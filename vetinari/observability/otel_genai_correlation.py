"""Context-local GenAI span correlation helpers."""

from __future__ import annotations

import time
from contextvars import ContextVar
from hashlib import sha256
from typing import Any

from vetinari.logging_context import get_correlation_ids

_RECENT_SPAN_CORRELATION_TTL_SECONDS = 5.0
_recent_span_trace_id_var: ContextVar[str | None] = ContextVar("recent_genai_span_trace_id", default=None)
_recent_span_id_var: ContextVar[str | None] = ContextVar("recent_genai_span_id", default=None)
_recent_span_recorded_at_var: ContextVar[float | None] = ContextVar("recent_genai_span_recorded_at", default=None)


def _remember_recent_span_correlation(span: Any) -> None:
    """Store the just-ended span correlation for adjacent cost telemetry."""
    _recent_span_trace_id_var.set(span.trace_id)
    _recent_span_id_var.set(span.span_id)
    _recent_span_recorded_at_var.set(time.monotonic())


def _clear_recent_span_correlation() -> None:
    """Clear the just-ended GenAI span correlation for this task/thread."""
    _recent_span_trace_id_var.set(None)
    _recent_span_id_var.set(None)
    _recent_span_recorded_at_var.set(None)


def _pop_recent_span_correlation() -> dict[str, str | None]:
    """Return active or just-ended GenAI correlation and consume recent state."""
    active_ids = get_correlation_ids()
    if active_ids.get("trace_id") and active_ids.get("span_id"):
        return {"trace_id": active_ids["trace_id"], "span_id": active_ids["span_id"]}

    recorded_at = _recent_span_recorded_at_var.get()
    if recorded_at is None:
        return {"trace_id": None, "span_id": None}
    age_seconds = time.monotonic() - recorded_at
    if age_seconds > _RECENT_SPAN_CORRELATION_TTL_SECONDS:
        _clear_recent_span_correlation()
        return {"trace_id": None, "span_id": None}

    trace_id = _recent_span_trace_id_var.get()
    span_id = _recent_span_id_var.get()
    _clear_recent_span_correlation()
    return {"trace_id": trace_id, "span_id": span_id}


def _span_correlation_attributes(correlation_ids: dict[str, str | None]) -> dict[str, str]:
    """Return export-safe correlation attributes for a span."""
    attrs: dict[str, str] = {}
    for key in ("trace_id", "span_id"):
        value = correlation_ids.get(key)
        if value is not None:
            attrs[key] = value
    for key in ("request_id", "plan_id"):
        value = correlation_ids.get(key)
        if value is not None:
            attrs[f"{key}_hash"] = sha256(value.encode("utf-8")).hexdigest()[:16]
    return attrs
