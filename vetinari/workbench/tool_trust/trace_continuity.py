"""Trace continuity checks across subagent, LLM, tool, and MCP spans."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from vetinari.workbench.traces import WorkbenchTrace


class TraceContinuityStatus(str, Enum):
    """Trace continuity promotion status."""

    ALLOWED = "allowed"
    IMPORT_DEGRADED = "import_degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class TraceContinuityDecision:
    """Result for one trace continuity check."""

    status: TraceContinuityStatus
    promoted: bool
    receipt_id: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", TraceContinuityStatus(self.status))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TraceContinuityDecision(status={self.status!r}, promoted={self.promoted!r}, receipt_id={self.receipt_id!r})"


def assess_trace_continuity(trace: WorkbenchTrace, *, expected_run_id: str) -> TraceContinuityDecision:
    """Require one run id and parent continuity before promoting a trace receipt.

    Returns:
        TraceContinuityDecision value produced by assess_trace_continuity().
    """
    reasons: list[str] = []
    if trace.run_id != expected_run_id:
        reasons.append("run_id_mismatch")
    span_ids = {span.span_id for span in trace.spans}
    for span in trace.spans:
        if span.span_id != trace.root_span_id and not span.parent_span_id:
            reasons.append(f"missing_parent_span:{span.span_id}")
        if span.parent_span_id and span.parent_span_id not in span_ids:
            reasons.append(f"orphan_parent_span:{span.span_id}")
        if span.tool_name.startswith("mcp.") and not span.parent_span_id:
            reasons.append(f"mcp_orphan_span:{span.span_id}")
    if any(reason.startswith(("orphan_parent_span", "mcp_orphan_span")) for reason in reasons):
        return TraceContinuityDecision(TraceContinuityStatus.IMPORT_DEGRADED, False, "", tuple(reasons))
    if reasons:
        return TraceContinuityDecision(TraceContinuityStatus.BLOCKED, False, "", tuple(reasons))
    graph_hash = hashlib.sha256(
        "|".join(f"{span.span_id}>{span.parent_span_id or 'root'}" for span in trace.spans).encode("utf-8")
    ).hexdigest()[:16]
    return TraceContinuityDecision(
        TraceContinuityStatus.ALLOWED,
        True,
        f"trace-continuity:{trace.run_id}:{trace.trace_id}:{graph_hash}",
        ("trace-continuity-ok",),
    )


__all__ = ["TraceContinuityDecision", "TraceContinuityStatus", "assess_trace_continuity"]
