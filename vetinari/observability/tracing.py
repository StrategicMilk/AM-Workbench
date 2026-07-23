# SPDX-FileCopyrightText: 2024-2026 Vetinari Contributors
# SPDX-License-Identifier: Apache-2.0
"""Distributed Tracing (C7).

=========================
Optional OpenTelemetry integration with no-op fallback.

Span hierarchy: Pipeline > Stage > Agent > LLM Call.
Attributes: agent_type, mode, task_id, model_id, success, tokens.

When ``opentelemetry`` is installed, real spans are emitted.  Otherwise,
a lightweight no-op tracer records spans to structured logging only.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections import deque
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from vetinari.security.fail_closed import sanitize_untrusted_text
from vetinari.security.redaction import redact_value

logger = logging.getLogger(__name__)

# GDPR Art. 17 PII retention contract for span attributes.
# Any attribute capturing user-identifying information (email, user_id, IP
# address, request body excerpts) MUST be written with the ``pii.`` key
# prefix and is retained for at most TRACE_PII_TTL_DAYS days. The prefix
# lets downstream log-scraping and span-retention policies match and purge
# PII spans within the GDPR-compliant window.
PII_ATTR_PREFIX = "pii."  # GDPR: PII field, retained <=30d
TRACE_PII_TTL_DAYS = int(os.environ.get("VETINARI_TRACE_PII_TTL_DAYS", "30"))  # GDPR Art. 17 retention bound

# Canonical PII attribute keys — write any user-identifying span attribute
# under one of these keys so retention policies catch every PII surface.
PII_ATTR_USER_ID = f"{PII_ATTR_PREFIX}user_id"  # GDPR: PII field, retained <=30d
PII_ATTR_EMAIL = f"{PII_ATTR_PREFIX}email"  # GDPR: PII field, retained <=30d
PII_ATTR_IP = f"{PII_ATTR_PREFIX}ip_address"  # GDPR: PII field, retained <=30d
PII_ATTR_REQUEST_BODY = f"{PII_ATTR_PREFIX}request_body"  # GDPR: PII field, retained <=30d


def set_pii_attribute(span: Any, key: str, value: Any) -> None:
    """Record a PII span attribute under the GDPR Art. 17 pii. namespace.

    Ensures the attribute key carries the ``pii.`` prefix so log-scraping
    retention policies can match and purge PII spans within
    ``TRACE_PII_TTL_DAYS`` days. Callers MUST use this helper for any
    user-identifying value rather than calling ``span.set_attribute``
    directly so retention coverage is consistent.

    Args:
        span: Any object exposing ``set_attribute(key, value)`` — works for
            both ``NoOpSpan`` and OpenTelemetry spans.
        key: Attribute name. ``pii.`` prefix is added if not already present.
        value: Attribute value — any JSON-serialisable type.
    """
    pii_key = key if key.startswith(PII_ATTR_PREFIX) else PII_ATTR_PREFIX + key
    span.set_attribute(pii_key, redact_value({pii_key: value})[pii_key])


# ── Try to import OpenTelemetry ───────────────────────────────────────

_OTEL_AVAILABLE = False
_tracer: Any = None
trace: Any = None
StatusCode: Any = None

try:
    _opentelemetry: Any = import_module("opentelemetry")
    _opentelemetry_trace: Any = import_module("opentelemetry.trace")
    trace = _opentelemetry.trace
    StatusCode = _opentelemetry_trace.StatusCode

    _OTEL_AVAILABLE = True
    _tracer = trace.get_tracer("vetinari", "0.4.0")
    logger.info("OpenTelemetry tracing enabled")
except ImportError:
    logger.debug("OpenTelemetry not installed — using no-op tracer")


# ── No-op span for when OTel is not available ─────────────────────────

MAX_SPAN_EVENTS = 100  # Cap per-span event buffer to prevent unbounded growth


def _safe_span_attributes(attributes: dict[str, Any] | None) -> dict[str, Any]:
    """Return span attributes safe for logs/exporters."""
    safe: dict[str, Any] = {}
    for key, value in (attributes or {}).items():
        safe_key = sanitize_untrusted_text(str(key), max_length=160)
        if isinstance(value, str) and value.strip():
            value = sanitize_untrusted_text(value, max_length=2_000)
        safe[safe_key] = value
    return dict(redact_value(safe))


@dataclass
class NoOpSpan:
    """Lightweight span that logs to structured logging."""

    name: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    trace_id: str | None = None  # Inherited from parent when nested; None for root spans
    parent_id: str | None = None
    _otel_span: Any | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    start_time: float = field(default_factory=time.monotonic)
    end_time: float | None = None
    status: str = "OK"
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_SPAN_EVENTS))

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"NoOpSpan(name={self.name!r}, span_id={self.span_id!r}, status={self.status!r})"

    def set_attribute(self, key: str, value: Any) -> None:
        """Record a key-value attribute on the span for inclusion in structured log output.

        Args:
            key: Attribute name (e.g. ``"agent_type"``, ``"model_id"``).
            value: Attribute value — any JSON-serialisable type.
        """
        self.attributes[key] = redact_value({key: value})[key]

    def set_status(self, status: str, description: str = "") -> None:
        """Mark the span outcome and attach an optional human-readable description.

        The description is stored as a ``status_description`` attribute and appears
        in the DEBUG log emitted by ``end()``.

        Args:
            status: Outcome string, e.g. ``"OK"`` or ``"ERROR"``.
            description: Optional plain-English explanation appended to the span.
        """
        self.status = status
        if description:
            self.attributes["status_description"] = redact_value({"status_description": description})[
                "status_description"
            ]

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        """Append a timestamped event to this span's bounded event buffer.

        The buffer is capped at ``MAX_SPAN_EVENTS``; the oldest entries are dropped
        automatically when the cap is reached (``deque(maxlen=…)`` semantics).

        Args:
            name: Short event label (e.g. ``"cache.hit"``, ``"retry.attempt"``).
            attributes: Optional key-value metadata attached to the event.
        """
        self.events.append(
            {
                "name": name,
                "timestamp": time.monotonic(),
                "attributes": _safe_span_attributes(attributes),
            },
        )

    def end(self) -> None:
        """Close the span, recording end time and emitting a DEBUG log with duration and attributes."""
        self.end_time = time.monotonic()
        duration_ms = (self.end_time - self.start_time) * 1000
        logger.debug(
            "Span[%s] %s duration=%.1fms status=%s attrs=%s",
            self.span_id[:8],
            self.name,
            duration_ms,
            self.status,
            {k: v for k, v in self.attributes.items() if k != "status_description"},
        )


# ── Span context manager ─────────────────────────────────────────────


@contextmanager
def start_span(
    name: str,
    attributes: dict[str, Any] | None = None,
    parent: Any | None = None,
) -> Generator:
    """Start a tracing span.

    Uses OpenTelemetry if available, otherwise uses NoOpSpan with logging.

    Usage::

        with start_span("agent.execute", {"agent_type": AgentType.WORKER.value}) as span:
            span.set_attribute("model_id", "qwen-32b")
            result = agent.execute(task)
            span.set_attribute("success", result.success)

    Args:
        name: The name.
        attributes: The attributes.
        parent: The parent.

    Raises:
        Exception: Re-raises any exception raised inside the span body after recording it on the span.
    """
    name = sanitize_untrusted_text(name, max_length=200)
    safe_attributes = _safe_span_attributes(attributes)
    if _OTEL_AVAILABLE and _tracer:
        # Extract OTel context from parent span so this span is nested under it
        # rather than starting a new root trace. Without this, every call to
        # start_span() with a parent argument was ignoring the parent on the
        # OTel path, causing spans to appear as unrelated top-level traces.
        otel_ctx = None
        if parent is not None and hasattr(parent, "_otel_span"):
            otel_ctx = trace.set_span_in_context(parent._otel_span)

        with _tracer.start_as_current_span(name, context=otel_ctx, attributes=safe_attributes) as otel_span:
            if safe_attributes:
                for k, v in safe_attributes.items():
                    otel_span.set_attribute(k, v)
            try:
                yield otel_span
            except Exception as e:
                otel_span.set_status(StatusCode.ERROR, str(e))
                otel_span.record_exception(e)
                raise
    else:
        # Propagate parent lineage so child spans share the same trace_id
        # and record their parent_id — enables hierarchical span trees even
        # when the OTel SDK is not installed.
        parent_span_id = parent.span_id if parent is not None else None
        parent_trace_id = getattr(parent, "trace_id", None) if parent is not None else None
        span = NoOpSpan(
            name=name,
            attributes=safe_attributes,
            parent_id=parent_span_id,
            trace_id=parent_trace_id,
        )
        try:
            yield span
        except Exception as e:
            span.set_status("ERROR", str(e))
            raise
        finally:
            span.end()


# ── Convenience functions for common span types ───────────────────────


@contextmanager
def pipeline_span(goal: str, plan_id: str = "") -> Generator:
    """Top-level pipeline span.

    Args:
        goal: The goal.
        plan_id: The plan id.
    """
    with start_span(
        "vetinari.pipeline",
        {
            "goal": sanitize_untrusted_text(goal, max_length=200),
            "plan_id": sanitize_untrusted_text(plan_id or "none", max_length=120),
        },
    ) as span:
        yield span


@contextmanager
def stage_span(stage_name: str, stage_number: int = 0) -> Generator:
    """Pipeline stage span.

    Args:
        stage_name: The stage name.
        stage_number: The stage number.
    """
    with start_span(
        f"vetinari.stage.{stage_name}",
        {
            "stage.name": sanitize_untrusted_text(stage_name, max_length=120),
            "stage.number": stage_number,
        },
    ) as span:
        yield span


@contextmanager
def agent_span(
    agent_type: str,
    mode: str = "",
    task_id: str = "",
) -> Generator:
    """Agent execution span.

    Args:
        agent_type: The agent type.
        mode: The mode.
        task_id: The task id.
    """
    with start_span(
        "vetinari.agent.execute",
        {
            "agent.type": sanitize_untrusted_text(agent_type, max_length=120),
            "agent.mode": sanitize_untrusted_text(mode or "default", max_length=120),
            "task.id": sanitize_untrusted_text(task_id or "none", max_length=160),
        },
    ) as span:
        yield span


@contextmanager
def llm_span(
    model_id: str,
    agent_type: str = "",
    max_tokens: int = 0,
) -> Generator:
    """LLM inference call span.

    Args:
        model_id: The model id.
        agent_type: The agent type.
        max_tokens: The max tokens.
    """
    with start_span(
        "vetinari.llm.infer",
        {
            "llm.model_id": sanitize_untrusted_text(model_id, max_length=200),
            "llm.max_tokens": max_tokens,
            "agent.type": sanitize_untrusted_text(agent_type or "unknown", max_length=120),
        },
    ) as span:
        yield span


@contextmanager
def user_span(user_id: str, request_id: str = "") -> Generator:
    """User request span with PII attributes recorded under the ``pii.`` prefix.

    Wraps the request in an OpenTelemetry-compatible span and records
    ``user_id`` and ``request_id`` using :func:`set_pii_attribute` so that
    PII access is consistently routed through the enforced attribute-naming
    scheme (``pii.<key>``). The span is a no-op when OpenTelemetry is
    unavailable.

    Args:
        user_id: Authenticated user identifier (treated as PII).
        request_id: Optional correlation ID for the HTTP request.
    """
    with start_span(
        "vetinari.user.request",
        {
            "request.id": sanitize_untrusted_text(request_id or "none", max_length=160),
        },
    ) as span:
        set_pii_attribute(span, "user_id", user_id)
        if request_id:
            set_pii_attribute(span, "request_id", request_id)
        yield span


def is_otel_available() -> bool:
    """Check if OpenTelemetry is available."""
    return _OTEL_AVAILABLE


# ── VetinariInstrumentor ──────────────────────────────────────────────

_instrumented: bool = False  # Module-level flag — tracks active instrumentation state


class VetinariInstrumentor:
    """OpenTelemetry instrumentor for the Vetinari pipeline.

    Activates or deactivates span emission for pipeline stages, model calls,
    and tool calls.  When OpenTelemetry is not installed, ``instrument()``
    succeeds silently — the pipeline falls back to ``NoOpSpan`` logging and
    local development is not broken.

    Langfuse compatibility: Langfuse provides an OTLP exporter.  Pass the
    configured ``OTLPSpanExporter`` (or ``LangfuseExporter``) to
    ``instrument(exporter=...)`` and it will be registered with the SDK's
    ``BatchSpanProcessor``.  When ``exporter`` is ``None``, spans are emitted
    to whatever exporter is already registered on the global tracer provider
    (useful in test environments where the provider is pre-configured).

    Usage::

        # Enable with no exporter (uses global provider / NoOp fallback):
        VetinariInstrumentor().instrument()

        # Enable with a Langfuse exporter:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter(endpoint="https://cloud.langfuse.com/api/public/otel/v1/traces")
        VetinariInstrumentor().instrument(exporter=exporter)

        # Disable (e.g. in teardown or test isolation):
        VetinariInstrumentor().uninstrument()

    Attributes:
        _exporter_registered: Whether this instance registered a span processor.
    """

    def __init__(self) -> None:
        """Initialise the instrumentor (no side effects)."""
        self._exporter_registered: bool = False

    def instrument(self, exporter: Any = None) -> None:
        """Activate Vetinari instrumentation.

        If an ``exporter`` is provided AND the OpenTelemetry SDK is installed,
        registers it as a ``BatchSpanProcessor`` on the tracer provider.  This
        is the integration point for Langfuse or any OTLP-compatible backend.

        When OpenTelemetry is not installed, the call succeeds without error —
        the pipeline continues to use ``NoOpSpan`` for local development.

        Args:
            exporter: An optional OTel ``SpanExporter`` instance.  When
                ``None``, spans are forwarded to the global tracer provider's
                existing processors (or dropped if none are registered).
        """
        global _instrumented

        if _instrumented:
            logger.debug("VetinariInstrumentor.instrument() called but already instrumented — no-op")
            return

        if exporter is not None and _OTEL_AVAILABLE:
            try:
                _otel_trace_sdk: Any = import_module("opentelemetry").trace
                _otel_sdk_trace: Any = import_module("opentelemetry.sdk.trace")
                _otel_sdk_export: Any = import_module("opentelemetry.sdk.trace.export")
                tracer_provider = _otel_sdk_trace.TracerProvider
                batch_span_processor = _otel_sdk_export.BatchSpanProcessor

                provider = tracer_provider()
                provider.add_span_processor(batch_span_processor(exporter))
                _otel_trace_sdk.set_tracer_provider(provider)
                self._exporter_registered = True
                logger.info(
                    "VetinariInstrumentor: registered exporter %s on new TracerProvider",
                    type(exporter).__name__,
                )
            except Exception:
                logger.warning(
                    "VetinariInstrumentor: failed to register exporter — spans will use default provider",
                    exc_info=True,
                )
        elif not _OTEL_AVAILABLE:
            logger.debug("VetinariInstrumentor.instrument(): opentelemetry not installed — NoOpSpan logging active")

        _instrumented = True
        logger.info("VetinariInstrumentor: instrumentation active (otel_available=%s)", _OTEL_AVAILABLE)

    def uninstrument(self) -> None:
        """Deactivate Vetinari instrumentation.

        Clears the instrumented flag so subsequent calls to ``instrument()``
        can re-register.  If a ``TracerProvider`` was created by this
        instrumentor, its ``shutdown()`` is called to flush pending spans
        before the provider is replaced with a no-op.
        """
        global _instrumented

        if not _instrumented:
            logger.debug("VetinariInstrumentor.uninstrument() called but not instrumented — no-op")
            return

        if self._exporter_registered and _OTEL_AVAILABLE:
            try:
                _otel_sdk: Any = import_module("opentelemetry").trace
                _otel_sdk_trace: Any = import_module("opentelemetry.sdk.trace")
                tracer_provider = _otel_sdk_trace.TracerProvider

                current_provider = _otel_sdk.get_tracer_provider()
                if isinstance(current_provider, tracer_provider):
                    current_provider.shutdown()
                    logger.debug("VetinariInstrumentor: TracerProvider shutdown complete")
            except Exception:
                logger.warning(
                    "VetinariInstrumentor: error during provider shutdown — spans may be lost",
                    exc_info=True,
                )
            self._exporter_registered = False

        _instrumented = False
        logger.info("VetinariInstrumentor: instrumentation removed")

    @staticmethod
    def is_instrumented() -> bool:
        """Return whether Vetinari instrumentation is currently active.

        Returns:
            ``True`` if ``instrument()`` has been called and ``uninstrument()``
            has not been called since.
        """
        return _instrumented
