"""Record-shape adapters for universal Workbench weaving events."""

from __future__ import annotations

from typing import Any

from vetinari.workbench.assets import WorkbenchAsset
from vetinari.workbench.automation import AutomationDefinition, AutomationRunReceipt
from vetinari.workbench.evals import EvalResult
from vetinari.workbench.leases import WorkbenchLease
from vetinari.workbench.monitoring import MonitoringSignal
from vetinari.workbench.proposals import Promotion, WorkbenchProposal
from vetinari.workbench.runs import WorkbenchRun
from vetinari.workbench.self_improvement import GovernanceDecision, ImprovementProposal
from vetinari.workbench.traces import WorkbenchTrace

WEAVING_PUBLIC_EXPORTS = [
    "ChangePropagationDecision",
    "ClosedLoopAcceptance",
    "InfluenceKind",
    "WeavingAuthorityLevel",
    "WorkbenchEvent",
    "WorkbenchEventKind",
    "WorkbenchInfluence",
    "WorkbenchSubjectKind",
    "WorkbenchWeavingError",
    "WorkbenchWeavingLedger",
    "authority_at_least",
    "event_from_workbench_record",
    "pack_acceptance_event",
]


def _record_shape(
    record: object, *, event_kind: Any, subject_kind: Any, error_cls: type[Exception]
) -> tuple[Any, Any, str, dict[str, Any]]:
    if isinstance(record, WorkbenchAsset):
        return (
            event_kind.ASSET_RECORDED,
            subject_kind.ASSET,
            record.asset_id,
            {
                "kind": record.kind.value,
                "revision": record.revision,
            },
        )
    if isinstance(record, WorkbenchRun):
        return (
            event_kind.RUN_RECORDED,
            subject_kind.RUN,
            record.run_id,
            {
                "kind": record.kind.value,
                "status": record.status.value,
            },
        )
    if isinstance(record, WorkbenchTrace):
        return (
            event_kind.TRACE_CAPTURED,
            subject_kind.TRACE,
            record.trace_id,
            {
                "run_id": record.run_id,
                "span_count": len(record.spans),
            },
        )
    if isinstance(record, EvalResult):
        return (
            event_kind.EVAL_RECORDED,
            subject_kind.EVAL,
            record.eval_id,
            {
                "run_id": record.run_id,
                "asset_id": record.asset_id,
                "score_count": len(record.scores),
            },
        )
    if isinstance(record, WorkbenchProposal):
        return (
            event_kind.PROPOSAL_RECORDED,
            subject_kind.PROPOSAL,
            record.proposal_id,
            {
                "kind": record.kind.value,
                "status": record.status.value,
            },
        )
    if isinstance(record, Promotion):
        return (
            event_kind.PROMOTION_DECIDED,
            subject_kind.PROMOTION,
            record.promotion_id,
            {
                "proposal_id": record.proposal_id,
                "accepted": record.accepted,
            },
        )
    if isinstance(record, WorkbenchLease):
        return (
            event_kind.LEASE_RECORDED,
            subject_kind.LEASE,
            record.lease_id,
            {
                "status": record.status.value,
                "requested_for_run_id": record.requested_for_run_id,
            },
        )
    if isinstance(record, (AutomationDefinition, AutomationRunReceipt)):
        return (
            event_kind.AUTOMATION_SIMULATED,
            subject_kind.AUTOMATION,
            record.automation_id,
            {
                "runtime": type(record).__name__,
            },
        )
    if isinstance(record, MonitoringSignal):
        return (
            event_kind.MONITORING_SIGNAL,
            subject_kind.MONITORING_SIGNAL,
            record.signal_id,
            {
                "kind": str(record.kind),
                "severity": str(record.severity),
                "run_id": record.run_id,
            },
        )
    if isinstance(record, ImprovementProposal):
        return (
            event_kind.IMPROVEMENT_DECISION,
            subject_kind.IMPROVEMENT,
            record.proposal_id,
            {
                "kind": record.kind.value,
                "mode": record.mode.value,
            },
        )
    if isinstance(record, GovernanceDecision):
        return (
            event_kind.IMPROVEMENT_DECISION,
            subject_kind.IMPROVEMENT,
            record.proposal_id,
            {
                "approved": record.approved,
                "blockers": tuple(record.blockers),
            },
        )
    raise error_cls(f"unsupported Workbench record type: {type(record).__name__}")


__all__ = ["WEAVING_PUBLIC_EXPORTS", "_record_shape"]
