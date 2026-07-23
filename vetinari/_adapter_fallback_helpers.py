"""Stateless inference fallback helpers extracted from AdapterManager.

These module-level functions replace the private static and instance methods
that were growing ``adapter_manager.py`` beyond the project's 550-LOC ceiling.
The AdapterManager still owns all mutable state; helpers receive it as explicit
parameters so the logic stays testable without instantiating the full manager.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from vetinari.constants import INFERENCE_STATUS_ERROR, INFERENCE_STATUS_OK
from vetinari.validation import validate_numeric_signal

if TYPE_CHECKING:
    from collections.abc import Callable

    from vetinari._provider_metrics import ProviderMetrics
    from vetinari.adapters.base import InferenceRequest, InferenceResponse, ProviderAdapter

logger = logging.getLogger(__name__)


def cascade_error_response(request: InferenceRequest, message: str) -> InferenceResponse:
    """Build a fail-closed cascade error InferenceResponse.

    Args:
        request: The original inference request whose model_id is echoed back.
        message: Operator-readable error description attached to the response.

    Returns:
        An InferenceResponse with status=error, zero tokens, and the given message.
    """
    from vetinari.adapters.base import InferenceResponse

    return InferenceResponse(
        model_id=request.model_id,
        output="",
        latency_ms=0,
        tokens_used=0,
        status=INFERENCE_STATUS_ERROR,
        error=message,
    )


def permission_denied_response(request: InferenceRequest) -> InferenceResponse:
    """Build the InferenceResponse returned when model access is permission-denied.

    Args:
        request: The original inference request whose model_id is echoed back.

    Returns:
        An InferenceResponse with status=error and a permission-denied message.
    """
    from vetinari.adapters.base import InferenceResponse

    return InferenceResponse(
        model_id=request.model_id,
        output="",
        latency_ms=0,
        tokens_used=0,
        status=INFERENCE_STATUS_ERROR,
        error="MODEL_INFERENCE permission denied in current context",
    )


def all_providers_failed_response(request: InferenceRequest, last_error: object) -> InferenceResponse:
    """Build the final error InferenceResponse after the provider fallback list is exhausted.

    Args:
        request: The original inference request whose model_id is echoed back.
        last_error: The last error observed during fallback, included in the message.

    Returns:
        An InferenceResponse indicating all providers failed.
    """
    from vetinari.adapters.base import InferenceResponse

    return InferenceResponse(
        model_id=request.model_id,
        output="",
        latency_ms=0,
        tokens_used=0,
        status=INFERENCE_STATUS_ERROR,
        error=f"All providers failed. Last error: {last_error}",
    )


def _metadata_cost(metadata: object) -> float:
    """Return a numeric response cost, treating unreadable metadata as zero."""
    if not isinstance(metadata, dict):
        logger.warning("Inference response metadata was unreadable; treating provider cost as 0.0")
        return 0.0
    try:
        return float(metadata.get("cost", 0.0))
    except (TypeError, ValueError):
        logger.warning("Inference response cost metadata was non-numeric; treating provider cost as 0.0")
        return 0.0


def warn_if_vram_may_exceed_capacity(request: InferenceRequest) -> None:
    """Fail closed when the requested model exceeds available VRAM.

    Args:
        request: The inference request whose model_id is checked against the VRAM manager.

    Raises:
        RuntimeError: If the VRAM manager or its numeric capacity signals are unavailable.
    """
    if not request.model_id:
        return
    try:
        from vetinari.models.vram_manager import get_vram_manager

        vram_manager = get_vram_manager()
        if not vram_manager.can_load(request.model_id):
            max_available = validate_numeric_signal(
                vram_manager.get_max_available_vram_gb(),
                field_name="max_available_vram_gb",
                minimum=0,
                maximum=1024,
                source="vram_manager",
            ).value
            needed = validate_numeric_signal(
                vram_manager.get_model_vram_requirement(request.model_id),
                field_name="model_vram_requirement_gb",
                minimum=0,
                maximum=1024,
                source="vram_manager",
            ).value
            logger.warning(
                "Model %s needs %.1f GB but only %.1f GB available (including evictable)",
                request.model_id,
                needed,
                max_available,
            )
            raise RuntimeError(
                f"VRAM guard rejected {request.model_id}: needs {needed:.1f} GB, available {max_available:.1f} GB"
            )
    except RuntimeError as exc:
        if str(exc).startswith("VRAM guard rejected"):
            raise
        msg = "VRAMManager pre-check unavailable; recover VRAM guard before inference"
        logger.error("%s: %s", msg, exc)
        raise RuntimeError(msg) from exc
    except Exception as exc:
        msg = "VRAMManager pre-check unavailable; recover VRAM guard before inference"
        logger.error("%s: %s", msg, exc)
        raise RuntimeError(msg) from exc


def providers_to_try(
    provider_name: str | None,
    fallback_on_error: bool,
    provider_fallback_order: list[str],
) -> list[str]:
    """Return the ordered provider names for the standard fallback path.

    Args:
        provider_name: Preferred provider name, placed first when given.
        fallback_on_error: Whether to append the full fallback order after the preferred provider.
        provider_fallback_order: Configured fallback sequence from AdapterManager state.

    Returns:
        Ordered list of provider names to attempt inference against.
    """
    result: list[str] = []
    if provider_name:
        result.append(provider_name)
    if fallback_on_error:
        for candidate in provider_fallback_order:
            if candidate not in result:
                result.append(candidate)
    return result


def record_provider_response(
    provider_name: str,
    response: InferenceResponse,
    metrics: dict[str, ProviderMetrics],
    metrics_lock: threading.Lock,
) -> None:
    """Update per-provider metrics after a successful adapter response.

    Args:
        provider_name: Name of the provider that produced the response.
        response: The InferenceResponse returned by the adapter.
        metrics: Live metrics dict keyed by provider name (mutated under lock).
        metrics_lock: Lock that serialises all metric counter updates.
    """
    if provider_name not in metrics:
        return
    m = metrics[provider_name]
    with metrics_lock:
        if response.status == INFERENCE_STATUS_OK:
            previous_successes = m.successful_inferences
            m.successful_inferences += 1
            m.total_tokens_used += response.tokens_used
            if response.latency_ms > 0:
                m.avg_latency_ms = (m.avg_latency_ms * previous_successes + response.latency_ms) / (
                    previous_successes + 1
                )
            m.estimated_cost += _metadata_cost(response.metadata)
        else:
            m.failed_inferences += 1


def record_provider_failure(
    provider_name: str,
    metrics: dict[str, ProviderMetrics],
    metrics_lock: threading.Lock,
) -> None:
    """Increment failure counter for a provider after an adapter exception.

    Args:
        provider_name: Name of the provider that raised an exception.
        metrics: Live metrics dict keyed by provider name (mutated under lock).
        metrics_lock: Lock that serialises all metric counter updates.
    """
    if provider_name in metrics:
        with metrics_lock:
            metrics[provider_name].failed_inferences += 1


def infer_with_provider_fallback(
    request: InferenceRequest,
    providers_to_try_list: list[str],
    fallback_on_error: bool,
    get_provider_fn: Callable[[str], ProviderAdapter | None],
    metrics: dict[str, ProviderMetrics],
    metrics_lock: threading.Lock,
) -> InferenceResponse:
    """Run inference through providers in fallback order until one succeeds.

    Args:
        request: The inference request to execute.
        providers_to_try_list: Ordered list of provider names to attempt.
        fallback_on_error: Whether to continue to the next provider on adapter error.
        get_provider_fn: Callable that returns a ProviderAdapter by name, or None.
        metrics: Live metrics dict keyed by provider name.
        metrics_lock: Lock that serialises all metric counter updates.

    Returns:
        The first successful InferenceResponse, or a final error response when
        all providers in the list have been exhausted.
    """
    if not providers_to_try_list:
        return all_providers_failed_response(request, "No providers configured for fallback inference")

    last_error: Any = None
    for prov_name in providers_to_try_list:
        try:
            adapter = get_provider_fn(prov_name)
        except Exception as exc:
            logger.exception(
                "Provider lookup failed for %s, %s",
                prov_name,
                "stopping fallback" if not fallback_on_error else "trying next provider",
            )
            record_provider_failure(prov_name, metrics, metrics_lock)
            last_error = f"Provider lookup failed for {prov_name}: {exc}"
            if not fallback_on_error:
                break
            continue
        if not adapter:
            last_error = f"Provider {prov_name} unavailable"
            continue

        try:
            logger.info("Attempting inference with %s", prov_name)
            response = adapter.infer(request)
            record_provider_response(prov_name, response, metrics, metrics_lock)

            if response.status == INFERENCE_STATUS_OK:
                return response
            last_error = response.error
            continue
        except Exception:
            logger.exception(
                "Inference failed with %s — incrementing failure counter, %s",
                prov_name,
                "stopping fallback" if not fallback_on_error else "trying next provider",
            )
            record_provider_failure(prov_name, metrics, metrics_lock)
            last_error = "Provider inference failed"
            if not fallback_on_error:
                break
            continue

    return all_providers_failed_response(request, last_error)


__all__ = [
    "all_providers_failed_response",
    "cascade_error_response",
    "infer_with_provider_fallback",
    "permission_denied_response",
    "providers_to_try",
    "record_provider_failure",
    "record_provider_response",
    "warn_if_vram_may_exceed_capacity",
]
