"""OpenTelemetry/OpenInference trace export and import for Workbench."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from importlib import import_module
from importlib.util import find_spec
from typing import Any, Protocol, cast
from urllib.parse import urlparse

import requests

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.security.redaction import REDACTED
from vetinari.security.redaction import is_sensitive_key as _default_redact_keys
from vetinari.security.redaction import redact_text as _default_redact_text
from vetinari.types import AgentType, EvidenceBasis, ShardKind
from vetinari.workbench.metadata_spine import get_workbench_spine
from vetinari.workbench.openinference import (
    OpenInferenceSpanAttributes,
    ScalarAttribute,
    openinference_attributes_to_trace_span,
    workbench_trace_to_openinference_attributes,
)
from vetinari.workbench.runs import WorkbenchRun
from vetinari.workbench.trace_interop_helpers import (
    TRACE_INTEROP_PUBLIC_EXPORTS,
    _attrs_to_jsonable,
    _stable_int,
    _trace_value_shape_equal,
)
from vetinari.workbench.trace_interop_helpers import (
    _coerce_trace_record as _coerce_trace_record_impl,
)
from vetinari.workbench.traces import WorkbenchTrace

logger = logging.getLogger(__name__)


_MAX_ATTRIBUTES_PER_SPAN_DEFAULT: int = 128
_MAX_PAYLOAD_BYTES_DEFAULT: int = 1_048_576
_DEFAULT_TIMEOUT_S: float = 5.0
_RECEIPT_ACTOR: str = "vetinari.workbench.trace_interop"

# Module-level mutable state:
# - Writers: _ensure_exporter publishes cache entries; reset_exporter_cache_for_test clears them.
# - Readers: _ensure_exporter reads before and after acquiring _EXPORTER_LOCK.
# - Lifecycle: process-local cache, reset only by tests.
# - Guard: _EXPORTER_LOCK protects every write and the second read in the DCL path.
_EXPORTER_LOCK: threading.Lock = threading.Lock()
_CACHED_EXPORTER: object | None = None
_CACHED_EXPORTER_KEY: tuple[str, tuple[tuple[str, str], ...], float, int | None] | None = None
_EXPORTER_OWNER_REF: str = _RECEIPT_ACTOR


def _otel_module_available(module_name: str) -> bool:
    try:
        return find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError) as exc:
        logger.warning(
            "OpenTelemetry module discovery failed; expected importable module metadata, observed %s. "
            "Treating the tracing dependency as unavailable; install or repair the OpenTelemetry package "
            "before enabling trace export.",
            type(exc).__name__,
            extra={"otel_module": module_name},
        )
        return False


def _required_otel_modules_available(config: TraceExporterConfig) -> bool:
    module_names = [
        "opentelemetry.sdk.trace.export",
        "opentelemetry.sdk.trace",
        "opentelemetry.trace",
        "opentelemetry.trace.status",
    ]
    if config.session is None:
        module_names.append("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    return all(_otel_module_available(module_name) for module_name in module_names)


def _span_export_succeeded(export_result: object) -> bool:
    span_export_module = import_module("opentelemetry.sdk.trace.export")
    return export_result == span_export_module.SpanExportResult.SUCCESS


class _SpanExporter(Protocol):
    """OpenTelemetry exporter surface used by this module."""

    def export(self, spans: Sequence[object]) -> object:
        """Export spans to the configured telemetry backend."""
        ...


class _TraceSpine(Protocol):
    """Workbench spine append surface used for imported traces."""

    def append_trace(self, *args: object) -> object:
        """Append trace data to the workbench spine."""
        ...


class _SessionOTLPExporter:
    """Small OTLP/HTTP exporter adapter for injected test or custom sessions."""

    def __init__(self, config: TraceExporterConfig) -> None:
        self._endpoint = config.endpoint
        self._headers = dict(config.headers)
        self._timeout = config.timeout_s
        self._session = config.session

    def export(self, spans: Sequence[object]) -> object:
        """Serialize spans, POST them to the configured OTLP endpoint, and return success.

        Returns:
            SpanExportResult.SUCCESS when the backend accepts the batch.
        """
        from opentelemetry.sdk.trace.export import SpanExportResult

        payload = json.dumps([_readable_span_to_jsonable(span) for span in spans], separators=(",", ":"))
        response = self._session.post(
            self._endpoint,
            data=payload,
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        return SpanExportResult.SUCCESS


class TraceExportOutcome(str, Enum):
    """Typed outcome for every trace export attempt."""

    SUCCESS = "success"  # Exporter accepted the span batch.
    TRANSIENT_FAILURE = "transient_failure"  # Retryable network/exporter failure.
    PERMANENT_FAILURE = "permanent_failure"  # Bad config, oversized payload, or unrecoverable error.
    REDACTION_BLOCKED = "redaction_blocked"  # Redaction failed, so no network call was made.


@dataclass(frozen=True, slots=True)
class TraceExportResult:
    """Result of one trace export attempt."""

    outcome: TraceExportOutcome
    spans_exported: int
    endpoint: str | None
    failure_reason: str | None
    receipt_id: str
    attempted_at_utc: str

    def __repr__(self) -> str:
        return f"TraceExportResult(outcome={self.outcome.value!r}, spans_exported={self.spans_exported!r}, endpoint={self.endpoint!r}, receipt_id={self.receipt_id!r})"


@dataclass(frozen=True, slots=True)
class TraceExporterConfig:
    """Configuration for OTLP/OpenInference trace export."""

    endpoint: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    timeout_s: float = _DEFAULT_TIMEOUT_S
    max_attributes_per_span: int = _MAX_ATTRIBUTES_PER_SPAN_DEFAULT
    max_payload_bytes: int = _MAX_PAYLOAD_BYTES_DEFAULT
    redact_text: Callable[[str], str] = _default_redact_text
    redact_keys: Callable[[object], bool] = _default_redact_keys
    session: object | None = None

    def __repr__(self) -> str:
        endpoint_host = urlparse(self.endpoint or "").netloc or None
        return f"TraceExporterConfig(endpoint_host={endpoint_host!r}, timeout_s={self.timeout_s!r}, max_attributes_per_span={self.max_attributes_per_span!r}, max_payload_bytes={self.max_payload_bytes!r})"


class TraceImportError(Exception):
    """Raised when an OTel/OpenInference trace record cannot be imported."""


@dataclass(frozen=True, slots=True)
class RoundTripResult:
    """Result of an export-then-import trace round trip."""

    export_result: TraceExportResult
    imported_trace: WorkbenchTrace | None
    value_shape_equal: bool | None


def reset_exporter_cache_for_test() -> None:
    """Clear the cached OTLP exporter for isolated tests."""
    global _CACHED_EXPORTER, _CACHED_EXPORTER_KEY
    with _EXPORTER_LOCK:
        _CACHED_EXPORTER = None
        _CACHED_EXPORTER_KEY = None


def exporter_cache_lifecycle_owner() -> str:
    """Return the current process-local exporter cache owner reference.

    Returns:
        Owner reference currently allowed to release the exporter cache.
    """
    with _EXPORTER_LOCK:
        return _EXPORTER_OWNER_REF


def claim_exporter_cache_lifecycle(owner_ref: str) -> str:
    """Bind the process-local exporter cache lifecycle to an explicit owner.

    Returns:
        Owner reference now bound to the exporter cache lifecycle.

    Raises:
        ValueError: If the owner reference is empty.
    """
    if not owner_ref.strip():
        raise ValueError("owner_ref must be non-empty")
    global _EXPORTER_OWNER_REF
    with _EXPORTER_LOCK:
        _EXPORTER_OWNER_REF = owner_ref
        return _EXPORTER_OWNER_REF


def release_exporter_cache_lifecycle(owner_ref: str) -> None:
    """Clear the exporter cache only for the owner that claimed it.

    Raises:
        ValueError: If the owner reference is empty or does not own the cache.
    """
    global _CACHED_EXPORTER, _CACHED_EXPORTER_KEY, _EXPORTER_OWNER_REF
    if not owner_ref.strip():
        raise ValueError("owner_ref must be non-empty")
    with _EXPORTER_LOCK:
        if owner_ref != _EXPORTER_OWNER_REF:
            raise ValueError("owner_ref does not own the trace exporter cache")
        _CACHED_EXPORTER = None
        _CACHED_EXPORTER_KEY = None
        _EXPORTER_OWNER_REF = _RECEIPT_ACTOR


def _ensure_exporter(config: TraceExporterConfig) -> object:
    """Return a cached OTLP HTTP exporter for the supplied config."""
    global _CACHED_EXPORTER, _CACHED_EXPORTER_KEY
    key = (
        config.endpoint or "",
        tuple(sorted(config.headers)),
        config.timeout_s,
        id(config.session) if config.session is not None else None,
    )
    if _CACHED_EXPORTER is not None and key == _CACHED_EXPORTER_KEY:
        return _CACHED_EXPORTER
    with _EXPORTER_LOCK:
        if _CACHED_EXPORTER is not None and key == _CACHED_EXPORTER_KEY:
            return _CACHED_EXPORTER
        if config.session is not None:
            _CACHED_EXPORTER = _SessionOTLPExporter(config)
            _CACHED_EXPORTER_KEY = key
            return _CACHED_EXPORTER
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        kwargs: dict[str, Any] = {"endpoint": config.endpoint, "timeout": config.timeout_s}
        if config.headers:
            kwargs["headers"] = dict(config.headers)
        _CACHED_EXPORTER = OTLPSpanExporter(**kwargs)
        _CACHED_EXPORTER_KEY = key
        return _CACHED_EXPORTER


def export_trace_to_otlp(
    trace: WorkbenchTrace,
    run: WorkbenchRun,
    *,
    config: TraceExporterConfig,
) -> TraceExportResult:
    """Export a WorkbenchTrace as redacted OpenInference-attributed OTLP spans.

    Args:
        trace: Workbench trace to export.
        run: Run metadata associated with the trace.
        config: OTLP exporter configuration.

    Returns:
        Typed export result. Network, redaction, and exporter failures are
        represented as outcomes rather than escaping as runtime exceptions.

    Raises:
        TypeError: If ``trace`` is ``None``.
    """
    if trace is None:
        raise TypeError("trace must not be None")

    attempted_at = _utc_now_iso()
    endpoint_error = _validate_endpoint(config.endpoint)
    if endpoint_error is not None:
        return _finish_export(
            trace=trace,
            run=run,
            outcome=TraceExportOutcome.PERMANENT_FAILURE,
            spans_exported=0,
            endpoint=config.endpoint,
            failure_reason=endpoint_error,
            attempted_at_utc=attempted_at,
        )

    span_attrs_result = _redacted_span_attrs_or_result(trace, run, config, attempted_at)
    if isinstance(span_attrs_result, TraceExportResult):
        return span_attrs_result
    span_attrs = span_attrs_result

    oversized_reason = _oversized_reason(span_attrs, config=config)
    if oversized_reason is not None:
        return _finish_export(
            trace=trace,
            run=run,
            outcome=TraceExportOutcome.PERMANENT_FAILURE,
            spans_exported=0,
            endpoint=config.endpoint,
            failure_reason=oversized_reason,
            attempted_at_utc=attempted_at,
        )

    return _export_redacted_spans(trace, run, span_attrs, config=config, attempted_at_utc=attempted_at)


def _redacted_span_attrs_or_result(
    trace: WorkbenchTrace,
    run: WorkbenchRun,
    config: TraceExporterConfig,
    attempted_at_utc: str,
) -> tuple[OpenInferenceSpanAttributes, ...] | TraceExportResult:
    try:
        span_attrs = workbench_trace_to_openinference_attributes(
            trace,
            run,
            redact_text=config.redact_text,
            redact_keys=config.redact_keys,
        )
        return _redact_export_attributes(span_attrs, config=config)
    except Exception as exc:
        logger.warning("Trace export blocked because redaction failed: %s", type(exc).__name__)
        return _finish_export(
            trace=trace,
            run=run,
            outcome=TraceExportOutcome.REDACTION_BLOCKED,
            spans_exported=0,
            endpoint=config.endpoint,
            failure_reason=f"redaction failed: {type(exc).__name__}",
            attempted_at_utc=attempted_at_utc,
        )


def _export_redacted_spans(
    trace: WorkbenchTrace,
    run: WorkbenchRun,
    span_attrs: tuple[OpenInferenceSpanAttributes, ...],
    *,
    config: TraceExporterConfig,
    attempted_at_utc: str,
) -> TraceExportResult:
    spans_exported = 0
    if not _required_otel_modules_available(config):
        return _finish_export(
            trace=trace,
            run=run,
            outcome=TraceExportOutcome.PERMANENT_FAILURE,
            spans_exported=0,
            endpoint=config.endpoint,
            failure_reason="otel_sdk_missing",
            attempted_at_utc=attempted_at_utc,
        )
    try:
        exporter = cast("_SpanExporter", _ensure_exporter(config))
        export_result = exporter.export(_to_readable_spans(trace, span_attrs))
        if _span_export_succeeded(export_result):
            outcome = TraceExportOutcome.SUCCESS
            reason = None
            spans_exported = len(span_attrs)
        else:
            outcome = TraceExportOutcome.TRANSIENT_FAILURE
            reason = "span_export_result_failure"
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.SSLError) as exc:
        outcome = TraceExportOutcome.TRANSIENT_FAILURE
        reason = f"network_error: {type(exc).__name__}"
    except requests.exceptions.HTTPError as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        outcome = (
            TraceExportOutcome.PERMANENT_FAILURE
            if status_code and 400 <= status_code < 500
            else (TraceExportOutcome.TRANSIENT_FAILURE)
        )
        reason = f"http_error: {status_code or type(exc).__name__}"
    except Exception as exc:
        outcome = TraceExportOutcome.PERMANENT_FAILURE
        reason = f"unexpected_exporter_error: {type(exc).__name__}"

    return _finish_export(
        trace=trace,
        run=run,
        outcome=outcome,
        spans_exported=spans_exported,
        endpoint=config.endpoint,
        failure_reason=reason,
        attempted_at_utc=attempted_at_utc,
    )


def import_otel_trace(
    otel_trace_record: dict[str, Any] | Sequence[OpenInferenceSpanAttributes] | object,
    *,
    project_id: str,
    spine: object | None = None,
) -> WorkbenchTrace:
    """Import an OpenInference trace record and append it to the Workbench spine.

    Args:
        otel_trace_record: Dict or sequence of ``OpenInferenceSpanAttributes``.
        project_id: Project id for the target spine append.
        spine: Optional injected WorkbenchSpine-compatible object.

    Returns:
        The appended Workbench trace.

    Raises:
        TraceImportError: If the record is malformed or unsupported.
    """
    record = _coerce_trace_record(otel_trace_record)
    trace = WorkbenchTrace(
        trace_id=record["trace_id"],
        run_id=record["run_id"],
        root_span_id=record["root_span_id"],
        spans=tuple(openinference_attributes_to_trace_span(attrs) for attrs in record["spans"]),
        captured_at_utc=record["captured_at_utc"],
    )
    target_spine = cast("_TraceSpine", spine if spine is not None else get_workbench_spine())
    append_trace = target_spine.append_trace
    try:
        append_trace(trace)
    except TypeError:
        append_trace(project_id, trace)
    return trace


def round_trip_trace(
    trace: WorkbenchTrace,
    run: WorkbenchRun,
    *,
    config: TraceExporterConfig,
    spine: object | None = None,
) -> RoundTripResult:
    """Export a trace and import the same OpenInference value-shape back.

    Args:
        trace: Source Workbench trace.
        run: Run metadata associated with the trace.
        config: OTLP exporter configuration.
        spine: Optional injected WorkbenchSpine-compatible object.

    Returns:
        Export result plus the imported trace when export succeeds.
    """
    attrs = workbench_trace_to_openinference_attributes(
        trace,
        run,
        redact_text=config.redact_text,
        redact_keys=config.redact_keys,
    )
    export_result = export_trace_to_otlp(trace, run, config=config)
    if export_result.outcome is not TraceExportOutcome.SUCCESS:
        return RoundTripResult(export_result=export_result, imported_trace=None, value_shape_equal=None)

    imported = import_otel_trace(
        {
            "trace_id": trace.trace_id,
            "run_id": trace.run_id,
            "root_span_id": trace.root_span_id,
            "spans": attrs,
            "captured_at_utc": trace.captured_at_utc,
        },
        project_id=run.project_id,
        spine=spine,
    )
    return RoundTripResult(
        export_result=export_result,
        imported_trace=imported,
        value_shape_equal=_trace_value_shape_equal(trace, imported),
    )


def _validate_endpoint(endpoint: str | None) -> str | None:
    if endpoint is None:
        return "config.endpoint is None"
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"}:
        return "endpoint scheme must be http(s); grpc and other schemes are not supported"
    if not parsed.netloc:
        return "endpoint must include a host"
    return None


def _redact_export_attributes(
    span_attrs: tuple[OpenInferenceSpanAttributes, ...],
    *,
    config: TraceExporterConfig,
) -> tuple[OpenInferenceSpanAttributes, ...]:
    redacted: list[OpenInferenceSpanAttributes] = []
    for attrs in span_attrs:
        normalized: list[tuple[str, ScalarAttribute]] = []
        for key, value in attrs.attributes:
            if config.redact_keys(key):
                normalized.append((key, REDACTED))
            elif isinstance(value, str):
                normalized.append((key, config.redact_text(value)))
            else:
                normalized.append((key, value))
        redacted.append(replace(attrs, attributes=tuple(normalized)))
    return tuple(redacted)


def _oversized_reason(
    span_attrs: tuple[OpenInferenceSpanAttributes, ...],
    *,
    config: TraceExporterConfig,
) -> str | None:
    for attrs in span_attrs:
        if len(attrs.attributes) > config.max_attributes_per_span:
            return (
                f"oversized_batch: span has {len(attrs.attributes)} attributes, max is {config.max_attributes_per_span}"
            )
    payload = json.dumps([_attrs_to_jsonable(attrs) for attrs in span_attrs], separators=(",", ":")).encode("utf-8")
    if len(payload) > config.max_payload_bytes:
        return f"oversized_payload: bytes={len(payload)}, max={config.max_payload_bytes}"
    return None


def _to_readable_spans(
    trace: WorkbenchTrace,
    span_attrs: tuple[OpenInferenceSpanAttributes, ...],
) -> list[object]:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.trace import SpanContext, SpanKind, TraceFlags, TraceState
    from opentelemetry.trace.status import Status, StatusCode

    trace_id_int = _stable_int(trace.trace_id, bits=128)
    spans: list[object] = []
    for attrs in span_attrs:
        context = SpanContext(
            trace_id=trace_id_int,
            span_id=_stable_int(attrs.span_id, bits=64),
            is_remote=False,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )
        parent = None
        if attrs.parent_span_id is not None:
            parent = SpanContext(
                trace_id=trace_id_int,
                span_id=_stable_int(attrs.parent_span_id, bits=64),
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
                trace_state=TraceState(),
            )
        spans.append(
            ReadableSpan(
                name=attrs.name,
                context=context,
                parent=parent,
                attributes=dict(attrs.attributes),
                kind=SpanKind.INTERNAL,
                status=Status(StatusCode.ERROR if attrs.status_error else StatusCode.OK),
                start_time=attrs.start_time_unix_nano,
                end_time=attrs.end_time_unix_nano,
            )
        )
    return spans


def _readable_span_to_jsonable(span: object) -> dict[str, Any]:
    context = span.get_span_context()
    parent = getattr(span, "parent", None)
    status = getattr(span, "status", None)
    status_code = getattr(status, "status_code", None)
    return {
        "name": getattr(span, "name", ""),
        "trace_id": f"{context.trace_id:032x}",
        "span_id": f"{context.span_id:016x}",
        "parent_span_id": f"{parent.span_id:016x}" if parent is not None else None,
        "attributes": dict(getattr(span, "attributes", {}) or {}),
        "start_time_unix_nano": getattr(span, "start_time", None),
        "end_time_unix_nano": getattr(span, "end_time", None),
        "status": str(getattr(status_code, "name", status_code)),
    }


def _finish_export(
    *,
    trace: WorkbenchTrace,
    run: WorkbenchRun,
    outcome: TraceExportOutcome,
    spans_exported: int,
    endpoint: str | None,
    failure_reason: str | None,
    attempted_at_utc: str,
) -> TraceExportResult:
    result = TraceExportResult(
        outcome=outcome,
        spans_exported=spans_exported,
        endpoint=endpoint,
        failure_reason=failure_reason,
        receipt_id="",
        attempted_at_utc=attempted_at_utc,
    )
    receipt_id = _emit_trace_export_receipt(trace=trace, run=run, result=result)
    return replace(result, receipt_id=receipt_id)


def _emit_trace_export_receipt(
    *,
    trace: WorkbenchTrace,
    run: WorkbenchRun,
    result: TraceExportResult,
) -> str:
    parsed = urlparse(result.endpoint or "")
    endpoint_host = parsed.netloc or "none"
    receipt = WorkReceipt(
        project_id=run.project_id,
        agent_id=_RECEIPT_ACTOR,
        agent_type=AgentType.WORKBENCH,
        kind=WorkReceiptKind.TRACE_EXPORT,
        outcome=OutcomeSignal(
            passed=result.outcome is TraceExportOutcome.SUCCESS,
            score=1.0 if result.outcome is TraceExportOutcome.SUCCESS else 0.0,
            basis=EvidenceBasis.TOOL_EVIDENCE,
            tool_evidence=(
                ToolEvidence(
                    tool_name="otlp_http_trace_export",
                    command=f"POST {endpoint_host}",
                    exit_code=0 if result.outcome is TraceExportOutcome.SUCCESS else 1,
                    stdout_snippet=f"spans={result.spans_exported} outcome={result.outcome.value}",
                    passed=result.outcome is TraceExportOutcome.SUCCESS,
                ),
            ),
            provenance=Provenance(
                source=__name__,
                timestamp_utc=result.attempted_at_utc,
                tool_name="otlp_http_trace_export",
            ),
            issues=() if result.outcome is TraceExportOutcome.SUCCESS else (result.outcome.value,),
            kind=ShardKind.STANDARD,
        ),
        started_at_utc=result.attempted_at_utc,
        finished_at_utc=_utc_now_iso(),
        inputs_summary=f"trace_id={trace.trace_id} run_id={trace.run_id} endpoint_host={endpoint_host}",
        outputs_summary=f"spans={result.spans_exported} outcome={result.outcome.value}",
    )
    try:
        _get_receipt_store().append(receipt)
    except Exception:
        logger.exception("Failed to emit TRACE_EXPORT receipt for trace %s", trace.trace_id)
        return ""
    return str(receipt.receipt_id)


def _get_receipt_store() -> WorkReceiptStore:
    return WorkReceiptStore()


def _coerce_trace_record(
    otel_trace_record: dict[str, Any] | Sequence[OpenInferenceSpanAttributes] | object,
) -> dict[str, Any]:
    return _coerce_trace_record_impl(
        otel_trace_record,
        error_cls=TraceImportError,
        utc_now_iso=_utc_now_iso,
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = (
    *TRACE_INTEROP_PUBLIC_EXPORTS,
    "claim_exporter_cache_lifecycle",
    "exporter_cache_lifecycle_owner",
    "release_exporter_cache_lifecycle",
)
