"""OpenTelemetry GenAI Semantic Conventions tracer (P10.1).

Implements the OpenTelemetry GenAI semantic conventions for agent spans,
tool calls, and token accounting.  Works without the OTEL SDK installed -
falls back to in-process recording with JSON export.

Standard attribute names follow the GenAI semantic conventions draft spec:
  https://opentelemetry.io/docs/specs/semconv/gen-ai/
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vetinari.logging_context import (
    clear_span_id,
    clear_trace_id,
    get_correlation_ids,
    get_trace_id,
    set_span_id,
    set_trace_id,
)
from vetinari.safety.guardrails import redact_pii

from .otel_genai_backend import (
    _get_backend,
    _init_backend_from_env,
    configure_backend,
    flush_file_backend,
    get_active_backend,
)
from .otel_genai_correlation import (
    _clear_recent_span_correlation,
    _pop_recent_span_correlation,
    _remember_recent_span_correlation,
    _span_correlation_attributes,
)
from .otel_genai_io import _GenAITraceIOMixin

logger = logging.getLogger(__name__)
__all__ = [
    "GenAITracer",
    "SpanContext",
    "_clear_recent_span_correlation",
    "_pop_recent_span_correlation",
    "_record_span_cost",
    "configure_backend",
    "flush_file_backend",
    "get_active_backend",
    "get_genai_tracer",
    "reset_genai_tracer",
]


# -- Optional OpenTelemetry import --------------------------------------------

_OTEL_AVAILABLE: bool | None = None
_otel_trace: Any = None

_backend_initialized = False
_backend_init_lock = threading.Lock()


def _get_otel_trace() -> Any | None:
    global _OTEL_AVAILABLE, _otel_trace
    if _OTEL_AVAILABLE is not None:
        return _otel_trace if _OTEL_AVAILABLE else None
    try:
        from importlib import import_module

        _otel_trace = import_module("opentelemetry.trace")
        _OTEL_AVAILABLE = True
        logger.debug("opentelemetry available - GenAI tracer will emit real spans")
    except ImportError:
        _otel_trace = None
        _OTEL_AVAILABLE = False
        logger.debug("opentelemetry not installed - using in-process GenAI tracer")
    return _otel_trace


def _ensure_backend_initialized() -> None:
    global _backend_initialized
    if _backend_initialized:
        return
    with _backend_init_lock:
        if _get_backend() != "noop":
            _backend_initialized = True
            return
        if not _backend_initialized:
            _init_backend_from_env()
            _backend_initialized = True


# OTel tracer name following GenAI semantic conventions
_OTEL_TRACER_NAME = "vetinari.genai"

# -- GenAI attribute name constants -------------------------------------------

ATTR_AGENT_NAME = "gen_ai.agent.name"
ATTR_OPERATION = "gen_ai.operation.name"
ATTR_REQUEST_MODEL = "gen_ai.request.model"
ATTR_RESPONSE_MODEL = "gen_ai.response.model"
ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
ATTR_COST = "gen_ai.usage.cost"
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_TOOL_INPUT = "gen_ai.tool.input"
ATTR_TOOL_OUTPUT = "gen_ai.tool.output"
ATTR_SPAN_STATUS = "gen_ai.span.status"
ATTR_SYSTEM = "gen_ai.system"  # Fixed system identifier for this service

# Export redaction hook for GenAI tool payloads.
# Written by: module import and internal tests/configuration that need a custom export sanitizer.
# Read by: GenAITracer.export_traces before JSON serialization.
# Lifecycle: process-wide; callers should assign during setup, before export begins.
# Lock: export reads this once per call; callable reference assignment is atomic in CPython.
_export_redact_fn: Callable[[str], str] | None = redact_pii

# -- SpanContext dataclass ----------------------------------------------------


@dataclass
class SpanContext:
    """Lightweight span record adhering to GenAI semantic conventions.

    Attributes:
        trace_id: Hex trace identifier (32 chars).
        span_id: Hex span identifier (16 chars).
        agent_name: Name of the agent that owns this span.
        operation: Operation name (e.g. ``"chat"``, ``"embeddings"``).
        start_time: Monotonic clock value at span creation.
        attributes: Mutable attribute bag keyed by GenAI convention names.
        events: Ordered list of event dicts recorded on this span.
        parent_span_id: Span ID of the parent span, enabling hierarchical nesting
            (pipeline > agent > llm).  ``None`` for root spans.
        _end_time: Set when the span is closed; ``None`` while active.
    """

    trace_id: str
    span_id: str
    agent_name: str
    operation: str
    start_time: float
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    parent_span_id: str | None = field(default=None)
    _end_time: float | None = field(default=None, repr=False)
    _otel_span: Any | None = field(default=None, repr=False)
    _trace_token: Any | None = field(default=None, repr=False)
    _span_token: Any | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"SpanContext(trace_id={self.trace_id!r},"
            f" span_id={self.span_id!r},"
            f" agent_name={self.agent_name!r}, operation={self.operation!r})"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _close(self, status: str, tokens_used: int) -> None:
        """Finalise this span (called by GenAITracer.end_agent_span)."""
        self._end_time = time.monotonic()
        self.attributes[ATTR_SPAN_STATUS] = status
        if tokens_used:
            existing = self.attributes.get(ATTR_OUTPUT_TOKENS, 0)
            self.attributes[ATTR_OUTPUT_TOKENS] = existing + tokens_used

    @property
    def duration_ms(self) -> float:
        """Elapsed wall-clock time in milliseconds.

        Returns:
            Duration from span start to end (or now if still active).
        """
        end = self._end_time if self._end_time is not None else time.monotonic()
        return (end - self.start_time) * 1_000

    @property
    def is_active(self) -> bool:
        """True while the span has not been ended."""
        return self._end_time is None

    def to_dict(self) -> dict[str, Any]:
        """Serialise span to a JSON-compatible dict.

        Returns:
            Dictionary representation suitable for ``json.dumps``.
        """
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "agent_name": self.agent_name,
            "operation": self.operation,
            "start_time": self.start_time,
            "end_time": self._end_time,
            "duration_ms": round(self.duration_ms, 3),
            "attributes": self.attributes,
            "events": self.events,
        }


# -- GenAITracer --------------------------------------------------------------


class GenAITracer(_GenAITraceIOMixin):
    """Singleton tracer that records GenAI semantic-convention spans.

    Instantiate via :func:`get_genai_tracer` - do not construct directly.

    Example::

        tracer = get_genai_tracer()
        span = tracer.start_agent_span("builder", "chat", model="qwen-32b")
        tracer.record_tool_call(span, "code_search", '{"q": "foo"}', '"bar"')
        tracer.end_agent_span(span, status="ok", tokens_used=512)
        tracer.export_traces("/tmp/traces.json")
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._spans: deque[SpanContext] = deque(maxlen=5000)
        self._active: dict[str, SpanContext] = {}  # span_id -> SpanContext
        self._total_tokens: int = 0
        # Note: trace ID is stored per-async-task/thread in logging_context
        # ContextVars rather than on the singleton. This prevents trace ID
        # leakage across concurrent callers.

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_agent_span(
        self,
        agent_name: str,
        operation: str,
        model: str = "",
    ) -> SpanContext:
        """Open a new agent span.

        When OpenTelemetry is available, also starts a real OTel span that
        will be exported via the configured OTel exporter.

        Args:
            agent_name: Logical agent name (e.g. ``"builder"``).
            operation: GenAI operation (e.g. ``"chat"``, ``"embeddings"``).
            model: Request model identifier, if known.

        Returns:
            A :class:`SpanContext` representing the open span.
        """
        span_id = uuid.uuid4().hex[:16]
        attrs: dict[str, Any] = {
            ATTR_AGENT_NAME: agent_name,
            ATTR_OPERATION: operation,
            ATTR_SYSTEM: "vetinari",
        }
        if model:
            attrs[ATTR_REQUEST_MODEL] = model

        # Allocate a new trace ID for this async task / thread if none exists.
        # logging_context is the single source of truth for trace correlation.
        trace_id = get_trace_id()
        trace_token = None
        if trace_id is None:
            trace_id = uuid.uuid4().hex
            trace_token = set_trace_id(trace_id)
        span_token = set_span_id(span_id)

        attrs.update(_span_correlation_attributes(get_correlation_ids()))

        ctx = SpanContext(
            trace_id=trace_id,
            span_id=span_id,
            agent_name=agent_name,
            operation=operation,
            start_time=time.monotonic(),
            attributes=attrs,
            _trace_token=trace_token,
            _span_token=span_token,
        )

        # Bridge to real OTel spans when SDK is available and backend is not noop.
        # Guard on _get_backend() != "noop" so we don't create OTel spans when tracing
        # is disabled - avoids spurious exports to a noop provider.
        _ensure_backend_initialized()
        otel_trace = _get_otel_trace()
        if otel_trace is not None and _get_backend() != "noop":
            tracer = otel_trace.get_tracer(_OTEL_TRACER_NAME)
            # Root agent spans are always root OTel spans - parent propagation
            # is handled by start_child_span, which sets parent_span_id and
            # calls set_span_in_context explicitly.  The dead block that
            # checked ctx.parent_span_id here was removed: SpanContext is
            # created above with no parent_span_id, so the check could never
            # fire.
            otel_span = tracer.start_span(
                f"gen_ai.{operation}",
                context=None,
                attributes={k: str(v) for k, v in attrs.items()},
            )
            ctx._otel_span = otel_span

        with self._lock:
            self._active[span_id] = ctx

        logger.debug(
            "GenAI span started: agent=%s op=%s span_id=%s otel=%s",
            agent_name,
            operation,
            span_id,
            bool(_OTEL_AVAILABLE),
        )
        return ctx

    def end_agent_span(
        self,
        span: SpanContext,
        status: str = "ok",
        tokens_used: int = 0,
    ) -> None:
        """Close a span and move it to the completed list.

        Also ends the corresponding OTel span if one was created.

        Args:
            span: The :class:`SpanContext` returned by :meth:`start_agent_span`.
            status: Completion status - ``"ok"`` or ``"error"``.
            tokens_used: Total tokens consumed (added to output token count).
        """
        if not span.is_active:
            logger.warning("end_agent_span called on already-closed span %s", span.span_id)
            return

        span._close(status=status, tokens_used=tokens_used)

        # End the real OTel span if one was attached
        otel_span = getattr(span, "_otel_span", None)
        if otel_span is not None:
            if tokens_used:
                otel_span.set_attribute(ATTR_OUTPUT_TOKENS, tokens_used)
            otel_span.set_attribute(ATTR_SPAN_STATUS, status)
            otel_span.end()

        _remember_recent_span_correlation(span)

        if span._span_token is not None:
            clear_span_id(span._span_token)
            span._span_token = None
        if span._trace_token is not None:
            clear_trace_id(span._trace_token)
            span._trace_token = None

        with self._lock:
            self._active.pop(span.span_id, None)
            self._spans.append(span)
            self._total_tokens += tokens_used

        logger.debug(
            "GenAI span ended: agent=%s status=%s tokens=%d duration=%.1fms",
            span.agent_name,
            status,
            tokens_used,
            span.duration_ms,
        )

    def _record_span_cost(self, trace_id: str | None, span_id: str | None, cost_usd: float) -> bool:
        """Attach cost metadata to an active or completed in-process span."""
        if not trace_id or not span_id or cost_usd < 0.0:
            return False
        with self._lock:
            span = self._active.get(span_id)
            if span is None:
                span = next((candidate for candidate in reversed(self._spans) if candidate.span_id == span_id), None)
            if span is None or span.trace_id != trace_id:
                return False
            existing = span.attributes.get(ATTR_COST, 0.0)
            try:
                total_cost = float(existing) + cost_usd
            except (TypeError, ValueError):
                total_cost = cost_usd
            span.attributes[ATTR_COST] = round(total_cost, 12)
            otel_span = getattr(span, "_otel_span", None)
            if otel_span is not None:
                otel_span.set_attribute(ATTR_COST, round(total_cost, 12))
            return True

    def start_child_span(
        self,
        parent: SpanContext,
        agent_name: str,
        operation: str,
        model: str = "",
    ) -> SpanContext:
        """Open a new span that is a child of an existing span.

        The child inherits ``trace_id`` from the parent and records the
        parent's ``span_id`` in ``parent_span_id``, enabling hierarchical
        nesting such as ``pipeline > agent > llm``.

        Args:
            parent: The parent :class:`SpanContext` to nest under.
            agent_name: Logical agent name for the child span.
            operation: GenAI operation name for the child span.
            model: Request model identifier, if known.

        Returns:
            A new :class:`SpanContext` linked to the parent.
        """
        span_id = uuid.uuid4().hex[:16]
        attrs: dict[str, Any] = {
            ATTR_AGENT_NAME: agent_name,
            ATTR_OPERATION: operation,
            ATTR_SYSTEM: "vetinari",
        }
        if model:
            attrs[ATTR_REQUEST_MODEL] = model

        trace_token = set_trace_id(parent.trace_id)
        span_token = set_span_id(span_id)
        attrs.update(_span_correlation_attributes(get_correlation_ids()))

        ctx = SpanContext(
            trace_id=parent.trace_id,
            span_id=span_id,
            agent_name=agent_name,
            operation=operation,
            start_time=time.monotonic(),
            attributes=attrs,
            parent_span_id=parent.span_id,
            _trace_token=trace_token,
            _span_token=span_token,
        )

        # Bridge to real OTel spans when SDK is available and backend is not noop.
        # Always propagate the parent OTel span as context so the SDK links
        # child spans under their parent trace rather than starting a new root.
        _ensure_backend_initialized()
        otel_trace = _get_otel_trace()
        if otel_trace is not None and _get_backend() != "noop":
            tracer = otel_trace.get_tracer(_OTEL_TRACER_NAME)
            parent_otel = getattr(parent, "_otel_span", None)
            otel_ctx = otel_trace.set_span_in_context(parent_otel) if parent_otel is not None else None
            otel_span = tracer.start_span(
                f"gen_ai.{operation}",
                context=otel_ctx,
                attributes={k: str(v) for k, v in attrs.items()},
            )
            ctx._otel_span = otel_span

        with self._lock:
            self._active[span_id] = ctx

        logger.debug(
            "GenAI child span started: agent=%s op=%s span_id=%s parent_span_id=%s",
            agent_name,
            operation,
            span_id,
            parent.span_id,
        )
        return ctx


def _record_span_cost(trace_id: str | None, span_id: str | None, cost_usd: float) -> bool:
    """Attach cost metadata to a recorded GenAI span when correlation matches."""
    return get_genai_tracer()._record_span_cost(trace_id, span_id, cost_usd)


_tracer_instance: GenAITracer | None = None
_tracer_lock = threading.Lock()


def get_genai_tracer() -> GenAITracer:
    """Return the process-global :class:`GenAITracer` instance.

    Returns:
        The singleton :class:`GenAITracer`.
    """
    global _tracer_instance
    _ensure_backend_initialized()
    if _tracer_instance is None:
        with _tracer_lock:
            if _tracer_instance is None:
                _tracer_instance = GenAITracer()
    return _tracer_instance


def reset_genai_tracer() -> None:
    """Reset the singleton (intended for testing)."""
    global _tracer_instance
    with _tracer_lock:
        _tracer_instance = None
