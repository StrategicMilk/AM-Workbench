"""Lazy telemetry facades used by provider adapters."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vetinari.constants import INFERENCE_STATUS_ERROR, INFERENCE_STATUS_OK

_log_event_fn: Any = None
_genai_tracer_fn: Any = None
_telemetry_collector_fn: Any = None
_record_model_call_failure_fn: Any = None
_cost_tracker_fn: Any = None
_sla_tracker_fn: Any = None
_forecaster_fn: Any = None
_anomaly_detector_fn: Any = None
_lazy_import_lock = threading.RLock()
DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
BASE_TELEMETRY_WORKFLOW_GUARDS: tuple[str, ...] = (
    "telemetry facades import lazily under a reentrant lock",
    "cost tracker cache is cleared after provider failure",
    "failure metric labels collapse free-text errors to stable classes",
    "best-effort telemetry logs warnings without blocking inference responses",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return adapter telemetry workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/adapters/base_telemetry.py",
        "guards": BASE_TELEMETRY_WORKFLOW_GUARDS,
    }


def log_event(*args: Any, **kwargs: Any) -> Any:
    """Call the structured logging facade with a cached import.

    Returns:
        The wrapped logging function result.
    """
    global _log_event_fn
    if _log_event_fn is None:
        with _lazy_import_lock:
            if _log_event_fn is None:
                from vetinari.structured_logging import log_event as imported

                _log_event_fn = imported
    return _log_event_fn(*args, **kwargs)


def get_genai_tracer() -> Any:
    """Return the GenAI tracer through a cached import.

    Returns:
        The active GenAI tracer object.
    """
    global _genai_tracer_fn
    if _genai_tracer_fn is None:
        with _lazy_import_lock:
            if _genai_tracer_fn is None:
                from vetinari.observability.otel_genai import get_genai_tracer as imported

                _genai_tracer_fn = imported
    return _genai_tracer_fn()


def get_telemetry_collector() -> Any:
    """Return the telemetry collector through a cached import.

    Returns:
        The active telemetry collector object.
    """
    global _telemetry_collector_fn
    if _telemetry_collector_fn is None:
        with _lazy_import_lock:
            if _telemetry_collector_fn is None:
                from vetinari.telemetry import get_telemetry_collector as imported

                _telemetry_collector_fn = imported
    return _telemetry_collector_fn()


def record_model_call_failure(*args: Any, **kwargs: Any) -> Any:
    """Record a model-call failure through a cached metrics import.

    Returns:
        The wrapped metrics function result.
    """
    global _record_model_call_failure_fn
    if _record_model_call_failure_fn is None:
        with _lazy_import_lock:
            if _record_model_call_failure_fn is None:
                from vetinari.metrics import record_model_call_failure as imported

                _record_model_call_failure_fn = imported
    return _record_model_call_failure_fn(*args, **kwargs)


def get_cost_tracker() -> Any:
    """Return the cost tracker through a cached import.

    Returns:
        The active cost tracker object.

    Raises:
        Exception: Propagates failures from the configured cost tracker
            provider after clearing the cached provider reference.
    """
    global _cost_tracker_fn
    if _cost_tracker_fn is None:
        with _lazy_import_lock:
            if _cost_tracker_fn is None:
                from vetinari.analytics.cost import get_cost_tracker as imported

                _cost_tracker_fn = imported
    try:
        return _cost_tracker_fn()
    except Exception:
        with _lazy_import_lock:
            _cost_tracker_fn = None
        raise


def get_sla_tracker() -> Any:
    """Return the SLA tracker through a cached import.

    Returns:
        The active SLA tracker object.
    """
    global _sla_tracker_fn
    if _sla_tracker_fn is None:
        with _lazy_import_lock:
            if _sla_tracker_fn is None:
                from vetinari.analytics.sla import get_sla_tracker as imported

                _sla_tracker_fn = imported
    return _sla_tracker_fn()


def get_forecaster() -> Any:
    """Return the forecaster through a cached import.

    Returns:
        The active forecaster object.
    """
    global _forecaster_fn
    if _forecaster_fn is None:
        with _lazy_import_lock:
            if _forecaster_fn is None:
                from vetinari.analytics.forecasting import get_forecaster as imported

                _forecaster_fn = imported
    return _forecaster_fn()


def get_anomaly_detector() -> Any:
    """Return the anomaly detector through a cached import.

    Returns:
        The active anomaly detector object.
    """
    global _anomaly_detector_fn
    if _anomaly_detector_fn is None:
        with _lazy_import_lock:
            if _anomaly_detector_fn is None:
                from vetinari.analytics.anomaly import get_anomaly_detector as imported

                _anomaly_detector_fn = imported
    return _anomaly_detector_fn()


def _exact_response_tokens(response: Any, logger: logging.Logger) -> tuple[int, int]:
    """Return engine-provided counts or a visible zero-cost fail-closed pair."""
    input_tokens = getattr(response, "input_tokens", None)
    output_tokens = getattr(response, "output_tokens", None)
    if input_tokens is None or output_tokens is None:
        logger.warning("Inference response omitted exact input/output token counts; recording zero cost")
        return 0, 0
    return max(0, int(input_tokens)), max(0, int(output_tokens))


@dataclass(frozen=True, slots=True)
class AdapterCostEntry:
    """Adapter-local cost entry shape compatible with the analytics tracker."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    agent: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    timestamp: float | None = None
    cost_usd: float | None = None
    latency_ms: float = 0.0

    def __repr__(self) -> str:
        return f"AdapterCostEntry(provider={self.provider!r}, model={self.model!r}, tokens={self.input_tokens + self.output_tokens!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the analytics-compatible dict representation."""
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "agent": self.agent,
            "task_id": self.task_id,
            "project_id": self.project_id,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "timestamp": self.timestamp,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
        }


def _record_genai_span(request: Any, response: Any, logger: logging.Logger) -> None:
    """Record the OpenTelemetry GenAI span for an inference response."""
    try:
        genai_tracer = get_genai_tracer()
        llm_span = genai_tracer.start_agent_span(
            agent_name="llm",
            operation="inference",
            model=request.model_id,
        )
        llm_span.attributes["latency_ms"] = response.latency_ms
        llm_span.attributes["gen_ai.usage.input_tokens"] = getattr(response, "input_tokens", 0)
        llm_span.attributes["gen_ai.usage.output_tokens"] = getattr(response, "output_tokens", 0) or 0
        llm_span.attributes["gen_ai.response.model"] = getattr(response, "model_id", request.model_id)
        genai_tracer.end_agent_span(
            llm_span,
            status=INFERENCE_STATUS_OK if response.status == INFERENCE_STATUS_OK else INFERENCE_STATUS_ERROR,
            tokens_used=response.tokens_used,
        )
    except Exception:
        logger.warning("GenAI tracer unavailable for LLM inference span", exc_info=True)


def _record_structured_event(request: Any, response: Any, logger: logging.Logger) -> None:
    """Emit the structured inference_completed event."""
    try:
        input_tokens, output_tokens = _exact_response_tokens(response, logger)
        log_event(
            "info" if response.status == INFERENCE_STATUS_OK else "warning",
            "vetinari.adapters.base",
            "inference_completed",
            model_id=request.model_id,
            latency_ms=response.latency_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            status="completed" if response.status == INFERENCE_STATUS_OK else "failed",
        )
    except Exception:
        logger.warning("Failed to emit inference_completed structured event for %s", request.model_id, exc_info=True)


def _record_collector_latency(provider: str, request: Any, response: Any, logger: logging.Logger) -> None:
    """Record adapter latency in the telemetry collector."""
    try:
        get_telemetry_collector().record_adapter_latency(
            provider=provider,
            model=request.model_id,
            latency_ms=response.latency_ms,
            tokens_used=response.tokens_used,
            success=response.status == INFERENCE_STATUS_OK,
        )
    except Exception:
        logger.warning("Failed to record adapter telemetry for %s", request.model_id, exc_info=True)


_FAILURE_CLASS_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("timeout", ("timed out", "timeout", "deadline")),
    ("rate_limit", ("rate limit", "rate-limit", "too many requests", "429")),
    ("auth", ("unauthorized", "forbidden", "invalid api key", "401", "403")),
    ("not_found", ("not found", "404")),
    ("connection", ("connection refused", "connection reset", "network unreachable")),
    ("server_error", ("internal server error", "bad gateway", "service unavailable", "502", "503")),
    ("validation", ("validation error", "bad request", "invalid argument", "400")),
)


def stable_failure_class(message: str | None, fallback: str) -> str:
    """Reduce a free-text failure message to a stable metric tag.

    Production metric labels must come from a bounded set so dashboards do not
    explode into one bucket per unique error message.  This helper inspects
    ``message`` for known operational classes (timeout, rate limit, auth, etc.)
    and returns a coarse label suitable for metric tagging.  When no class
    matches, it returns ``fallback`` so callers can still distinguish the
    response status without leaking unbounded label cardinality.

    Args:
        message: Free-text failure description, typically from
            ``InferenceResponse.error`` or an exception ``str``.
        fallback: Value to return when no known class matches; usually a coarse
            status like ``"error"`` or ``"failed"``.

    Returns:
        One of the known class tags or ``fallback``.
    """
    if not isinstance(message, str) or not message.strip():
        return fallback
    lowered = message.lower()
    for class_tag, keywords in _FAILURE_CLASS_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return class_tag
    return fallback


def _record_failure_metric(
    request: Any,
    response: Any,
    logger: logging.Logger,
    record_failure_metric: Callable[..., None],
) -> None:
    """Record the model-call failure metric for failed inference responses."""
    if response.status == INFERENCE_STATUS_OK:
        return
    try:
        metadata = request.metadata or {}
        record_failure_metric(
            project_id=str(metadata.get("project_id") or "unknown"),
            task_id=str(metadata.get("task_id") or "unknown"),
            agent_type=str(metadata.get("agent_type") or metadata.get("agent") or "unknown"),
            model_id=request.model_id,
            failure_class=stable_failure_class(response.error, str(response.status or "failed")),
        )
    except Exception:
        logger.warning("Failed to record model failure metric for %s", request.model_id, exc_info=True)


def _record_cost_entry(provider: str, request: Any, response: Any, logger: logging.Logger) -> None:
    """Record cost attribution for an inference response."""
    try:
        input_tokens, output_tokens = _exact_response_tokens(response, logger)
        metadata = request.metadata or {}
        entry = AdapterCostEntry(
            provider=provider,
            model=request.model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            agent=metadata.get("agent"),
            task_id=metadata.get("task_id"),
            latency_ms=float(response.latency_ms),
        )
        get_cost_tracker().record(entry)
    except Exception:
        logger.warning("Failed to record cost tracking entry for %s", request.model_id, exc_info=True)


def _record_sla_metrics(provider: str, request: Any, response: Any, logger: logging.Logger) -> None:
    """Record SLA latency and success counters."""
    try:
        tracker = get_sla_tracker()
        tracker.record_latency(
            f"{provider}:{request.model_id}",
            latency_ms=float(response.latency_ms),
            success=response.status == INFERENCE_STATUS_OK,
        )
        tracker.record_request(success=response.status == INFERENCE_STATUS_OK)
    except Exception:
        logger.warning("Failed to record SLA metrics for %s", request.model_id, exc_info=True)


def _record_forecaster_metrics(request: Any, response: Any, logger: logging.Logger) -> None:
    """Feed latency and token observations into the forecaster."""
    try:
        forecaster = get_forecaster()
        forecaster.ingest("adapter.latency", float(response.latency_ms))
        forecaster.ingest("adapter.tokens", float(response.tokens_used or 0))
    except Exception:
        logger.warning("Failed to ingest forecaster data for %s", request.model_id, exc_info=True)


def _record_anomaly_metrics(request: Any, response: Any, logger: logging.Logger) -> None:
    """Run latency anomaly detection and log detected anomalies."""
    try:
        result = get_anomaly_detector().detect("adapter.latency", float(response.latency_ms))
        if result.is_anomaly:
            logger.warning(
                "Anomaly detected: %s=%s (%s, score=%.2f)",
                result.metric,
                result.value,
                result.method,
                result.score,
            )
    except Exception:
        logger.warning("Failed to run anomaly detection for %s", request.model_id, exc_info=True)


def _record_inference_telemetry(
    *,
    provider: str,
    request: Any,
    response: Any,
    logger: logging.Logger,
    record_failure_metric: Callable[..., None],
) -> None:
    """Record best-effort inference telemetry across analytics integrations."""
    _record_genai_span(request, response, logger)
    _record_structured_event(request, response, logger)
    _record_collector_latency(provider, request, response, logger)
    _record_failure_metric(request, response, logger, record_failure_metric)
    _record_cost_entry(provider, request, response, logger)
    _record_sla_metrics(provider, request, response, logger)
    _record_forecaster_metrics(request, response, logger)
    _record_anomaly_metrics(request, response, logger)
