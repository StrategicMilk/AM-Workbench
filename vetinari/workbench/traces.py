"""Typed operator-readable trace records linked to workbench runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vetinari.utils.serialization import dataclass_to_dict


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


@dataclass(frozen=True, slots=True)
class TraceSpan:
    """One terminated span in a workbench trace tree."""

    span_id: str
    parent_span_id: str | None
    tool_name: str
    started_at_utc: str
    finished_at_utc: str
    inputs_hash: str
    outputs_hash: str
    error: str
    duration_ms: int

    def __post_init__(self) -> None:
        _require_non_empty(self.span_id, "span_id")
        _require_non_empty(self.tool_name, "tool_name")
        _require_non_empty(self.started_at_utc, "started_at_utc")
        _require_non_empty(self.finished_at_utc, "finished_at_utc")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be >= 0")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"TraceSpan(span_id={self.span_id!r}, parent_span_id={self.parent_span_id!r}, tool_name={self.tool_name!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the operator-console JSON contract for this span."""
        return dataclass_to_dict(self)


@dataclass(frozen=True, slots=True)
class WorkbenchTrace:
    """A span tree captured for one workbench run."""

    trace_id: str
    run_id: str
    root_span_id: str
    spans: tuple[TraceSpan, ...]
    captured_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.trace_id, "trace_id")
        _require_non_empty(self.run_id, "run_id")
        _require_non_empty(self.root_span_id, "root_span_id")
        _require_non_empty(self.captured_at_utc, "captured_at_utc")
        if not self.spans:
            raise ValueError("spans must be non-empty")
        span_ids = [span.span_id for span in self.spans]
        if len(span_ids) != len(set(span_ids)):
            raise ValueError("duplicate span_id rejected")
        span_id_set = set(span_ids)
        if self.root_span_id not in span_id_set:
            raise ValueError(f"root_span_id {self.root_span_id!r} not present in spans")
        root = next(span for span in self.spans if span.span_id == self.root_span_id)
        if root.parent_span_id is not None:
            raise ValueError("root span must have parent_span_id=None")
        for span in self.spans:
            if span.parent_span_id is not None and span.parent_span_id not in span_id_set:
                raise ValueError(f"orphan parent_span_id rejected for span {span.span_id!r}")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchTrace(trace_id={self.trace_id!r}, run_id={self.run_id!r}, root_span_id={self.root_span_id!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the operator-console JSON contract for this trace."""
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "root_span_id": self.root_span_id,
            "spans": [span.to_dict() for span in self.spans],
            "captured_at_utc": self.captured_at_utc,
        }


__all__ = ["TraceSpan", "WorkbenchTrace"]
