"""Pure-data OpenInference mapping for Workbench traces.

Step OpenInference-Map: map ``WorkbenchTrace`` and ``TraceSpan`` records to a
minimal OpenInference 0.1.29 attribute surface. No network I/O, no module-level
side effects, and no OpenTelemetry SDK import at module load.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from vetinari.security.redaction import REDACTED
from vetinari.workbench.runs import WorkbenchRun
from vetinari.workbench.traces import TraceSpan, WorkbenchTrace

OPENINFERENCE_SCHEMA_VERSION: str = "0.1.29"
OTEL_SDK_VERSION_TESTED: str = "1.41.0"

_OPENINFERENCE_SPAN_KIND_KEY = "openinference.span.kind"
_INPUT_VALUE_KEY = "input.value"
_OUTPUT_VALUE_KEY = "output.value"
_TOOL_NAME_KEY = "tool.name"
_LLM_MODEL_NAME_KEY = "llm.model_name"
_LLM_SYSTEM_KEY = "llm.system"
_LLM_INVOCATION_PARAMETERS_KEY = "llm.invocation_parameters"
_RETRIEVAL_DOCUMENTS_KEY = "retrieval.documents"
_EMBEDDING_MODEL_NAME_KEY = "embedding.model_name"
_SESSION_ID_KEY = "session.id"
_USER_ID_KEY = "user.id"
_VETINARI_TRACE_ID_KEY = "vetinari.trace_id"
_VETINARI_RUN_ID_KEY = "vetinari.run_id"
_VETINARI_SCHEMA_VERSION_KEY = "vetinari.openinference.schema_version"
_VETINARI_PROJECT_ID_KEY = "vetinari.project_id"
_VETINARI_ACTOR_AGENT_TYPE_KEY = "vetinari.actor_agent_type"
_VETINARI_RUN_KIND_KEY = "vetinari.run_kind"
_VETINARI_RUN_STATUS_KEY = "vetinari.run_status"
_VETINARI_INPUTS_HASH_KEY = "vetinari.inputs_hash"
_VETINARI_OUTPUTS_HASH_KEY = "vetinari.outputs_hash"


class OpenInferenceSpanKind(str, Enum):
    """OpenInference span kind taxonomy mirrored from spec version 0.1.29."""

    LLM = "LLM"  # Language model invocation.
    CHAIN = "CHAIN"  # Multi-step chain execution.
    TOOL = "TOOL"  # Tool call or external function invocation.
    RETRIEVER = "RETRIEVER"  # Retrieval/search span.
    EMBEDDING = "EMBEDDING"  # Embedding creation span.
    AGENT = "AGENT"  # Agent orchestration span.
    RERANKER = "RERANKER"  # Reranker span.
    UNKNOWN = "UNKNOWN"  # Fail-closed fallback for unmapped tool names.


_OPENINFERENCE_KIND_BY_TOOL_NAME: dict[str, OpenInferenceSpanKind] = {
    "llm.complete": OpenInferenceSpanKind.LLM,
    "llm.chat": OpenInferenceSpanKind.LLM,
    "llm.invoke": OpenInferenceSpanKind.LLM,
    "tool.run": OpenInferenceSpanKind.TOOL,
    "tool.invoke": OpenInferenceSpanKind.TOOL,
    "retrieval.search": OpenInferenceSpanKind.RETRIEVER,
    "retrieval.fetch": OpenInferenceSpanKind.RETRIEVER,
    "embedding.encode": OpenInferenceSpanKind.EMBEDDING,
    "reranker.rerank": OpenInferenceSpanKind.RERANKER,
    "agent.run": OpenInferenceSpanKind.AGENT,
    "chain.run": OpenInferenceSpanKind.CHAIN,
}


ScalarAttribute = str | int | float | bool


@dataclass(frozen=True, slots=True)
class OpenInferenceSpanAttributes:
    """Redacted attributes for one exported OpenInference span."""

    span_id: str
    parent_span_id: str | None
    name: str
    kind: OpenInferenceSpanKind
    start_time_unix_nano: int
    end_time_unix_nano: int
    attributes: tuple[tuple[str, ScalarAttribute], ...]
    status_error: bool

    def __repr__(self) -> str:
        return (
            "OpenInferenceSpanAttributes("
            f"span_id={self.span_id!r}, kind={self.kind.value!r}, "
            f"attribute_count={len(self.attributes)}, status_error={self.status_error!r})"
        )


def workbench_trace_to_openinference_attributes(
    trace: WorkbenchTrace,
    run: WorkbenchRun,
    *,
    redact_text: Callable[[str], str],
    redact_keys: Callable[[object], bool],
) -> tuple[OpenInferenceSpanAttributes, ...]:
    """Map a Workbench trace into redacted OpenInference span attributes.

    Args:
        trace: Workbench trace whose spans should be exported.
        run: Run metadata attached to every exported span.
        redact_text: Text redactor invoked for string attribute values.
        redact_keys: Predicate that redacts sensitive attribute keys.

    Returns:
        Frozen tuple of OpenInference span attribute records.
    """
    mapped: list[OpenInferenceSpanAttributes] = []
    for span in trace.spans:
        kind = _OPENINFERENCE_KIND_BY_TOOL_NAME.get(span.tool_name, OpenInferenceSpanKind.UNKNOWN)
        raw_attrs: tuple[tuple[str, ScalarAttribute], ...] = (
            (_VETINARI_TRACE_ID_KEY, trace.trace_id),
            (_VETINARI_RUN_ID_KEY, trace.run_id),
            (_VETINARI_SCHEMA_VERSION_KEY, OPENINFERENCE_SCHEMA_VERSION),
            (_VETINARI_PROJECT_ID_KEY, run.project_id),
            (_VETINARI_ACTOR_AGENT_TYPE_KEY, run.actor_agent_type.value),
            (_VETINARI_RUN_KIND_KEY, run.kind.value),
            (_VETINARI_RUN_STATUS_KEY, run.status.value),
            (_OPENINFERENCE_SPAN_KIND_KEY, kind.value),
            (_TOOL_NAME_KEY, span.tool_name),
            (_SESSION_ID_KEY, run.run_id),
            (_VETINARI_INPUTS_HASH_KEY, span.inputs_hash),
            (_VETINARI_OUTPUTS_HASH_KEY, span.outputs_hash),
        )
        mapped.append(
            OpenInferenceSpanAttributes(
                span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                name=span.tool_name,
                kind=kind,
                start_time_unix_nano=_iso_to_unix_nano(span.started_at_utc),
                end_time_unix_nano=_iso_to_unix_nano(span.finished_at_utc),
                attributes=_redact_attributes(raw_attrs, redact_text=redact_text, redact_keys=redact_keys),
                status_error=bool(span.error),
            )
        )
    return tuple(mapped)


def openinference_attributes_to_trace_span(attrs: OpenInferenceSpanAttributes) -> TraceSpan:
    """Convert one OpenInference span attribute record back into a TraceSpan.

    Args:
        attrs: OpenInference span attributes to reverse-map.

    Returns:
        A Workbench ``TraceSpan`` with the preserved value shape.
    """
    attr_map = dict(attrs.attributes)
    return TraceSpan(
        span_id=attrs.span_id,
        parent_span_id=attrs.parent_span_id,
        tool_name=str(attr_map.get(_TOOL_NAME_KEY, attrs.name)),
        started_at_utc=_unix_nano_to_iso(attrs.start_time_unix_nano),
        finished_at_utc=_unix_nano_to_iso(attrs.end_time_unix_nano),
        inputs_hash=str(attr_map.get(_VETINARI_INPUTS_HASH_KEY, "")),
        outputs_hash=str(attr_map.get(_VETINARI_OUTPUTS_HASH_KEY, "")),
        error="openinference_status_error" if attrs.status_error else "",
        duration_ms=max(0, (attrs.end_time_unix_nano - attrs.start_time_unix_nano) // 1_000_000),
    )


def _redact_attributes(
    attributes: tuple[tuple[str, ScalarAttribute], ...],
    *,
    redact_text: Callable[[str], str],
    redact_keys: Callable[[object], bool],
) -> tuple[tuple[str, ScalarAttribute], ...]:
    redacted: list[tuple[str, ScalarAttribute]] = []
    for key, value in attributes:
        if redact_keys(key):
            redacted.append((key, REDACTED))
        elif isinstance(value, str):
            redacted.append((key, redact_text(value)))
        else:
            redacted.append((key, value))
    return tuple(redacted)


def _iso_to_unix_nano(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


def _unix_nano_to_iso(value: int) -> str:
    dt = datetime.fromtimestamp(value / 1_000_000_000, tz=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


__all__ = [
    "OPENINFERENCE_SCHEMA_VERSION",
    "OTEL_SDK_VERSION_TESTED",
    "OpenInferenceSpanAttributes",
    "OpenInferenceSpanKind",
    "openinference_attributes_to_trace_span",
    "workbench_trace_to_openinference_attributes",
]
