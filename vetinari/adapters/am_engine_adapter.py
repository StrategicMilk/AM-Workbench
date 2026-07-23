"""Provider adapter for the first-party AM Engine runtime.

AM Engine confidence is the geometric mean token probability supplied by the
engine DTO: ``confidence = exp(mean(token_logprobs))``.  The adapter consumes
that value; it does not recompute or reinterpret it.

Cost ownership for ``am_engine`` belongs exclusively to
``vetinari/engine/events.py`` exact-token ingest.  Adapter-side split-token
estimates would double-count, so :class:`AmEngineAdapter` deliberately omits
the base adapter's cost-tracker leg while preserving its other telemetry.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from itertools import starmap
from pathlib import Path
from typing import Any

from vetinari import metrics
from vetinari.adapters import base as adapter_base
from vetinari.constants import INFERENCE_STATUS_ERROR, INFERENCE_STATUS_OK
from vetinari.engine.client_types import ChatRequest, EngineErrorCode, EvalContext
from vetinari.engine.eval_receipts import (
    EvalRequestCorrelation,
    create_eval_receipt_tracker,
    verify_engine_response_receipt,
)
from vetinari.types import AgentType, PriorityClass

from .base import InferenceRequest, InferenceResponse, ModelInfo, ProviderAdapter, ProviderConfig, ProviderType

logger = logging.getLogger(__name__)


ENGINE_ERROR_FAILURE_CLASSES: dict[str, str] = {code.value: code.value for code in EngineErrorCode}
"""The single engine-error-to-failure-class mapping; keys and values are bijective."""

AGENT_TYPE_PRIORITY: dict[AgentType, PriorityClass] = {
    AgentType.FOREMAN: PriorityClass.INTERACTIVE,
    AgentType.INSPECTOR: PriorityClass.INTERACTIVE,
    AgentType.WORKER: PriorityClass.WORKER,
    AgentType.TRAINING: PriorityClass.BACKGROUND,
}
"""Explicit R6 priority assignments; all other callers fail safely to WORKER."""


def derive_priority_class(agent_type: AgentType | str | None, *, eval_slot: int | None = None) -> PriorityClass:
    """Derive the engine queue priority and count every defaulted caller.

    Returns:
        ``EVAL`` for explicit eval traffic, an R6 table result for mapped agent
        types, or ``WORKER`` for unknown and deliberately-defaulted callers.
    """
    if eval_slot is not None:
        return PriorityClass.EVAL
    normalized: AgentType | None
    if isinstance(agent_type, AgentType):
        normalized = agent_type
    else:
        try:
            normalized = AgentType(str(agent_type))
        except ValueError:
            normalized = None
    priority = AGENT_TYPE_PRIORITY.get(normalized) if normalized is not None else None
    if priority is not None:
        return priority
    metrics.get_metrics().increment(
        "vetinari.am_engine.unmapped_agent_priority",
        agent_type=normalized.value if normalized is not None else str(agent_type or "unknown"),
    )
    return PriorityClass.WORKER


def _normalized_eval_slot(eval_slot: int | None, priority: PriorityClass) -> int | None:
    """Validate the engine's paired EVAL priority/slot contract.

    Returns:
        The canonical non-negative integer slot for EVAL requests, otherwise
        ``None``.
    """
    if priority is not PriorityClass.EVAL:
        if eval_slot is not None:
            raise ValueError("eval_slot is only valid with EVAL priority")
        return None
    if isinstance(eval_slot, bool) or eval_slot is None:
        raise ValueError("EVAL priority requires a non-negative integer eval_slot")
    if not isinstance(eval_slot, int):
        raise ValueError("EVAL priority requires a non-negative integer eval_slot")
    if eval_slot < 0:
        raise ValueError("EVAL priority requires a non-negative integer eval_slot")
    return eval_slot


def _normalized_eval_seed(seed: int, priority: PriorityClass) -> int | None:
    """Validate the engine's explicit unsigned EVAL seed contract.

    Returns:
        The unsigned 64-bit EVAL seed, otherwise ``None`` for a random seed.
    """
    if priority is not PriorityClass.EVAL:
        return seed if seed >= 0 else None
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 0xFFFF_FFFF_FFFF_FFFF:
        raise ValueError("EVAL priority requires an explicit unsigned 64-bit seed")
    return seed


def _engine_model_id(result: object) -> str:
    """Return the model identity reported by the actual engine response."""
    raw = getattr(result, "raw", None)
    raw_model_id = None
    if isinstance(raw, Mapping):
        raw_model_id = raw.get("model_id") or raw.get("model")
    candidate = getattr(result, "model_id", None) or raw_model_id
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError("AM Engine response omitted actual model identity")
    return candidate.strip()


class AmEngineAdapter(ProviderAdapter):
    """Translate the Vetinari provider contract onto the sole AM Engine client."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        client_factory: Callable[[], Any] | None = None,
        supervisor_factory: Callable[[], Any] | None = None,
        configured_model_paths_fn: Callable[[], list[Path]] | None = None,
        scan_fn: Callable[[list[Path]], list[Any]] | None = None,
    ) -> None:
        super().__init__(config)
        if config.provider_type is not ProviderType.AM_ENGINE:
            raise ValueError("AmEngineAdapter requires ProviderType.AM_ENGINE")
        self._client_factory = client_factory
        self._supervisor_factory = supervisor_factory
        self._configured_model_paths_fn = configured_model_paths_fn
        self._scan_fn = scan_fn
        self._eval_receipt_tracker = create_eval_receipt_tracker()
        self._vision_enabled = _coerce_bool(config.extra_config.get("vision_enabled"), default=False)
        self._semantic_cache_enabled = _coerce_bool(
            config.extra_config.get("semantic_cache_enabled"),
            default=True,
        )

    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        from vetinari.engine import get_engine_client

        return get_engine_client()

    def _supervisor(self) -> Any:
        if self._supervisor_factory is not None:
            return self._supervisor_factory()
        from vetinari.engine import get_supervisor

        return get_supervisor()

    def discover_models(self) -> list[ModelInfo]:
        """Merge engine load state with installed-model scan records.

        Returns:
            Stable model metadata, with the engine authoritative for current
            load state and sidecar/local scan data filling descriptive fields.
        """
        engine_rows = self._engine_model_rows(self._client())
        scan_rows = self._scan_model_rows()
        merged: dict[str, dict[str, Any]] = {}
        for row in scan_rows:
            merged[str(row["id"])] = dict(row)
        for row in engine_rows:
            model_id = str(row["id"])
            merged[model_id] = {**merged.get(model_id, {}), **row}
        self.models = list(starmap(self._model_info, sorted(merged.items())))
        return list(self.models)

    def health_check(self) -> dict[str, Any]:
        """Return the base adapter's stable health dictionary shape.

        Returns:
            Health status, reason, and an ISO-8601 UTC timestamp.
        """
        timestamp = _utc_timestamp()
        try:
            probe = self._client().readyz()
            payload = getattr(probe, "payload", {})
            healthy = bool(payload.get("ready", payload.get("healthy", True))) if isinstance(payload, Mapping) else True
            reason = "AM Engine ready" if healthy else str(payload.get("reason", "AM Engine not ready"))
            return {"healthy": healthy, "reason": reason, "timestamp": timestamp}
        except Exception as exc:
            logger.warning("AM Engine health probe failed: %s", exc)
            return {"healthy": False, "reason": str(exc), "timestamp": timestamp}

    def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Run one non-streaming inference through the shared engine client.

        Returns:
            A typed success or failure response preserving engine token data.
        """
        self._emit_inference_started(request)
        started_ns = time.perf_counter_ns()
        if request.images and not self._vision_enabled:
            return self._failure_response(request, started_ns, "template_untrusted", "AM Engine vision is disabled")

        try:
            priority = self._priority_for(request)
        except ValueError as exc:
            logger.warning("AM Engine priority value was rejected: %s", exc)
            return self._failure_response(request, started_ns, "internal", str(exc))

        is_eval = priority is PriorityClass.EVAL
        correlation = None
        if is_eval:
            try:
                if request.images:
                    raise ValueError("AM Engine EVAL receipt schema v1 does not support image inputs")
                _normalized_eval_seed(request.seed, priority)
                correlation = EvalRequestCorrelation.from_metadata(request.metadata or {})
            except ValueError as exc:
                logger.warning("AM Engine eval correlation was rejected: %s", exc)
                return self._failure_response(request, started_ns, "internal", str(exc))
        elif "eval_context" in (request.metadata or {}):
            return self._failure_response(
                request,
                started_ns,
                "internal",
                "eval_context is only valid with EVAL priority",
            )
        if is_eval:
            cache_model_id, cache_context = request.model_id, ""
            cached = None
        else:
            cache_model_id, cache_context = self._semantic_cache_identity(request, priority)
            cached = self._semantic_cache_lookup(request, cache_model_id, cache_context)
        if cached is not None:
            response = InferenceResponse(
                model_id=request.model_id,
                output=cached,
                latency_ms=0,
                tokens_used=0,
                status=INFERENCE_STATUS_OK,
                metadata={"cache_hit": True, "priority_class": priority.value},
            )
            self._record_telemetry(request, response)
            return response

        try:
            supervisor = self._supervisor()
            supervisor.ensure_running()
            chat_request = self._chat_request(request, priority, correlation=correlation)
            trust_context = supervisor.receipt_trust_context() if is_eval else None
            engine_instance_id = supervisor.receipt_engine_instance_id() if is_eval else None
            result = self._client().chat(chat_request)
            input_tokens = getattr(result, "input_tokens", None)
            output_tokens = getattr(result, "output_tokens", None)
            actual_model_id = _engine_model_id(result) if is_eval else request.model_id
            if is_eval and actual_model_id != request.model_id:
                raise ValueError("AM Engine response model does not match requested model")
            request_id = getattr(result, "request_id", None)
            if not isinstance(request_id, str) or not request_id.strip():
                raise ValueError("AM Engine response omitted request_id")
            trace_id = getattr(result, "trace_id", None)
            receipt_id = None
            engine_receipt = None
            engine_model_sha256 = None
            if is_eval:
                if correlation is None:
                    raise ValueError("AM Engine eval correlation was not established")
                if trust_context is None:
                    raise ValueError("AM Engine eval trust context was not established")
                if not isinstance(engine_instance_id, str) or not engine_instance_id.strip():
                    raise ValueError("AM Engine eval engine instance identity was not established")
                if not isinstance(trace_id, str) or not trace_id.strip():
                    raise ValueError("AM Engine response omitted trace_id")
                engine_receipt = getattr(result, "engine_receipt", None)
                verified = verify_engine_response_receipt(
                    engine_receipt,
                    trust_context=trust_context,
                    correlation=correlation,
                    engine_instance_id=engine_instance_id,
                    request_id=request_id,
                    trace_id=trace_id,
                    model_id=actual_model_id,
                    seed=request.seed,
                    eval_slot=_normalized_eval_slot(request.eval_slot, priority) or 0,
                    messages=chat_request.messages,
                    output=result.content,
                    tracker=self._eval_receipt_tracker,
                )
                receipt_id = verified.receipt_id
                engine_model_sha256 = verified.model_sha256
            response = InferenceResponse(
                model_id=actual_model_id,
                output=result.content,
                latency_ms=_elapsed_ms(started_ns),
                tokens_used=int(input_tokens or 0) + int(output_tokens or 0),
                status=INFERENCE_STATUS_OK,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                confidence=getattr(result, "confidence", None),
                metadata={
                    "cache_hit": False,
                    "priority_class": priority.value,
                    "request_id": request_id,
                    "trace_id": trace_id,
                    "engine_instance_id": engine_instance_id,
                    "engine_model_id": actual_model_id,
                    "engine_model_sha256": engine_model_sha256,
                    "eval_receipt_id": receipt_id,
                    "engine_receipt": dict(engine_receipt) if isinstance(engine_receipt, Mapping) else None,
                    "eval_evidence_origin": "am_engine" if is_eval else None,
                },
            )
            if not is_eval:
                self._semantic_cache_store(request, response, cache_model_id, cache_context)
        except Exception as exc:
            failure_class = _failure_class_for_exception(exc)
            response = InferenceResponse(
                model_id=request.model_id,
                output="",
                latency_ms=_elapsed_ms(started_ns),
                tokens_used=0,
                status=INFERENCE_STATUS_ERROR,
                error=failure_class,
                metadata={"engine_error": str(exc)},
            )
        self._record_telemetry(request, response)
        return response

    def stream_inference(self, request: InferenceRequest) -> Iterator[Mapping[str, Any]]:
        """Yield the shared engine stream while mirroring it to the local UI tee.

        Closing an observer EventSource never cancels this iterator. Only the
        tee's explicit cancel endpoint calls the active engine stream's
        cancellation surface.
        """
        priority = self._priority_for(request)
        if priority is PriorityClass.EVAL:
            raise ValueError("AM Engine evaluation requires non-streaming receipt verification")
        self._supervisor().ensure_running()
        stream = self._client().chat_stream(self._chat_request(request, priority))
        from vetinari.engine.stream_tee import mirror

        yield from mirror(stream)

    def get_capabilities(self) -> dict[str, list[str]]:
        """Return capabilities for each discovered model.

        Returns:
            Model identifiers mapped to their adapter-level capabilities.
        """
        capabilities = ["chat", "text_generation"]
        if self._vision_enabled:
            capabilities.append("vision")
        models = self.models or self.discover_models()
        return {model.id: list(capabilities) for model in models}

    def _priority_for(self, request: InferenceRequest) -> PriorityClass:
        if request.priority_class is not None:
            priority = (
                request.priority_class
                if isinstance(request.priority_class, PriorityClass)
                else PriorityClass(str(request.priority_class))
            )
        else:
            metadata = request.metadata or {}
            priority = derive_priority_class(
                metadata.get("agent_type") or metadata.get("agent"),
                eval_slot=request.eval_slot,
            )
        _normalized_eval_slot(request.eval_slot, priority)
        return priority

    def _chat_request(
        self,
        request: InferenceRequest,
        priority: PriorityClass,
        *,
        correlation: EvalRequestCorrelation | None = None,
    ) -> ChatRequest:
        eval_slot = _normalized_eval_slot(request.eval_slot, priority)
        if (priority is PriorityClass.EVAL) != (correlation is not None):
            raise ValueError("EVAL priority and validated eval correlation must be supplied together")
        grammar = request.grammar
        if grammar is None and request.task_type:
            from vetinari.adapters.grammar_library import get_grammar_for_task_type

            grammar = get_grammar_for_task_type(request.task_type)
        messages: list[dict[str, Any]] = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        user_message: dict[str, Any] = {"role": "user", "content": request.prompt}
        if request.images:
            user_message["images"] = list(request.images)
        messages.append(user_message)
        extra = {
            key: value
            for key, value in {
                "top_k": request.top_k,
                "stop_sequences": list(request.stop_sequences),
                "repeat_penalty": request.repeat_penalty,
                "frequency_penalty": request.frequency_penalty,
                "min_p": request.min_p,
                "presence_penalty": request.presence_penalty,
                "mirostat_mode": request.mirostat_mode if request.mirostat_mode > 0 else None,
                "mirostat_tau": request.mirostat_tau if request.mirostat_mode > 0 else None,
                "mirostat_eta": request.mirostat_eta if request.mirostat_mode > 0 else None,
                "response_format": request.response_format,
                "grammar": grammar,
                "logit_bias": request.logit_bias,
                "typical_p": request.typical_p if request.typical_p > 0 else None,
                "tfs_z": request.tfs_z if request.tfs_z > 0 else None,
                "session_id": request.session_id,
                "dry_multiplier": request.dry_multiplier,
                "dry_base": request.dry_base,
                "dry_allowed_length": request.dry_allowed_length,
                "xtc_probability": request.xtc_probability,
                "xtc_threshold": request.xtc_threshold,
                "top_n_sigma": request.top_n_sigma,
            }.items()
            if value is not None
        }
        return ChatRequest(
            messages=tuple(messages),
            model_id=request.model_id,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
            seed=_normalized_eval_seed(request.seed, priority),
            priority_class=priority.value,
            eval_slot=eval_slot,
            eval_context=(
                EvalContext(
                    run_id=correlation.run_id,
                    suite_id=correlation.suite_id,
                    suite_revision_sha256=correlation.suite_revision_sha256,
                    case_id=correlation.case_id,
                    ordinal=correlation.ordinal,
                    case_spec_sha256=correlation.case_spec_sha256,
                )
                if correlation is not None
                else None
            ),
            prefix_refs=tuple({"prefix_name": ref, "content_hash": ref} for ref in request.prefix_refs),
            extra=extra,
        )

    def _failure_response(
        self,
        request: InferenceRequest,
        started_ns: int,
        failure_class: str,
        detail: str,
    ) -> InferenceResponse:
        response = InferenceResponse(
            model_id=request.model_id,
            output="",
            latency_ms=_elapsed_ms(started_ns),
            tokens_used=0,
            status=INFERENCE_STATUS_ERROR,
            error=ENGINE_ERROR_FAILURE_CLASSES[failure_class],
            metadata={"engine_error": detail},
        )
        self._record_telemetry(request, response)
        return response

    def _semantic_cache_identity(self, request: InferenceRequest, priority: PriorityClass) -> tuple[str, str]:
        identity = {
            "adapter": "am_engine.v1",
            "model_id": request.model_id,
            "priority_class": priority.value,
            "sampling": self._chat_request(request, priority).to_wire(),
        }
        context = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
        endpoint_hash = hashlib.sha256(str(self.endpoint).encode("utf-8")).hexdigest()
        return f"am_engine:{endpoint_hash}:{request.model_id}", context

    def _semantic_cache_lookup(self, request: InferenceRequest, model_id: str, context: str) -> str | None:
        if not self._semantic_cache_enabled:
            return None
        try:
            from vetinari.optimization.semantic_cache import get_semantic_cache

            return get_semantic_cache().get(
                request.prompt,
                task_type=request.task_type or "",
                model_id=model_id,
                system_prompt=context,
            )
        except Exception:
            logger.warning("AM Engine semantic cache lookup failed; continuing uncached", exc_info=True)
            return None

    def _semantic_cache_store(
        self,
        request: InferenceRequest,
        response: InferenceResponse,
        model_id: str,
        context: str,
    ) -> None:
        if not (self._semantic_cache_enabled and response.output):
            return
        try:
            from vetinari.optimization.semantic_cache import get_semantic_cache

            get_semantic_cache().put(request.prompt, response.output, model_id=model_id, system_prompt=context)
        except Exception:
            logger.warning("AM Engine semantic cache store failed", exc_info=True)

    def _engine_model_rows(self, client: Any) -> list[dict[str, Any]]:
        list_models = getattr(client, "list_models", None)
        if not callable(list_models):
            raise RuntimeError("AM Engine client is missing the required public list_models contract")
        payload = list_models()
        raw_rows: Any = payload.get("models", payload.get("data", [])) if isinstance(payload, Mapping) else []
        if isinstance(raw_rows, Mapping):
            raw_rows = [raw_rows]
        rows: list[dict[str, Any]] = []
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            if not isinstance(raw, Mapping):
                continue
            model_id = raw.get("id") or raw.get("model_id") or raw.get("name")
            if model_id:
                rows.append({"id": str(model_id), **dict(raw), "loaded": bool(raw.get("loaded", True))})
        return rows

    def _scan_model_rows(self) -> list[dict[str, Any]]:
        if self._configured_model_paths_fn is None or self._scan_fn is None:
            from vetinari.models.scan import configured_model_paths, scan

            records: list[Any] = list(scan(configured_model_paths()))
        else:
            records = self._scan_fn(self._configured_model_paths_fn())
        rows: list[dict[str, Any]] = []
        for record in records:
            sidecar = _read_model_sidecar(getattr(record, "path", ""))
            rows.append({
                "id": str(record.model_id),
                "name": sidecar.get("name", record.model_id),
                "path": record.path,
                "format": getattr(getattr(record, "format", None), "value", "unknown"),
                "size_bytes": int(getattr(record, "size_bytes", 0)),
                "loaded": False,
                **sidecar,
            })
        return rows

    def _model_info(self, model_id: str, row: Mapping[str, Any]) -> ModelInfo:
        size_bytes = int(row.get("size_bytes", 0) or 0)
        memory_gb = int(row.get("memory_gb", 0) or 0) or max(1, (size_bytes + (1024**3 - 1)) // 1024**3)
        tags = ["loaded" if row.get("loaded") else "installed"]
        if row.get("format"):
            tags.append(str(row["format"]))
        return ModelInfo(
            id=model_id,
            name=str(row.get("name") or model_id),
            provider=ProviderType.AM_ENGINE.value,
            endpoint=self.endpoint,
            capabilities=list(row.get("capabilities") or ["chat", "text_generation"]),
            context_len=int(row.get("context_len", row.get("context_length", 0)) or 0),
            memory_gb=memory_gb,
            version=str(row.get("version") or "unknown"),
            cost_per_1k_tokens=0.0,
            tags=tags,
        )

    def _record_telemetry(self, request: InferenceRequest, response: InferenceResponse) -> None:
        """Preserve base telemetry except the intentionally absent cost leg."""
        provider = self.provider_type.value
        try:
            tracer = adapter_base.get_genai_tracer()
            span = tracer.start_agent_span(agent_name="llm", operation="inference", model=request.model_id)
            span.attributes["latency_ms"] = response.latency_ms
            span.attributes["gen_ai.usage.input_tokens"] = response.input_tokens or 0
            span.attributes["gen_ai.usage.output_tokens"] = response.output_tokens or response.tokens_used
            span.attributes["gen_ai.response.model"] = response.model_id
            tracer.end_agent_span(
                span,
                status=INFERENCE_STATUS_OK if response.status == INFERENCE_STATUS_OK else INFERENCE_STATUS_ERROR,
                tokens_used=response.tokens_used,
            )
        except Exception:
            logger.warning("GenAI tracer unavailable for AM Engine inference", exc_info=True)
        try:
            adapter_base.log_event(
                "info" if response.status == INFERENCE_STATUS_OK else "warning",
                __name__,
                "inference_completed",
                model_id=request.model_id,
                latency_ms=response.latency_ms,
                input_tokens=response.input_tokens or 0,
                output_tokens=response.output_tokens or 0,
                status="completed" if response.status == INFERENCE_STATUS_OK else "failed",
            )
        except Exception:
            logger.warning("Failed to emit AM Engine completion event", exc_info=True)
        try:
            adapter_base.get_telemetry_collector().record_adapter_latency(
                provider=provider,
                model=request.model_id,
                latency_ms=response.latency_ms,
                tokens_used=response.tokens_used,
                success=response.status == INFERENCE_STATUS_OK,
            )
        except Exception:
            logger.warning("Failed to record AM Engine adapter telemetry", exc_info=True)
        if response.status != INFERENCE_STATUS_OK:
            metadata = request.metadata or {}
            adapter_base._record_model_call_failure_metric(
                project_id=str(metadata.get("project_id") or "unknown"),
                task_id=str(metadata.get("task_id") or "unknown"),
                agent_type=str(metadata.get("agent_type") or metadata.get("agent") or "unknown"),
                model_id=request.model_id,
                failure_class=str(response.error or "internal"),
            )
        try:
            tracker = adapter_base.get_sla_tracker()
            tracker.record_latency(
                f"{provider}:{request.model_id}",
                latency_ms=float(response.latency_ms),
                success=response.status == INFERENCE_STATUS_OK,
            )
            tracker.record_request(success=response.status == INFERENCE_STATUS_OK)
        except Exception:
            logger.warning("Failed to record AM Engine SLA metrics", exc_info=True)
        try:
            forecaster = adapter_base.get_forecaster()
            forecaster.ingest("adapter.latency", float(response.latency_ms))
            forecaster.ingest("adapter.tokens", float(response.tokens_used or 0))
        except Exception:
            logger.warning("Failed to record AM Engine forecasting telemetry", exc_info=True)
        try:
            result = adapter_base.get_anomaly_detector().detect("adapter.latency", float(response.latency_ms))
            if result.is_anomaly:
                logger.warning("AM Engine latency anomaly: value=%s score=%.2f", result.value, result.score)
        except Exception:
            logger.warning("Failed to record AM Engine anomaly telemetry", exc_info=True)


def _failure_class_for_exception(exc: Exception) -> str:
    raw_code = getattr(exc, "code", None)
    code = raw_code.value if isinstance(raw_code, EngineErrorCode) else str(raw_code or "internal")
    return ENGINE_ERROR_FAILURE_CLASSES.get(code, ENGINE_ERROR_FAILURE_CLASSES["internal"])


def _read_model_sidecar(raw_path: object) -> dict[str, Any]:
    if not raw_path:
        return {}
    path = Path(str(raw_path))
    candidates = (path.with_suffix(path.suffix + ".metadata.json"), path.with_suffix(".metadata.json"))
    for candidate in candidates:
        try:
            if candidate.is_file():
                payload = json.loads(candidate.read_text(encoding="utf-8"))
                return dict(payload) if isinstance(payload, Mapping) else {}
        except (OSError, json.JSONDecodeError):
            logger.warning("Ignoring unreadable AM Engine model sidecar %s", candidate, exc_info=True)
    return {}


def _coerce_bool(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"", "0", "false", "no", "off"}


def _elapsed_ms(started_ns: int) -> int:
    return max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)


def _utc_timestamp() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
