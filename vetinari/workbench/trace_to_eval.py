"""Trace-to-eval factory for Workbench playground flows.

This is step 7 in the Workbench improvement spine: it reads a real
``WorkbenchTrace`` loaded by ``WorkbenchSpine.list_traces_for_run`` and
builds an ``EvalResult`` plus a replay scaffold. The eval result is only
constructed here; durable writes are owned by the playground promotion path.
Fail-closed boundaries reject missing traces, malformed traces, oversized
traces, schema-version mismatches, and synthesized in-memory traces that do
not round-trip through the spine.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Final

from vetinari.workbench.evals import EvalKind, EvalResult, EvalScore
from vetinari.workbench.metadata_spine import WorkbenchSpine
from vetinari.workbench.traces import WorkbenchTrace

logger = logging.getLogger(__name__)


_SCHEMA_VERSION: Final[int] = 1
_MAX_TRACE_BYTES: Final[int] = 1_048_576
MAX_TRACE_BYTES: Final[int] = _MAX_TRACE_BYTES


class TraceEvalFactoryError(Exception):
    """Typed fail-closed boundary error from trace promotion."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.args[0]} (reason={self.reason})"


@dataclass(frozen=True, slots=True)
class ReplayScaffold:
    """Editable replay inputs derived from a real Workbench trace."""

    trace_id: str
    run_id: str
    prompt_text: str
    agent_edits: tuple[str, ...]
    tool_overrides: tuple[str, ...]
    model_overrides: tuple[str, ...]
    captured_at_utc: str

    def __post_init__(self) -> None:
        if not self.trace_id.strip():
            raise ValueError("trace_id must be non-empty")
        if not self.run_id.strip():
            raise ValueError("run_id must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ReplayScaffold(trace_id={self.trace_id!r}, run_id={self.run_id!r}, prompt_text={self.prompt_text!r})"


class TraceEvalFactory:
    """Read-only trust boundary from real spine traces to eval cases."""

    def __init__(self, spine: WorkbenchSpine) -> None:
        self._spine = spine

    def trace_to_eval_case(
        self,
        trace: WorkbenchTrace,
        *,
        asset_id: str,
        asset_revision: str,
        kind: EvalKind = EvalKind.LIVE_TRACE_DERIVED,
    ) -> EvalResult:
        """Build an eval case from a spine-resident trace without writing it.

        Returns:
            EvalResult value produced by trace_to_eval_case().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        real_trace = self._load_real_trace(trace.run_id, trace.trace_id)
        if real_trace is None:
            raise TraceEvalFactoryError("trace not present in spine", reason="missing")
        if real_trace.spans != trace.spans:
            raise TraceEvalFactoryError("trace does not match spine record", reason="synthesised")

        _validate_trace_size(trace)
        _validate_trace_schema(trace, _SCHEMA_VERSION)
        failure_kind = _derive_failure_kind(trace, self._spine)
        passed = failure_kind == "clean"
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("derived trace eval trace_id=%s failure_kind=%s", trace.trace_id, failure_kind)
        eval_id = _deterministic_eval_id(
            trace_id=trace.trace_id,
            run_id=trace.run_id,
            asset_id=asset_id,
            asset_revision=asset_revision,
            kind=kind,
            failure_kind=failure_kind,
        )
        return EvalResult(
            eval_id=eval_id,
            kind=kind,
            run_id=trace.run_id,
            asset_id=asset_id,
            asset_revision=asset_revision,
            scores=(
                EvalScore(
                    metric_name="trace_failure",
                    value=1.0 if passed else 0.0,
                    threshold=1.0,
                    passed=passed,
                    unit="",
                ),
            ),
            captured_at_utc=datetime.now(timezone.utc).isoformat(),
            notes=f"derived from trace {trace.trace_id} ({failure_kind})",
        )

    def promote_trace(
        self,
        trace: WorkbenchTrace,
        *,
        asset_id: str,
        asset_revision: str,
        kind: EvalKind = EvalKind.LIVE_TRACE_DERIVED,
    ) -> tuple[EvalResult, ReplayScaffold]:
        """Return the eval case and editable replay scaffold for a real trace.

        Returns:
            tuple[EvalResult, ReplayScaffold] value produced by promote_trace().
        """
        eval_result = self.trace_to_eval_case(
            trace,
            asset_id=asset_id,
            asset_revision=asset_revision,
            kind=kind,
        )
        return eval_result, _build_replay_scaffold(trace)

    def _load_real_trace(self, run_id: str, trace_id: str) -> WorkbenchTrace | None:
        for trace in self._spine.list_traces_for_run(run_id):
            if trace.trace_id == trace_id:
                return trace
        return None


def _validate_trace_size(trace: WorkbenchTrace) -> None:
    size_bytes = len(json.dumps(asdict(trace), sort_keys=True).encode("utf-8"))
    if size_bytes > _MAX_TRACE_BYTES:
        raise TraceEvalFactoryError("trace exceeds MAX_TRACE_BYTES", reason="oversized")


def _validate_trace_schema(trace: WorkbenchTrace, expected_version: int) -> None:
    if expected_version != _SCHEMA_VERSION:
        raise TraceEvalFactoryError("trace schema does not match expected version", reason="schema-mismatch")
    if not trace.captured_at_utc.endswith(("Z", "+00:00")):
        raise TraceEvalFactoryError("trace schema does not match expected version", reason="schema-mismatch")
    span_ids = {span.span_id for span in trace.spans}
    if not span_ids or trace.root_span_id not in span_ids:
        raise TraceEvalFactoryError("trace span tree is malformed", reason="malformed")
    for span in trace.spans:
        if not span.span_id.strip():
            raise TraceEvalFactoryError("trace span tree is malformed", reason="malformed")
        if span.parent_span_id is not None and span.parent_span_id not in span_ids:
            raise TraceEvalFactoryError("trace span tree is malformed", reason="malformed")


def _derive_failure_kind(trace: WorkbenchTrace, spine: WorkbenchSpine | None = None) -> str:
    """Classify why a trace should become an eval case."""
    if any(span.error for span in trace.spans):
        return "failed"
    if any(span.tool_name.startswith("edit:") for span in trace.spans):
        return "edited"
    if any(span.tool_name.startswith("retrieve:") and not span.outputs_hash for span in trace.spans):
        return "suspicious-retrieval"
    if spine is not None:
        run = next((row for row in spine.list_runs() if row.run_id == trace.run_id), None)
        if run is not None and run.outcome is not None:
            if run.outcome.score < 0.5:
                return "low-score"
            if not run.outcome.passed:
                return "rejected"
    return "clean"


def _build_replay_scaffold(trace: WorkbenchTrace) -> ReplayScaffold:
    prompt_span = next((span for span in trace.spans if span.span_id != trace.root_span_id), None)
    return ReplayScaffold(
        trace_id=trace.trace_id,
        run_id=trace.run_id,
        prompt_text=prompt_span.inputs_hash if prompt_span is not None else "",
        agent_edits=(),
        tool_overrides=(),
        model_overrides=(),
        captured_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def _deterministic_eval_id(
    *,
    trace_id: str,
    run_id: str,
    asset_id: str,
    asset_revision: str,
    kind: EvalKind,
    failure_kind: str,
) -> str:
    seed = json.dumps(
        {
            "asset_id": asset_id,
            "asset_revision": asset_revision,
            "failure_kind": failure_kind,
            "kind": kind.value,
            "run_id": run_id,
            "trace_id": trace_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"trace-eval-{trace_id}-{digest}"


def trace_to_eval_case(
    trace: WorkbenchTrace,
    *,
    spine: WorkbenchSpine,
    asset_id: str,
    asset_revision: str,
    kind: EvalKind = EvalKind.LIVE_TRACE_DERIVED,
) -> EvalResult:
    """Convenience wrapper for one-off trace-to-eval conversion."""
    return TraceEvalFactory(spine).trace_to_eval_case(
        trace,
        asset_id=asset_id,
        asset_revision=asset_revision,
        kind=kind,
    )


__all__ = [
    "MAX_TRACE_BYTES",
    "ReplayScaffold",
    "TraceEvalFactory",
    "TraceEvalFactoryError",
    "trace_to_eval_case",
]
