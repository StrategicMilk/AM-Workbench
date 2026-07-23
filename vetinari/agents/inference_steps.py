"""Inference request execution steps for agent model calls."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Final

from vetinari.adapters.base import InferenceRequest
from vetinari.agents.observability import _ObservabilitySpan
from vetinari.constants import BATCH_RESULT_TIMEOUT, INFERENCE_STATUS_OK
from vetinari.context.window_manager import count_tokens
from vetinari.exceptions import InferenceError, ModelUnavailableError
from vetinari.guardrails.prompt_security import scan_prompt_security
from vetinari.safety.guardrails import redact_pii
from vetinari.safety.prompt_sanitizer import sanitize_task_description
from vetinari.security.redaction import redact_repr, redact_text, redact_value

logger = logging.getLogger(__name__)
_EPHEMERAL_CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TEMPERATURE = 0.3
_CONFIDENCE_GATE_SKIP_REASON_NO_LOGPROBS: Final = "missing_token_logprobs"
_CONFIDENCE_GATE_SKIP_REASON_LOCAL: Final = "local_inference_without_typed_signal"
_CONFIDENCE_GATE_SKIP_REASON_CACHE: Final = "semantic_cache_without_typed_signal"
_CONFIDENCE_GATE_SKIP_REASON_BATCH: Final = "batch_result_without_typed_signal"

_confidence_gate: Any | None = None
_confidence_gate_lock = threading.Lock()
_confidence_gate_skip_lock = threading.Lock()
confidence_gate_skips_total = 0


def _get_confidence_gate() -> Any:
    """Return the process-wide confidence gate, importing it only on first use."""
    global _confidence_gate
    if _confidence_gate is None:
        with _confidence_gate_lock:
            if _confidence_gate is None:
                from vetinari.agents.confidence_gate import ConfidenceGate

                _confidence_gate = ConfidenceGate()
    return _confidence_gate


def _record_confidence_gate_skip(agent: Any, metadata: dict[str, Any], reason: str) -> None:
    """Clear the previous decision and record a counted, typed skip reason."""
    global confidence_gate_skips_total
    metadata["confidence_gate_skip_reason"] = reason
    with _confidence_gate_skip_lock:
        confidence_gate_skips_total += 1
    agent._last_confidence_decision = None


def _stamp_non_direct_inference_state(state: _InferenceCallState, reason: str) -> None:
    """Prevent cached, local, or batch results from reusing direct-inference signals."""
    metadata: dict[str, Any] = {}
    _record_confidence_gate_skip(state.agent, metadata, reason)
    state.agent._last_inference_model_id = (
        state.request.model_id if state.request is not None else state.model_id or state.agent.default_model
    )
    state.agent._last_inference_temperature = state.temperature
    state.agent._last_inference_confidence = None
    state.agent._last_input_tokens = None
    state.agent._last_output_tokens = None
    state.agent._last_inference_metadata = metadata
    state.agent._last_tokens_used = 0
    state.agent._last_latency_ms = None
    state.agent._last_variant_id = state.variant_id
    state.agent._last_trace_id = str(getattr(state.agent, "_current_trace_id", "") or "")


@dataclass(slots=True)
class _InferenceCallState:
    agent: Any
    prompt: str
    system_prompt: str | None
    model_id: str | None
    max_tokens: int | None
    temperature: float | None
    expect_json: bool
    use_cascade: bool
    seams: Any
    agent_type_value: str
    agent_task_key: str
    active_system_prompt: str = ""
    variant_id: str = "default"
    memory_recall_metadata: dict[str, Any] | None = None
    cb_registry: Any = None
    request: InferenceRequest | None = None
    cache: Any = None
    cache_task_key: str = ""

    def __repr__(self) -> str:
        return redact_repr(
            type(self).__name__,
            {
                "agent": self.agent,
                "prompt": self.prompt,
                "system_prompt": self.system_prompt,
                "model_id": self.model_id,
            },
        )


def _contains_sensitive_text(value: str) -> bool:
    return redact_text(value) != value


def _sanitize_recalled_prompt_text(prompt_text: str) -> str:
    redacted = redact_pii(prompt_text)
    if scan_prompt_security(redacted):
        return sanitize_task_description(redacted)
    return redacted


def _make_state(
    agent: Any,
    prompt: str,
    system_prompt: str | None,
    model_id: str | None,
    max_tokens: int | None,
    temperature: float | None,
    expect_json: bool,
    use_cascade: bool,
    seams: Any,
) -> _InferenceCallState:
    agent_type_value = agent.agent_type.value if hasattr(agent.agent_type, "value") else str(agent.agent_type)
    return _InferenceCallState(
        agent=agent,
        prompt=prompt,
        system_prompt=system_prompt,
        model_id=model_id,
        max_tokens=max_tokens,
        temperature=temperature,
        expect_json=expect_json,
        use_cascade=use_cascade,
        seams=seams,
        agent_type_value=agent_type_value,
        agent_task_key=getattr(agent, "_current_task_type", None) or agent_type_value.lower(),
    )


def _precheck_circuit_breaker(state: _InferenceCallState) -> None:
    state.cb_registry = None if state.seams._LOCAL_ONLY_MODE else state.seams._get_circuit_breaker_registry()
    if state.cb_registry is None:
        return
    try:
        circuit_breaker = state.cb_registry.get(state.agent.agent_type.value)
        if circuit_breaker.allow_request():
            return
        state.agent._log("warning", "Circuit breaker OPEN for %s", state.agent.agent_type.value)
        raise InferenceError(f"Circuit breaker open for {state.agent.agent_type.value}")
    except InferenceError:
        raise
    except Exception as exc:
        logger.warning("Circuit breaker check failed: %s", exc)


def _precheck_budget(state: _InferenceCallState) -> None:
    budget_tracker = getattr(state.agent, "_budget_tracker", None)
    if budget_tracker is not None and not budget_tracker.check_budget():
        snap = budget_tracker.snapshot()
        state.agent._log(
            "warning",
            "Budget exhausted for %s (tokens=%d, iterations=%d)",
            state.agent.agent_type.value,
            snap.tokens_used,
            snap.iterations_used,
        )
        raise InferenceError(
            f"Budget exhausted for {state.agent.agent_type.value}: "
            f"tokens_used={snap.tokens_used}, iterations={snap.iterations_used}"
        )
    budget_remaining = getattr(state.agent, "_token_budget_remaining", None)
    if budget_remaining is not None and budget_remaining <= 0:
        state.agent._log("warning", "Token budget exhausted for %s", state.agent.agent_type.value)
        raise InferenceError(f"Token budget exhausted for {state.agent.agent_type.value}")


def _ensure_adapter_manager(state: _InferenceCallState) -> str | None:
    if state.agent._adapter_manager is None:
        state.agent._adapter_manager = state.seams._lazy_get_adapter_manager()
    if state.agent._adapter_manager is not None:
        return None
    try:
        system_prompt = state.active_system_prompt or state.system_prompt or state.agent.get_system_prompt()
        model = state.model_id or state.agent.default_model or "default"
        adapter = state.seams.get_local_inference_adapter(model)
        response = adapter.chat(model, system_prompt, state.prompt)
        output = response.get("output")
        if output is None:
            raise InferenceError(f"Inference adapter returned no 'output' key for model {model!r}")
        return str(output)
    except Exception as exc:
        state.agent._log("error", "LLM inference failed (no adapter_manager): %s", exc)
        raise ModelUnavailableError(f"No inference adapter available: {exc}") from exc


def _prepare_active_system_prompt(state: _InferenceCallState) -> None:
    agent_system = state.system_prompt or state.agent.get_system_prompt()
    resolved_model_id = state.model_id or state.agent.default_model
    state.active_system_prompt = state.agent._get_prompt_tier(model_id=resolved_model_id) + "\n\n" + agent_system


def _apply_prompt_evolver(state: _InferenceCallState) -> None:
    evolver = state.seams._lazy_get_prompt_evolver() if state.seams._PROMPT_EVOLVER_ENABLED else None
    if evolver is None:
        return
    try:
        evolved_prompt, state.variant_id = evolver.select_prompt(state.agent.agent_type.value)
        if evolved_prompt and evolved_prompt != state.active_system_prompt:
            state.active_system_prompt = evolved_prompt
    except Exception:
        logger.warning("Failed to select evolved prompt for agent %s", state.agent.agent_type.value, exc_info=True)


def _apply_prompt_assembler(state: _InferenceCallState) -> None:
    assembler = state.seams._lazy_get_prompt_assembler()
    if assembler is None:
        return
    try:
        task_type = getattr(state.agent, "_current_task_type", None) or "general"
        assembled = assembler.build(
            agent_type=state.agent.agent_type.value,
            task_type=task_type,
            task_description=state.prompt,
            mode=getattr(state.agent, "_current_mode", None),
            context_budget=getattr(state.agent, "_context_budget", 28000),
        )
        if assembled.get("system"):
            state.active_system_prompt = assembled["system"]
            if isinstance(assembled.get("memory_recall"), dict):
                state.memory_recall_metadata = redact_value(assembled["memory_recall"])
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "PromptAssembler built %d-char prompt for %s/%s (cache_hit=%s)",
                    assembled.get("total_chars", 0),
                    state.agent.agent_type.value,
                    task_type,
                    assembled.get("cache_hit", False),
                )
    except Exception:
        logger.warning("PromptAssembler unavailable for %s", state.agent.agent_type.value, exc_info=True)


def _inject_memory_recall(state: _InferenceCallState) -> None:
    prior_memories = getattr(state.agent, "_current_task_memories", None)
    if not prior_memories:
        return
    try:
        memory_pack = state.seams.build_memory_recall_pack(
            agent_type=state.agent.agent_type.value,
            task_type=getattr(state.agent, "_current_task_type", None) or "general",
            query=state.prompt,
            prior_memories=prior_memories,
        )
        state.memory_recall_metadata = redact_value(memory_pack.to_dict())
        if memory_pack.prompt_text:
            state.prompt = state.prompt + "\n\n" + _sanitize_recalled_prompt_text(memory_pack.prompt_text)
    except Exception as exc:
        logger.warning(
            "Memory recall packing failed for %s; memory content omitted: %s",
            state.agent.agent_type.value,
            exc,
            exc_info=True,
        )


def _apply_token_optimizer_profile(state: _InferenceCallState) -> None:
    optimizer = state.seams._lazy_get_token_optimizer()
    if optimizer is None:
        if state.max_tokens is None:
            state.max_tokens = _DEFAULT_MAX_TOKENS
        if state.temperature is None:
            state.temperature = _DEFAULT_TEMPERATURE
        return
    try:
        profile_max_tokens, profile_temp, _ = optimizer.get_task_profile(state.agent_task_key)
        if state.max_tokens is None:
            state.max_tokens = profile_max_tokens
        if state.temperature is None:
            state.temperature = profile_temp
    except (KeyError, ValueError):
        logger.warning("Token optimizer profile not available for %s - using defaults", state.agent.agent_type.value)
    finally:
        if state.max_tokens is None:
            state.max_tokens = _DEFAULT_MAX_TOKENS
        if state.temperature is None:
            state.temperature = _DEFAULT_TEMPERATURE


def _apply_inference_config(state: _InferenceCallState) -> None:
    config = state.seams._lazy_get_inference_config()
    model_for_config = state.model_id or state.agent.default_model
    if config is None:
        _apply_token_optimizer_profile(state)
        return
    try:
        effective = config.get_effective_params(state.agent_task_key, model_for_config)
        if state.max_tokens is None:
            state.max_tokens = effective.get("max_tokens", state.max_tokens)
        if state.temperature is None:
            state.temperature = effective.get("temperature", state.temperature)
    except (KeyError, ValueError):
        _apply_token_optimizer_profile(state)
    finally:
        if state.max_tokens is None:
            state.max_tokens = _DEFAULT_MAX_TOKENS
        if state.temperature is None:
            state.temperature = _DEFAULT_TEMPERATURE


def _apply_thompson_temperature(state: _InferenceCallState) -> None:
    try:
        thompson_pair = state.seams._lazy_get_thompson_strategy()
    except Exception:
        logger.warning("Thompson strategy unavailable; keeping config-based temperature", exc_info=True)
        return
    if thompson_pair is None:
        return
    try:
        thompson_strategy, _select_strategy_fn = thompson_pair
        agent_key = (
            state.agent.agent_type.value if hasattr(state.agent.agent_type, "value") else str(state.agent.agent_type)
        )
        mode_key = getattr(state.agent, "mode", "default")
        thompson_temp = thompson_strategy.select_strategy(agent_key, mode_key, "temperature")
        if isinstance(thompson_temp, (int, float)):
            state.temperature = float(thompson_temp)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[Thompson] Selected temperature %.2f for %s/%s", state.temperature, agent_key, mode_key)
    except Exception:
        logger.warning("Thompson temperature selection failed - keeping config-based value", exc_info=True)


def _enforce_prompt_budget(state: _InferenceCallState) -> None:
    try:
        context_length = getattr(state.agent, "_context_length", None) or 8192
        budget = state.seams.check_prompt_budget(
            state.active_system_prompt, state.prompt, state.max_tokens, context_length
        )
        if budget["fits"]:
            return
        preprocessor_cls = state.seams._get_local_preprocessor_cls()
        if preprocessor_cls is None:
            raise InferenceError(
                f"Prompt for {state.agent.agent_type.value} exceeds context window "
                f"({budget['total_tokens']} tokens > {context_length} n_ctx) - reduce prompt size"
            )
        compressed = preprocessor_cls().compress(state.prompt)
        budget2 = state.seams.check_prompt_budget(
            state.active_system_prompt, compressed, state.max_tokens, context_length
        )
        if budget2["fits"]:
            state.prompt = compressed
            logger.warning(
                "Prompt for %s compressed %d->%d tokens to fit n_ctx=%d",
                state.agent.agent_type.value,
                budget["task_tokens"],
                budget2["task_tokens"],
                context_length,
            )
            return
        raise InferenceError(
            f"Prompt for {state.agent.agent_type.value} exceeds context window "
            f"({budget['total_tokens']} tokens > {context_length} n_ctx) even after compression "
            f"({budget2['total_tokens']} tokens) - reduce prompt size"
        )
    except InferenceError:
        raise
    except Exception as exc:
        raise InferenceError(
            f"Prompt budget check failed for {state.agent.agent_type.value} - cannot verify context window fit, "
            "aborting inference to prevent silent truncation"
        ) from exc


def _build_request(state: _InferenceCallState) -> InferenceRequest:
    if state.expect_json:
        state.prompt = (
            state.prompt + "\n\nRespond ONLY with valid JSON. Do not include markdown code fences or explanation."
        )
    metadata: dict[str, Any] = {"agent": state.agent.agent_type.value, "cache_control": _EPHEMERAL_CACHE_CONTROL}
    if state.memory_recall_metadata is not None:
        metadata["memory_recall"] = state.memory_recall_metadata
    response_format = "json" if state.expect_json else None
    if state.expect_json:
        metadata["response_format"] = "json"
    state.request = InferenceRequest(
        model_id=state.model_id or state.agent.default_model or "default",
        prompt=state.prompt,
        system_prompt=state.active_system_prompt,
        max_tokens=state.max_tokens or _DEFAULT_MAX_TOKENS,
        temperature=state.temperature if state.temperature is not None else _DEFAULT_TEMPERATURE,
        response_format=response_format,
        metadata=metadata,
    )
    return state.request


def _semantic_cache_lookup(state: _InferenceCallState) -> str | None:
    state.cache = state.seams._lazy_get_semantic_cache()
    state.cache_task_key = state.agent_task_key
    if state.cache is None:
        return None
    if _contains_sensitive_text(state.prompt):
        logger.debug(
            "Semantic cache lookup skipped for task %s because prompt requires redaction", state.cache_task_key
        )
        return None
    try:
        cached_response = state.cache.get(
            state.prompt,
            task_type=state.cache_task_key,
            model_id=state.request.model_id if state.request else state.model_id or state.agent.default_model or "",
            system_prompt=state.active_system_prompt,
        )
        if cached_response is not None:
            logger.debug("Semantic cache hit for task %s", state.cache_task_key)
            return str(cached_response)
    except Exception:
        logger.warning(
            "Semantic cache lookup failed for task %s - proceeding with direct inference", state.cache_task_key
        )
    return None


def _batch_inference_result(state: _InferenceCallState) -> tuple[bool, str]:
    if not getattr(state.agent, "_batch_mode", False):
        return False, ""
    try:
        batch_processor = state.seams._lazy_get_batch_processor()
        if batch_processor is not None and batch_processor.enabled and state.request is not None:
            provider = state.request.metadata.get("provider", "anthropic")
            future = batch_processor.enqueue(state.request, provider=provider)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("Request enqueued for batch processing (provider=%s)", provider)
            batch_result = future.result(timeout=BATCH_RESULT_TIMEOUT)
            if batch_result.success:
                return True, batch_result.response.output if batch_result.response else ""
            logger.warning("Batch result failed: %s - falling back to direct", batch_result.error)
    except Exception as exc:
        logger.warning("Batch processing unavailable, falling back to direct: %s", exc)
    return False, ""


def _strip_json_markdown(result: str) -> str:
    stripped = result.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.split("\n")
    return "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])


def _record_circuit_success(state: _InferenceCallState) -> None:
    if state.cb_registry is None:
        return
    try:
        state.cb_registry.get(state.agent.agent_type.value).record_success()
    except Exception:
        logger.warning("Circuit breaker success recording failed for %s", state.agent.agent_type.value, exc_info=True)


def _record_circuit_failure(state: _InferenceCallState) -> None:
    if state.cb_registry is None:
        return
    try:
        state.cb_registry.get(state.agent.agent_type.value).record_failure()
    except Exception:
        logger.warning("Circuit breaker failure recording failed for %s", state.agent.agent_type.value, exc_info=True)


def _handle_success_response(state: _InferenceCallState, response: Any, call_start: float, obs_span: Any) -> str:
    if state.request is None:
        raise InferenceError("Inference request missing after adapter success")
    result = _strip_json_markdown(response.output) if state.expect_json else response.output
    _record_circuit_success(state)
    _apply_confidence_gate(state, response)
    reported_tokens = int(response.tokens_used or 0)
    budget_tokens = reported_tokens if reported_tokens > 0 else count_tokens(str(result))
    if hasattr(state.agent, "_token_budget_remaining") and state.agent._token_budget_remaining is not None:
        state.agent._token_budget_remaining -= budget_tokens
    budget_tracker = getattr(state.agent, "_budget_tracker", None)
    if budget_tracker is not None:
        budget_tracker.record_usage(budget_tokens, state.seams._infer_response_cost_usd(response, state.request))
    obs_span.set_attribute("prompt_tokens", count_tokens(state.prompt))
    obs_span.set_attribute("completion_tokens", budget_tokens)
    _store_semantic_cache(state, result)
    _stamp_agent_inference_state(state, response, reported_tokens, call_start)
    return str(result)


def _apply_confidence_gate(state: _InferenceCallState, response: Any) -> Any | None:
    """Route token log-probabilities and make an absent confidence signal observable."""
    response_metadata = response.metadata
    if not isinstance(response_metadata, dict):
        response_metadata = {}
        response.metadata = response_metadata
    logprobs = response_metadata.get("token_logprobs")
    if logprobs is None:
        _record_confidence_gate_skip(
            state.agent,
            response_metadata,
            _CONFIDENCE_GATE_SKIP_REASON_NO_LOGPROBS,
        )
        return None

    task_type = state.request.task_type if state.request and state.request.task_type else "general"
    gate = _get_confidence_gate()
    decision = gate.route_by_confidence(logprobs, task_type=task_type)
    state.agent._last_confidence_decision = decision
    return decision


def _store_semantic_cache(state: _InferenceCallState, result: str) -> None:
    if state.cache is None or not result:
        return
    if _contains_sensitive_text(state.prompt) or _contains_sensitive_text(result):
        logger.debug(
            "Semantic cache store skipped for task %s because content requires redaction", state.cache_task_key
        )
        return
    try:
        state.cache.put(
            state.prompt,
            result,
            model_id=state.request.model_id if state.request else state.model_id or state.agent.default_model or "",
            system_prompt=state.active_system_prompt,
        )
    except Exception:
        logger.warning("Semantic cache store failed for task %s - result not cached", state.cache_task_key)


def _stamp_agent_inference_state(
    state: _InferenceCallState, response: Any, reported_tokens: int, call_start: float
) -> None:
    response_metadata = dict(response.metadata or {})
    state.agent._last_inference_model_id = response.model_id
    state.agent._last_inference_temperature = state.temperature
    state.agent._last_inference_confidence = response.confidence
    state.agent._last_input_tokens = response.input_tokens
    state.agent._last_output_tokens = response.output_tokens
    if reported_tokens <= 0:
        response_metadata["usage_metering_status"] = "missing_or_zero"
    state.agent._last_inference_metadata = response_metadata
    state.agent._last_tokens_used = reported_tokens
    state.agent._last_latency_ms = (time.monotonic() - call_start) * 1000
    state.agent._last_variant_id = state.variant_id
    state.agent._last_trace_id = str(getattr(state.agent, "_current_trace_id", "") or "")


def _run_direct_inference(state: _InferenceCallState) -> str:
    if state.request is None:
        raise InferenceError("Inference request was not built")
    observability_span = getattr(state.seams, "_ObservabilitySpan", _ObservabilitySpan)
    with observability_span(
        f"agent.{state.agent.__class__.__name__}.infer",
        metadata={"agent_type": state.agent.agent_type.value, "model_name": state.request.model_id},
    ) as obs_span:
        try:
            call_start = time.monotonic()
            response = state.agent._adapter_manager.infer(state.request, use_cascade=state.use_cascade)
            if response.status == INFERENCE_STATUS_OK:
                return _handle_success_response(state, response, call_start, obs_span)
            state.agent._log("warning", "Inference failed: %s", response.error)
            _record_circuit_failure(state)
            raise InferenceError(f"Inference failed for {state.agent.agent_type.value}: {response.error}")
        except Exception as exc:
            state.agent._log("error", "Inference exception: %s", exc)
            _record_circuit_failure(state)
            raise InferenceError(f"Inference exception for {state.agent.agent_type.value}: {exc}") from exc


def _infer_steps(
    agent: Any,
    prompt: str,
    system_prompt: str | None,
    model_id: str | None,
    max_tokens: int | None,
    temperature: float | None,
    expect_json: bool,
    use_cascade: bool,
    seams: Any,
) -> str:
    state = _make_state(
        agent, prompt, system_prompt, model_id, max_tokens, temperature, expect_json, use_cascade, seams
    )
    _precheck_circuit_breaker(state)
    _precheck_budget(state)
    _prepare_active_system_prompt(state)
    _apply_prompt_evolver(state)
    _apply_prompt_assembler(state)
    _inject_memory_recall(state)
    _apply_inference_config(state)
    _apply_thompson_temperature(state)
    _enforce_prompt_budget(state)
    local_output = _ensure_adapter_manager(state)
    if local_output is not None:
        _stamp_non_direct_inference_state(state, _CONFIDENCE_GATE_SKIP_REASON_LOCAL)
        return local_output
    _build_request(state)
    cached_response = _semantic_cache_lookup(state)
    if cached_response is not None:
        _stamp_non_direct_inference_state(state, _CONFIDENCE_GATE_SKIP_REASON_CACHE)
        return cached_response
    batch_handled, batch_output = _batch_inference_result(state)
    if batch_handled:
        _stamp_non_direct_inference_state(state, _CONFIDENCE_GATE_SKIP_REASON_BATCH)
        return batch_output
    return _run_direct_inference(state)
