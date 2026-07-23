"""Multi-source promoter for core-loop eval cases.

Reasons: ``trace-rejected``, ``empty-correction``, ``empty-halt-reason``,
``route-not-promotable``, ``recall-not-promotable``, ``empty-tool-error``,
``autopsy-not-promotable``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Final

from vetinari.workbench.agents.routing import RouteDecisionOutcome, RouteDecisionRecord
from vetinari.workbench.evals import EvalKind
from vetinari.workbench.failure_intelligence import AutopsyResult, FailureKind
from vetinari.workbench.metadata_spine import WorkbenchSpine
from vetinari.workbench.trace_eval_core.case import (
    CoreLoopEventKind,
    EvalCaseProvenance,
    EvalCaseRecord,
    ReplayCommand,
)
from vetinari.workbench.trace_to_eval import TraceEvalFactory, TraceEvalFactoryError
from vetinari.workbench.traces import WorkbenchTrace

_PROMOTABLE_ROUTE_OUTCOMES: Final[frozenset[RouteDecisionOutcome]] = frozenset({
    RouteDecisionOutcome.DENIED,
    RouteDecisionOutcome.DEGRADED,
    RouteDecisionOutcome.FALLBACK_SELECTED,
})
_PROMOTABLE_FAILURE_KINDS: Final[frozenset[FailureKind]] = frozenset({
    FailureKind.INSUFFICIENT_EVAL,
    FailureKind.BAD_PROMPT,
    FailureKind.BAD_ROUTING,
    FailureKind.WEAK_METHOD,
    FailureKind.HALLUCINATED_TOOL_ABILITY,
    FailureKind.DATASET_DRIFT,
})
_PROMOTABLE_RECALL_STATUSES: Final[frozenset[str]] = frozenset({"degraded", "blocked", "stale"})


class EvalCasePromoterError(Exception):
    """Raised when an upstream event is not eligible for eval-case promotion."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason

    def __str__(self) -> str:
        return f"{self.args[0]} (reason={self.reason})"


class EvalCasePromoter:
    """Promote already-captured evidence into typed eval-case records."""

    def __init__(self, *, spine: WorkbenchSpine, factory: TraceEvalFactory | None = None) -> None:
        self._spine = spine
        self._factory = factory if factory is not None else TraceEvalFactory(spine)

    def promote_failed_trace(
        self,
        *,
        trace: WorkbenchTrace,
        asset_id: str,
        asset_revision: str,
        replay_command: ReplayCommand,
        consumer_feed_targets: tuple[str, ...],
    ) -> EvalCaseRecord:
        """Execute the promote failed trace operation.

        Returns:
            EvalCaseRecord value produced by promote_failed_trace().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            eval_result, _scaffold = self._factory.promote_trace(
                trace,
                asset_id=asset_id,
                asset_revision=asset_revision,
            )
        except TraceEvalFactoryError as exc:
            raise EvalCasePromoterError("trace was rejected by trace-to-eval factory", reason="trace-rejected") from exc
        return self._record(
            source_event_kind=CoreLoopEventKind.FAILED_TRACE,
            source_event_id=trace.trace_id,
            source_run_id=trace.run_id,
            asset_id=asset_id,
            asset_revision=asset_revision,
            replay_command=replay_command,
            consumer_feed_targets=consumer_feed_targets,
            kind=EvalKind.LIVE_TRACE_DERIVED,
            eval_result_ref=eval_result.eval_id,
        )

    def promote_user_correction(
        self,
        *,
        source_run_id: str,
        correction_id: str,
        original_text: str,
        corrected_text: str,
        asset_id: str,
        asset_revision: str,
        replay_command: ReplayCommand,
        consumer_feed_targets: tuple[str, ...],
    ) -> EvalCaseRecord:
        """Execute the promote user correction operation.

        Returns:
            EvalCaseRecord value produced by promote_user_correction().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not original_text.strip() or not corrected_text.strip():
            raise EvalCasePromoterError("correction text must be non-empty", reason="empty-correction")
        return self._record(
            source_event_kind=CoreLoopEventKind.USER_CORRECTION,
            source_event_id=correction_id,
            source_run_id=source_run_id,
            asset_id=asset_id,
            asset_revision=asset_revision,
            replay_command=replay_command,
            consumer_feed_targets=consumer_feed_targets,
            kind=EvalKind.HUMAN_ANNOTATION,
        )

    def promote_watcher_halt(
        self,
        *,
        halt_id: str,
        halted_run_id: str,
        halt_reason: str,
        asset_id: str,
        asset_revision: str,
        replay_command: ReplayCommand,
        consumer_feed_targets: tuple[str, ...],
    ) -> EvalCaseRecord:
        """Execute the promote watcher halt operation.

        Returns:
            EvalCaseRecord value produced by promote_watcher_halt().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not halt_reason.strip():
            raise EvalCasePromoterError("halt_reason must be non-empty", reason="empty-halt-reason")
        return self._record(
            source_event_kind=CoreLoopEventKind.WATCHER_HALT,
            source_event_id=halt_id,
            source_run_id=halted_run_id,
            asset_id=asset_id,
            asset_revision=asset_revision,
            replay_command=replay_command,
            consumer_feed_targets=consumer_feed_targets,
            kind=EvalKind.RED_TEAM,
        )

    def promote_route_decision(
        self,
        *,
        decision: RouteDecisionRecord,
        asset_id: str,
        asset_revision: str,
        replay_command: ReplayCommand,
        consumer_feed_targets: tuple[str, ...],
    ) -> EvalCaseRecord:
        """Execute the promote route decision operation.

        Returns:
            EvalCaseRecord value produced by promote_route_decision().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if decision.outcome not in _PROMOTABLE_ROUTE_OUTCOMES:
            raise EvalCasePromoterError("route decision is not regression evidence", reason="route-not-promotable")
        return self._record(
            source_event_kind=CoreLoopEventKind.ROUTE_DECISION,
            source_event_id=decision.decision_id,
            source_run_id=None,
            asset_id=asset_id,
            asset_revision=asset_revision,
            replay_command=replay_command,
            consumer_feed_targets=consumer_feed_targets,
            kind=EvalKind.LIVE_TRACE_DERIVED,
            route_decision_ref=decision.decision_id,
        )

    def promote_memory_recall(
        self,
        *,
        recall_id: str,
        source_run_id: str,
        recall_status: str,
        blocked_signals: tuple[str, ...],
        asset_id: str,
        asset_revision: str,
        replay_command: ReplayCommand,
        consumer_feed_targets: tuple[str, ...],
    ) -> EvalCaseRecord:
        """Execute the promote memory recall operation.

        Returns:
            EvalCaseRecord value produced by promote_memory_recall().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if recall_status not in _PROMOTABLE_RECALL_STATUSES:
            raise EvalCasePromoterError("memory recall is not promotable", reason="recall-not-promotable")
        return self._record(
            source_event_kind=CoreLoopEventKind.MEMORY_RECALL,
            source_event_id=recall_id,
            source_run_id=source_run_id,
            asset_id=asset_id,
            asset_revision=asset_revision,
            replay_command=replay_command,
            consumer_feed_targets=consumer_feed_targets,
            kind=EvalKind.LIVE_TRACE_DERIVED,
            redteam_fixture_ref=blocked_signals[0] if blocked_signals else None,
        )

    def promote_tool_error(
        self,
        *,
        error_id: str,
        source_run_id: str,
        tool_name: str,
        error_text: str,
        asset_id: str,
        asset_revision: str,
        replay_command: ReplayCommand,
        consumer_feed_targets: tuple[str, ...],
    ) -> EvalCaseRecord:
        """Execute the promote tool error operation.

        Returns:
            EvalCaseRecord value produced by promote_tool_error().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not tool_name.strip() or not error_text.strip():
            raise EvalCasePromoterError("tool error requires tool_name and error_text", reason="empty-tool-error")
        return self._record(
            source_event_kind=CoreLoopEventKind.TOOL_ERROR,
            source_event_id=error_id,
            source_run_id=source_run_id,
            asset_id=asset_id,
            asset_revision=asset_revision,
            replay_command=replay_command,
            consumer_feed_targets=consumer_feed_targets,
            kind=EvalKind.LIVE_TRACE_DERIVED,
            redteam_fixture_ref=tool_name,
        )

    def promote_inspector_finding(
        self,
        *,
        autopsy: AutopsyResult,
        asset_id: str,
        asset_revision: str,
        replay_command: ReplayCommand,
        consumer_feed_targets: tuple[str, ...],
    ) -> EvalCaseRecord:
        """Execute the promote inspector finding operation.

        Returns:
            EvalCaseRecord value produced by promote_inspector_finding().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not any(candidate.failure_kind in _PROMOTABLE_FAILURE_KINDS for candidate in autopsy.candidates):
            raise EvalCasePromoterError("autopsy is not promotable", reason="autopsy-not-promotable")
        return self._record(
            source_event_kind=CoreLoopEventKind.INSPECTOR_FINDING,
            source_event_id=autopsy.autopsy_id,
            source_run_id=autopsy.run_id,
            asset_id=asset_id,
            asset_revision=asset_revision,
            replay_command=replay_command,
            consumer_feed_targets=consumer_feed_targets,
            kind=EvalKind.JUDGE_ONLY,
            failure_intelligence_autopsy_ref=autopsy.autopsy_id,
        )

    def _record(
        self,
        *,
        source_event_kind: CoreLoopEventKind,
        source_event_id: str,
        source_run_id: str | None,
        asset_id: str,
        asset_revision: str,
        replay_command: ReplayCommand,
        consumer_feed_targets: tuple[str, ...],
        kind: EvalKind,
        eval_result_ref: str | None = None,
        redteam_fixture_ref: str | None = None,
        failure_intelligence_autopsy_ref: str | None = None,
        route_decision_ref: str | None = None,
    ) -> EvalCaseRecord:
        return EvalCaseRecord(
            case_id=f"eval-case-{uuid.uuid4().hex}",
            provenance=EvalCaseProvenance(
                source_event_kind=source_event_kind,
                source_event_id=source_event_id,
                source_run_id=source_run_id,
                source_asset_id=asset_id,
                source_asset_revision=asset_revision,
                captured_at_utc=datetime.now(timezone.utc).isoformat(),
            ),
            replay_command=replay_command,
            kind=kind,
            eval_result_ref=eval_result_ref,
            redteam_fixture_ref=redteam_fixture_ref,
            failure_intelligence_autopsy_ref=failure_intelligence_autopsy_ref,
            route_decision_ref=route_decision_ref,
            consumer_feed_targets=consumer_feed_targets,
        )


__all__ = [
    "_PROMOTABLE_FAILURE_KINDS",
    "_PROMOTABLE_RECALL_STATUSES",
    "_PROMOTABLE_ROUTE_OUTCOMES",
    "EvalCasePromoter",
    "EvalCasePromoterError",
]
