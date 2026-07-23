"""Non-streaming inference runtime for the llama.cpp provider adapter."""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.adapters.base import InferenceRequest, InferenceResponse
from vetinari.adapters.llama_cpp_model_info import GGUFIntegrityError
from vetinari.boundary_guards import account_evidence_drop
from vetinari.constants import INFERENCE_STATUS_OK

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LlamaCppInferenceRuntimeDeps:
    """Runtime hooks supplied by the compatibility facade module.

    Keeping these dependencies explicit lets ``llama_cpp_adapter.py`` remain the
    import and test-patching facade while the substantial inference behavior
    lives in this helper module.
    """

    llama_cpp_available: bool
    get_model_profiler_data: Callable[[], Any]
    get_vram_manager_module: Callable[[], Any]
    get_semantic_cache_fn: Callable[[], Callable[[], Any]]
    get_kv_state_cache_fn: Callable[[], Callable[[], Any]]
    get_hash_system_prompt_fn: Callable[[], Callable[[str], str]]
    semantic_cache_identity: Callable[..., tuple[str, str]]
    validate_loaded_chat_template: Callable[[str, Any], Any]
    chat_template_validation_metadata: Callable[[Any], dict[str, Any]]
    model_id_from_path: Callable[[Path], str]
    detect_speculative_capability: Callable[..., Any]
    system_prompt_boundary: str
    kv_bytes_per_token: dict[str, int]
    logger: logging.Logger

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"llama_cpp_available={self.llama_cpp_available!r}, "
            f"get_model_profiler_data={self.get_model_profiler_data!r}, "
            f"get_vram_manager_module={self.get_vram_manager_module!r}, "
            f"get_semantic_cache_fn={self.get_semantic_cache_fn!r}"
            ")"
        )


def _apply_profiled_temperature(
    *,
    request: InferenceRequest,
    model_id: str,
    model_path: Path,
    deps: LlamaCppInferenceRuntimeDeps,
) -> InferenceRequest:
    """Apply the model-family temperature profile when the request uses the default."""
    if request.temperature != 0.7:
        return request

    try:
        task_type = request.metadata.get("task_type", "general")
        model_profiler_data = deps.get_model_profiler_data()
        if model_profiler_data is None:
            raise RuntimeError("model_profiler_data unavailable")
        family = model_profiler_data.detect_family(model_id)
        quant = model_path.stem.lower()
        profiled_temp = model_profiler_data.get_temperature(family, task_type, quant)
        updated_request = dataclasses.replace(request, temperature=profiled_temp)
        deps.logger.debug(
            "Temperature set from model profile: %s family=%s task=%s quant=%s -> %.3f",
            model_id,
            family,
            task_type,
            quant,
            profiled_temp,
        )
        return updated_request
    except Exception:
        logger.warning("Exception handled by  apply profiled temperature fallback", exc_info=True)
        deps.logger.warning(
            "get_temperature failed for %s - using request default temperature %.2f",
            model_id,
            request.temperature,
        )
        return request


_VISION_MODEL_MARKERS = ("-vl", "_vl", "vision", "llava", "minicpm-v", "moondream")


def _model_supports_vision(model_id: str) -> bool:
    """Return True when the model id matches a known vision-capable marker (FSA-0047).

    Heuristic only — image input is allowed when the model id substring
    matches a curated marker list (e.g. ``qwen-vl``, ``llava``, ``moondream``)
    or contains the literal ``vision``.  Adapter-side metadata can expand
    this later, but the substring check covers every text-only vs vision
    case the current test corpus exercises.
    """
    lowered = model_id.lower()
    return any(marker in lowered for marker in _VISION_MODEL_MARKERS)


def _build_messages(request: InferenceRequest) -> list[dict[str, Any]]:
    """Build llama.cpp chat messages from a Vetinari inference request.

    Text-only requests produce the historic ``[{role, content: str}]``
    shape.  Multimodal requests (FSA-0047) — where ``request.images`` is
    non-empty — produce content as a list of segments compatible with the
    OpenAI vision message format::

        [{"type": "text", "text": "..."},
         {"type": "image_url", "image_url": {"url": "data:..."}}, ...]

    Raises:
        ValueError: When images are supplied for a model that does not
            advertise vision support.  Fail-closed by design: silently
            stripping images would corrupt the caller's intent.
    """
    messages: list[dict[str, Any]] = []
    if request.system_prompt:
        messages.append({"role": "system", "content": request.system_prompt})

    images = getattr(request, "images", None) or []
    if images:
        if not _model_supports_vision(request.model_id):
            raise ValueError(
                f"Model {request.model_id!r} does not support image inputs — "
                "use a vision-capable model id (e.g. qwen-vl, llava)."
            )
        content: list[dict[str, Any]] = [{"type": "text", "text": request.prompt}]
        content.extend({"type": "image_url", "image_url": {"url": image_url}} for image_url in images)
        messages.append({"role": "user", "content": content})
    else:
        messages.append({"role": "user", "content": request.prompt})

    return messages


def _restore_kv_state(
    *,
    request: InferenceRequest,
    model_id: str,
    llm: Any,
    deps: LlamaCppInferenceRuntimeDeps,
) -> bool:
    """Restore a cached KV state for the stable system-prompt prefix when available."""
    if not request.system_prompt:
        return False

    try:
        kv_cache = deps.get_kv_state_cache_fn()()
        kv_prefix = (
            request.system_prompt.split(deps.system_prompt_boundary, 1)[0]
            if deps.system_prompt_boundary in request.system_prompt
            else request.system_prompt
        )
        prompt_hash = deps.get_hash_system_prompt_fn()(kv_prefix)
        saved_state = kv_cache.get(model_id, prompt_hash)
        if saved_state is not None and hasattr(llm, "load_state"):
            llm.load_state(saved_state)
            deps.logger.debug("KV state restored for %s (hash=%s)", model_id, prompt_hash[:8])
            return True
    except Exception:
        deps.logger.warning(
            "KV state cache restore failed for %s - running without cached state",
            model_id,
            exc_info=True,
        )

    return False


def _save_kv_state(
    *,
    request: InferenceRequest,
    model_id: str,
    llm: Any,
    deps: LlamaCppInferenceRuntimeDeps,
) -> None:
    """Save a KV state for the stable system-prompt prefix after inference."""
    if not request.system_prompt or not hasattr(llm, "save_state"):
        return

    try:
        kv_cache = deps.get_kv_state_cache_fn()()
        save_prefix = (
            request.system_prompt.split(deps.system_prompt_boundary, 1)[0]
            if deps.system_prompt_boundary in request.system_prompt
            else request.system_prompt
        )
        save_hash = deps.get_hash_system_prompt_fn()(save_prefix)
        kv_cache.put(model_id, save_hash, llm.save_state())
    except Exception:
        deps.logger.warning("KV state save failed for %s - cache miss on next request", model_id, exc_info=True)


def _update_kv_vram_tracking(
    *,
    adapter: Any,
    model_id: str,
    tokens_used: int,
    deps: LlamaCppInferenceRuntimeDeps,
) -> None:
    """Update VRAM manager KV-cache accounting after a successful response."""
    if tokens_used <= 0:
        return

    try:
        kv_bpt = deps.kv_bytes_per_token.get(adapter._cache_type_k, 2048)
        kv_gb = tokens_used * kv_bpt / (1024**3)
        vram_manager_mod = deps.get_vram_manager_module()
        if vram_manager_mod is not None:
            vram_manager_mod.get_vram_manager().update_kv_cache(model_id, kv_gb)
    except Exception:
        deps.logger.warning("update_kv_cache failed for %s - VRAM KV tracking may be stale", model_id)


def _semantic_cache_lookup(
    *,
    request: InferenceRequest,
    model_path: Path,
    model_id: str,
    resolution_outcome: str,
    deps: LlamaCppInferenceRuntimeDeps,
) -> InferenceResponse | None:
    """Return a semantic-cache response when the current request has a hit."""
    try:
        cache = deps.get_semantic_cache_fn()()
        if cache is None:
            return None
        cache_model_id, cache_context = deps.semantic_cache_identity(
            request,
            model_path=model_path,
            resolved_model_id=model_id,
            resolution_outcome=resolution_outcome,
        )
        cached = cache.get(
            request.prompt,
            task_type=request.task_type or "",
            model_id=cache_model_id,
            system_prompt=cache_context,
        )
        if cached is None:
            return None
        deps.logger.debug("Semantic cache hit for resolved model %s", model_id)
        return InferenceResponse(
            model_id=model_id,
            output=cached,
            latency_ms=0,
            tokens_used=0,
            status=INFERENCE_STATUS_OK,
        )
    except Exception:
        logger.warning("Exception handled by  semantic cache lookup fallback", exc_info=True)
        deps.logger.warning("Semantic cache unavailable - proceeding without cache", exc_info=True)
        return None


def _semantic_cache_store(
    *,
    request: InferenceRequest,
    response: InferenceResponse,
    model_path: Path,
    model_id: str,
    resolution_outcome: str,
    deps: LlamaCppInferenceRuntimeDeps,
) -> None:
    """Store a successful inference response in the semantic cache."""
    if response.status != INFERENCE_STATUS_OK or not response.output:
        return

    try:
        cache = deps.get_semantic_cache_fn()()
        if cache is None:
            return
        cache_model_id, cache_context = deps.semantic_cache_identity(
            request,
            model_path=model_path,
            resolved_model_id=model_id,
            resolution_outcome=resolution_outcome,
        )
        cache.put(request.prompt, response.output, model_id=cache_model_id, system_prompt=cache_context)
    except Exception:
        deps.logger.warning("Semantic cache store failed - result will not be cached", exc_info=True)


def _log_speculative_status(adapter: Any, model_id: str, deps: LlamaCppInferenceRuntimeDeps) -> None:
    """Log whether speculative decoding is active for the resolved model."""
    spec_cfg = adapter._get_speculative_config()
    if spec_cfg.enabled:
        cap = deps.detect_speculative_capability(draft_model_id=spec_cfg.draft_model_id)
        if not cap.supported:
            deps.logger.debug(
                "Speculative decoding requested for %s but not supported by"
                " this llama_cpp build (detection_method=%s) - using standard inference",
                model_id,
                cap.detection_method,
            )
        elif cap.has_draft_model:
            deps.logger.debug(
                "Speculative decoding active for %s with draft model %s",
                model_id,
                cap.draft_model_id,
            )
        else:
            deps.logger.debug(
                "Speculative decoding enabled for %s; no draft model configured"
                " - PromptLookupDecoding fallback will be used if available",
                model_id,
            )
    else:
        deps.logger.debug(
            "Speculative decoding disabled for %s - standard inference",
            model_id,
        )


def _response_metadata_from_choices(
    *,
    choices: list[Any],
    template_validation: Any,
    deps: LlamaCppInferenceRuntimeDeps,
) -> dict[str, Any]:
    """Extract response metadata from llama.cpp choices."""
    response_metadata: dict[str, Any] = {}
    if template_validation is not None:
        response_metadata["chat_template_validation"] = deps.chat_template_validation_metadata(template_validation)
    if not choices:
        return response_metadata

    logprobs_data = choices[0].get("logprobs")
    if not logprobs_data or not isinstance(logprobs_data, dict):
        return response_metadata

    token_logprobs = logprobs_data.get("token_logprobs")
    if not token_logprobs or not isinstance(token_logprobs, list):
        return response_metadata

    valid_lps = [lp for lp in token_logprobs if lp is not None]
    if len(valid_lps) < 2:
        return response_metadata

    mean = sum(valid_lps) / len(valid_lps)
    variance = sum((lp - mean) ** 2 for lp in valid_lps) / len(valid_lps)
    response_metadata["logprob_variance"] = round(variance, 4)
    response_metadata["logprob_mean"] = round(mean, 4)
    response_metadata["confidence_calibrated"] = False
    response_metadata["confidence_basis"] = "raw_token_logprobs"
    response_metadata["token_logprobs"] = valid_lps
    return response_metadata


def _llama_cpp_unavailable_response(request: InferenceRequest) -> InferenceResponse:
    """Build the response returned when llama-cpp-python is unavailable."""
    return InferenceResponse(
        model_id=request.model_id,
        output="",
        latency_ms=0,
        tokens_used=0,
        status="error",
        error="llama-cpp-python is not installed",
    )


def _model_not_found_response(request: InferenceRequest) -> InferenceResponse:
    """Build the response returned when no GGUF path resolves."""
    return InferenceResponse(
        model_id=request.model_id,
        output="",
        latency_ms=0,
        tokens_used=0,
        status="error",
        error=f"Model not found: {request.model_id}",
    )


def _account_early_inference_return(item: InferenceResponse) -> InferenceResponse:
    account_evidence_drop(item, "llama_telemetry", logger=logger)
    account_evidence_drop(item, "llama_inference_completion", logger=logger)
    return item


def _ensure_models_discovered(adapter: Any, deps: LlamaCppInferenceRuntimeDeps) -> None:
    """Run lazy model discovery when the adapter has no discovered models."""
    if adapter._discovered_models:
        return
    try:
        adapter.discover_models()
    except Exception:
        deps.logger.warning("Lazy model discovery failed - no models may be available", exc_info=True)


def _load_llama_model(
    adapter: Any,
    *,
    model_id: str,
    model_path: Path,
    start_time: float,
    deps: LlamaCppInferenceRuntimeDeps,
) -> tuple[Any | None, InferenceResponse | None]:
    """Load a llama.cpp model or return the matching error response."""
    try:
        return adapter._get_or_load_model(model_id, model_path), None
    except GGUFIntegrityError as exc:
        logger.warning("Exception handled by  load llama model fallback", exc_info=True)
        latency_ms = int((time.monotonic() - start_time) * 1000)
        deps.logger.warning("Rejected invalid GGUF file for %s: %s", model_id, exc)
        return None, InferenceResponse(
            model_id=model_id,
            output="",
            latency_ms=latency_ms,
            tokens_used=0,
            status="error",
            error=f"GGUF integrity validation failed: {exc}",
        )
    except Exception:
        logger.warning("Exception handled by  load llama model fallback", exc_info=True)
        latency_ms = int((time.monotonic() - start_time) * 1000)
        deps.logger.exception("Failed to load model %s", model_id)
        return None, InferenceResponse(
            model_id=model_id,
            output="",
            latency_ms=latency_ms,
            tokens_used=0,
            status="error",
            error="Model load failed - check server logs for details",
        )


def _untrusted_template_response(
    *,
    model_id: str,
    template_validation: Any,
    start_time: float,
    deps: LlamaCppInferenceRuntimeDeps,
) -> InferenceResponse | None:
    """Return an error response when loaded chat-template validation is unsafe."""
    if template_validation is None or (template_validation.is_trusted and not template_validation.fallback_used):
        return None
    latency_ms = int((time.monotonic() - start_time) * 1000)
    deps.logger.warning(
        "Rejecting inference for %s due to untrusted embedded chat template",
        model_id,
    )
    return InferenceResponse(
        model_id=model_id,
        output="",
        latency_ms=latency_ms,
        tokens_used=0,
        status="error",
        error="Untrusted GGUF chat template rejected",
        metadata={"chat_template_validation": deps.chat_template_validation_metadata(template_validation)},
    )


def _run_chat_completion(
    adapter: Any,
    *,
    request: InferenceRequest,
    model_id: str,
    llm: Any,
    messages: list[dict[str, str]],
    template_validation: Any,
    kv_restored: bool,
    start_time: float,
    deps: LlamaCppInferenceRuntimeDeps,
) -> InferenceResponse:
    """Execute llama.cpp chat completion and update post-success caches."""
    try:
        result = llm.create_chat_completion(**adapter._build_completion_kwargs(request, messages))
        choices = result.get("choices") or []
        output = choices[0]["message"]["content"] if choices else ""
        usage = result.get("usage", {})
        tokens_used = usage.get("total_tokens", 0)
        response = InferenceResponse(
            model_id=model_id,
            output=output,
            latency_ms=int((time.monotonic() - start_time) * 1000),
            tokens_used=tokens_used,
            status=INFERENCE_STATUS_OK,
            metadata=_response_metadata_from_choices(
                choices=choices,
                template_validation=template_validation,
                deps=deps,
            ),
        )
        _update_kv_vram_tracking(adapter=adapter, model_id=model_id, tokens_used=tokens_used, deps=deps)
        if not kv_restored:
            _save_kv_state(request=request, model_id=model_id, llm=llm, deps=deps)
        return response
    except Exception:
        logger.warning("Exception handled by  run chat completion fallback", exc_info=True)
        latency_ms = int((time.monotonic() - start_time) * 1000)
        deps.logger.exception("Inference failed for model %s", model_id)
        return InferenceResponse(
            model_id=model_id,
            output="",
            latency_ms=latency_ms,
            tokens_used=0,
            status="error",
            error="Inference failed - check server logs for details",
        )


def _finalize_inference(
    adapter: Any,
    *,
    request: InferenceRequest,
    response: InferenceResponse,
    model_path: Path,
    model_id: str,
    resolution_outcome: Any,
    deps: LlamaCppInferenceRuntimeDeps,
) -> None:
    """Record telemetry, draft acceptance, and semantic-cache writes."""
    adapter._record_telemetry(request, response)
    if response.status == INFERENCE_STATUS_OK:
        adapter._record_draft_acceptance(request, response, model_id)
    _semantic_cache_store(
        request=request,
        response=response,
        model_path=model_path,
        model_id=model_id,
        resolution_outcome=resolution_outcome,
        deps=deps,
    )


def run_inference(
    adapter: Any,
    request: InferenceRequest,
    deps: LlamaCppInferenceRuntimeDeps,
) -> InferenceResponse:
    """Run non-streaming inference for ``LlamaCppProviderAdapter``.

    Args:
        adapter: Provider adapter facade instance.
        request: Inference request with model_id, prompt, and sampling params.
        deps: Runtime dependencies sourced from the compatibility facade.

    Returns:
        Inference response with output text, latency, token usage, and metadata.
    """
    adapter._emit_inference_started(request)
    start_time = time.monotonic()
    if not deps.llama_cpp_available:
        return _account_early_inference_return(_llama_cpp_unavailable_response(request))
    _ensure_models_discovered(adapter, deps)
    model_path, resolution_outcome = adapter._resolve_model_path_with_outcome(request.model_id)
    if model_path is None:
        return _account_early_inference_return(_model_not_found_response(request))
    model_id = deps.model_id_from_path(model_path)
    request = _apply_profiled_temperature(request=request, model_id=model_id, model_path=model_path, deps=deps)
    cached_response = _semantic_cache_lookup(
        request=request,
        model_path=model_path,
        model_id=model_id,
        resolution_outcome=resolution_outcome,
        deps=deps,
    )
    if cached_response is not None:
        return _account_early_inference_return(cached_response)
    llm, load_error = _load_llama_model(
        adapter, model_id=model_id, model_path=model_path, start_time=start_time, deps=deps
    )
    if load_error is not None:
        return _account_early_inference_return(load_error)
    template_validation = deps.validate_loaded_chat_template(model_id, llm)
    template_error = _untrusted_template_response(
        model_id=model_id,
        template_validation=template_validation,
        start_time=start_time,
        deps=deps,
    )
    if template_error is not None:
        return _account_early_inference_return(template_error)
    kv_restored = _restore_kv_state(request=request, model_id=model_id, llm=llm, deps=deps)
    messages = _build_messages(request)
    # Preserve the existing double-application behavior after model load.
    request = _apply_profiled_temperature(request=request, model_id=model_id, model_path=model_path, deps=deps)
    _log_speculative_status(adapter, model_id, deps)
    response = _run_chat_completion(
        adapter,
        request=request,
        model_id=model_id,
        llm=llm,
        messages=messages,
        template_validation=template_validation,
        kv_restored=kv_restored,
        start_time=start_time,
        deps=deps,
    )
    _finalize_inference(
        adapter,
        request=request,
        response=response,
        model_path=model_path,
        model_id=model_id,
        resolution_outcome=resolution_outcome,
        deps=deps,
    )
    return response
