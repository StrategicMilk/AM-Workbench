"""Private OpenInference trace interop value-shape helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import timezone
from typing import Any

from vetinari.workbench.openinference import (
    OPENINFERENCE_SCHEMA_VERSION,
    OTEL_SDK_VERSION_TESTED,
    OpenInferenceSpanAttributes,
)
from vetinari.workbench.traces import WorkbenchTrace

TRACE_INTEROP_PUBLIC_EXPORTS = [
    "RoundTripResult",
    "TraceExportOutcome",
    "TraceExportResult",
    "TraceExporterConfig",
    "TraceImportError",
    "export_trace_to_otlp",
    "import_otel_trace",
    "reset_exporter_cache_for_test",
    "round_trip_trace",
]


def _require_openinference_spans(spans: Iterable[object], error_cls: type[Exception]) -> None:
    for span in spans:
        if not isinstance(span, OpenInferenceSpanAttributes):
            raise error_cls(f"expected OpenInferenceSpanAttributes, got {type(span).__name__}")


def _coerce_trace_record(
    otel_trace_record: dict[str, Any] | Sequence[OpenInferenceSpanAttributes] | object,
    *,
    error_cls: type[Exception],
    utc_now_iso: Any,
) -> dict[str, Any]:
    if isinstance(otel_trace_record, dict):
        required = {"trace_id", "run_id", "root_span_id", "spans", "captured_at_utc"}
        missing = required.difference(otel_trace_record)
        if missing:
            raise error_cls(f"missing required trace record keys: {sorted(missing)}")
        spans = tuple(otel_trace_record["spans"])
        if not spans:
            raise error_cls("spans must be non-empty")
        _require_openinference_spans(spans, error_cls)
        return {
            "trace_id": str(otel_trace_record["trace_id"]),
            "run_id": str(otel_trace_record["run_id"]),
            "root_span_id": str(otel_trace_record["root_span_id"]),
            "spans": spans,
            "captured_at_utc": str(otel_trace_record["captured_at_utc"]),
        }
    if isinstance(otel_trace_record, Sequence):
        spans = tuple(otel_trace_record)
        if not spans:
            raise error_cls("spans must be non-empty")
        _require_openinference_spans(spans, error_cls)
        attr_map = dict(spans[0].attributes)
        trace_id = attr_map.get("vetinari.trace_id")
        run_id = attr_map.get("vetinari.run_id")
        if not trace_id or not run_id:
            raise error_cls("span sequence missing vetinari.trace_id or vetinari.run_id")
        return {
            "trace_id": str(trace_id),
            "run_id": str(run_id),
            "root_span_id": spans[0].span_id,
            "spans": spans,
            "captured_at_utc": utc_now_iso(),
        }
    raise error_cls("native_otel_readable_span_sequence_unsupported_in_this_pack")


def _trace_value_shape_equal(source: WorkbenchTrace, imported: WorkbenchTrace) -> bool:
    if (
        source.trace_id != imported.trace_id
        or source.run_id != imported.run_id
        or source.root_span_id != imported.root_span_id
        or len(source.spans) != len(imported.spans)
    ):
        return False
    for left, right in zip(source.spans, imported.spans, strict=True):
        if (
            left.span_id != right.span_id
            or left.parent_span_id != right.parent_span_id
            or left.tool_name != right.tool_name
            or _normalize_iso(left.started_at_utc) != _normalize_iso(right.started_at_utc)
            or _normalize_iso(left.finished_at_utc) != _normalize_iso(right.finished_at_utc)
            or left.duration_ms != right.duration_ms
            or left.inputs_hash != right.inputs_hash
            or left.outputs_hash != right.outputs_hash
        ):
            return False
        if bool(left.error) != bool(right.error):
            return False
    return True


def _attrs_to_jsonable(attrs: OpenInferenceSpanAttributes) -> dict[str, Any]:
    return {
        "span_id": attrs.span_id,
        "parent_span_id": attrs.parent_span_id,
        "name": attrs.name,
        "kind": attrs.kind.value,
        "start_time_unix_nano": attrs.start_time_unix_nano,
        "end_time_unix_nano": attrs.end_time_unix_nano,
        "attributes": list(attrs.attributes),
        "status_error": attrs.status_error,
        "openinference_schema": OPENINFERENCE_SCHEMA_VERSION,
        "otel_sdk_tested": OTEL_SDK_VERSION_TESTED,
    }


def _stable_int(value: str, *, bits: int) -> int:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return int.from_bytes(digest, "big") & ((1 << bits) - 1)


def _normalize_iso(value: str) -> str:
    from datetime import datetime

    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
