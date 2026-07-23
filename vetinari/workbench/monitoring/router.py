"""Alert routing for assessed production AI monitoring signals."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from vetinari.agents.contracts import OutcomeSignal, Provenance
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.types import AgentType, EvidenceBasis, ShardKind
from vetinari.workbench.evals import EvalKind, EvalResult, EvalScore
from vetinari.workbench.failure_intelligence import FailedRunContext, FailureIntelligence
from vetinari.workbench.metadata_spine import WorkbenchSpine
from vetinari.workbench.proposals import ProposalGate, ProposalStatus, WorkbenchProposal, WorkbenchProposalKind

from .signals import MonitoringSignal, MonitoringSignalKind, assess_signal

logger = logging.getLogger(__name__)


_DEFAULT_ASSET_REVISION = "monitoring"


class _SpineLike(Protocol):
    def append_eval(self, eval_result: EvalResult) -> None:
        """Append an evaluation result to the metadata spine."""
        ...

    def append_proposal(self, proposal: WorkbenchProposal) -> None:
        """Append a proposal record to the metadata spine."""
        ...


class _FailureIntelligenceLike(Protocol):
    def classify(self, context: FailedRunContext) -> object:
        """Classify a failed run context for monitoring."""
        ...

    def record_autopsy(self, result: object) -> object:
        """Persist an autopsy result from failure intelligence."""
        ...


class _ReceiptStoreLike(Protocol):
    def append(self, receipt: WorkReceipt) -> None:
        """Append a monitoring receipt."""
        ...


class MonitoringRouteDestination(str, Enum):
    """Alert destinations used by production AI monitoring."""

    FAILURE_INTELLIGENCE = "failure_intelligence"
    EVAL_RESULT = "eval"
    PROPOSAL = "proposal"
    OPERATOR_NOTIFICATION = "operator_notification"
    NO_OP = "no_op"
    NO_OP_DEGRADED = "no_op_degraded"


@dataclass(frozen=True, slots=True)
class MonitoringRouteResult:
    """Result of routing one monitoring alert."""

    signal_id: str
    destination: MonitoringRouteDestination
    passed: bool
    degraded: bool
    artifact_id: str
    blockers: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MonitoringRouteResult(signal_id={self.signal_id!r}, destination={self.destination!r}, passed={self.passed!r})"


class MonitoringAlertRouter:
    """Routes monitoring signals through existing Workbench stores."""

    def __init__(
        self,
        *,
        spine: _SpineLike | None = None,
        failure_intelligence: _FailureIntelligenceLike | None = None,
        receipt_store: _ReceiptStoreLike | None = None,
    ) -> None:
        self._spine = spine if spine is not None else WorkbenchSpine()
        self._failure_intelligence = failure_intelligence if failure_intelligence is not None else FailureIntelligence()
        self._receipt_store = receipt_store if receipt_store is not None else WorkReceiptStore()

    def route(self, signal: MonitoringSignal) -> MonitoringRouteResult:
        """Route one signal, returning degraded results for bad evidence or store failures.

        Returns:
            MonitoringRouteResult value produced by route().
        """
        assessment = assess_signal(signal)
        if not assessment.passed or assessment.degraded:
            return MonitoringRouteResult(
                signal_id=signal.signal_id,
                destination=MonitoringRouteDestination.NO_OP_DEGRADED,
                passed=False,
                degraded=True,
                artifact_id="",
                blockers=tuple(reason.value for reason in assessment.blockers),
                evidence_refs=signal.evidence_refs,
            )
        if not assessment.alerting:
            return MonitoringRouteResult(
                signal_id=signal.signal_id,
                destination=MonitoringRouteDestination.NO_OP,
                passed=True,
                degraded=False,
                artifact_id="",
                blockers=(),
                evidence_refs=signal.evidence_refs,
            )

        destination = _destination_for(signal.kind)
        try:
            artifact_id = self._append_destination(destination, signal)
        except Exception as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return MonitoringRouteResult(
                signal_id=signal.signal_id,
                destination=destination,
                passed=False,
                degraded=True,
                artifact_id="",
                blockers=(f"{type(exc).__name__}: {exc}",),
                evidence_refs=signal.evidence_refs,
            )
        return MonitoringRouteResult(
            signal_id=signal.signal_id,
            destination=destination,
            passed=True,
            degraded=False,
            artifact_id=artifact_id,
            blockers=(),
            evidence_refs=signal.evidence_refs,
        )

    def _append_destination(self, destination: MonitoringRouteDestination, signal: MonitoringSignal) -> str:
        if destination is MonitoringRouteDestination.EVAL_RESULT:
            eval_result = _eval_result_for(signal)
            self._spine.append_eval(eval_result)
            return eval_result.eval_id
        if destination is MonitoringRouteDestination.PROPOSAL:
            proposal = _proposal_for(signal)
            self._spine.append_proposal(proposal)
            return proposal.proposal_id
        if destination is MonitoringRouteDestination.FAILURE_INTELLIGENCE:
            autopsy = self._failure_intelligence.classify(_failed_run_context_for(signal))
            recorded = self._failure_intelligence.record_autopsy(autopsy)
            return str(getattr(recorded, "autopsy_id", ""))
        if destination is MonitoringRouteDestination.OPERATOR_NOTIFICATION:
            receipt = _operator_receipt_for(signal)
            self._receipt_store.append(receipt)
            return receipt.receipt_id
        return ""


def _destination_for(kind: MonitoringSignalKind | str) -> MonitoringRouteDestination:
    raw_kind = kind.value if isinstance(kind, Enum) else kind
    normalized = kind if isinstance(kind, MonitoringSignalKind) else MonitoringSignalKind(raw_kind)
    if normalized in {
        MonitoringSignalKind.DATA_DRIFT,
        MonitoringSignalKind.EMBEDDING_SHIFT,
        MonitoringSignalKind.QUALITY_REGRESSION,
        MonitoringSignalKind.RETRIEVAL_FAILURE,
    }:
        return MonitoringRouteDestination.EVAL_RESULT
    if normalized in {
        MonitoringSignalKind.HALLUCINATION,
        MonitoringSignalKind.TOXICITY,
        MonitoringSignalKind.PII_PHI,
        MonitoringSignalKind.TOOL_CALL_FAILURE,
        MonitoringSignalKind.AGENT_STATE_ANOMALY,
    }:
        return MonitoringRouteDestination.FAILURE_INTELLIGENCE
    if normalized in {
        MonitoringSignalKind.PROMPT_CHANGE,
        MonitoringSignalKind.MODEL_CHANGE,
        MonitoringSignalKind.PROVIDER_CHANGE,
    }:
        return MonitoringRouteDestination.PROPOSAL
    return MonitoringRouteDestination.OPERATOR_NOTIFICATION


def _eval_result_for(signal: MonitoringSignal) -> EvalResult:
    return EvalResult(
        eval_id=f"monitoring-eval-{signal.signal_id}",
        kind=EvalKind.LIVE_TRACE_DERIVED,
        run_id=signal.run_id,
        asset_id=signal.asset_id,
        asset_revision=_DEFAULT_ASSET_REVISION,
        scores=(
            EvalScore(
                metric_name=f"monitoring.{MonitoringSignalKind(signal.kind).value}",
                value=float(signal.score),
                threshold=float(signal.threshold),
                passed=False,
            ),
        ),
        captured_at_utc=signal.captured_at_utc,
        notes=f"monitoring signal {signal.signal_id}; evidence={','.join(signal.evidence_refs)}",
    )


def _proposal_for(signal: MonitoringSignal) -> WorkbenchProposal:
    kind = MonitoringSignalKind(signal.kind)
    proposal_kind = (
        WorkbenchProposalKind.PROMPT_VERSION
        if kind is MonitoringSignalKind.PROMPT_CHANGE
        else WorkbenchProposalKind.MODEL_DEFAULT
    )
    return WorkbenchProposal(
        proposal_id=f"monitoring-proposal-{signal.signal_id}",
        kind=proposal_kind,
        status=ProposalStatus.OPEN,
        affected_assets=(signal.asset_id,),
        affected_revisions=((signal.asset_id, _DEFAULT_ASSET_REVISION),),
        pre_promotion_evals=(),
        gate=ProposalGate(
            provenance_present=bool(signal.evidence_refs),
            eval_present=False,
            rollback_plan_present=False,
            blockers=(f"review {kind.value} monitoring signal {signal.signal_id}",),
        ),
        attached_outcome=None,
        opened_at_utc=signal.captured_at_utc,
        closed_at_utc="",
        notes=f"Production AI monitoring routed {kind.value} {signal.signal_id}: {','.join(signal.evidence_refs)}",
    )


def _failed_run_context_for(signal: MonitoringSignal) -> FailedRunContext:
    kind = MonitoringSignalKind(signal.kind)
    return FailedRunContext(
        project_id=signal.project_id,
        run_id=signal.run_id,
        status="failed",
        output_summary=f"production monitoring signal {kind.value} crossed threshold",
        error_message=_failure_message_for(kind, signal),
        hallucinated_tool_names=(signal.routing_hint,) if kind is MonitoringSignalKind.HALLUCINATION else (),
        unavailable_tool_names=(signal.routing_hint,) if kind is MonitoringSignalKind.TOOL_CALL_FAILURE else (),
        policy_rejection=kind.value if kind in {MonitoringSignalKind.TOXICITY, MonitoringSignalKind.PII_PHI} else None,
        eval_failures=(signal.signal_id,),
        evidence_refs=(signal.signal_id, *signal.evidence_refs),
    )


def _failure_message_for(kind: MonitoringSignalKind, signal: MonitoringSignal) -> str:
    if kind is MonitoringSignalKind.AGENT_STATE_ANOMALY:
        return f"routing wrong agent state transition anomaly for {signal.signal_id}"
    if kind in {MonitoringSignalKind.TOXICITY, MonitoringSignalKind.PII_PHI}:
        return f"policy conflict: {kind.value}"
    if kind is MonitoringSignalKind.TOOL_CALL_FAILURE:
        return "runtime unavailable tool call failure"
    if kind is MonitoringSignalKind.HALLUCINATION:
        return "hallucinated tool ability"
    return f"{kind.value} monitoring failure"


def _operator_receipt_for(signal: MonitoringSignal) -> WorkReceipt:
    now = datetime.now(timezone.utc).isoformat()
    return WorkReceipt(
        project_id=signal.project_id,
        agent_id="workbench-production-ai-monitoring",
        agent_type=AgentType.WORKBENCH,
        kind=WorkReceiptKind.SPINE_EVENT,
        outcome=OutcomeSignal(
            passed=False,
            score=0.0,
            basis=EvidenceBasis.TOOL_EVIDENCE,
            provenance=Provenance(
                source="vetinari.workbench.monitoring",
                timestamp_utc=now,
                tool_name="production_ai_monitoring",
            ),
            issues=(f"{MonitoringSignalKind(signal.kind).value} alert {signal.signal_id}",),
            kind=ShardKind.STANDARD,
        ),
        started_at_utc=now,
        finished_at_utc=now,
        inputs_summary=f"monitoring signal {signal.signal_id}",
        outputs_summary=f"operator notification for {MonitoringSignalKind(signal.kind).value}",
        awaiting_user=True,
        awaiting_reason=f"production_ai_monitoring:{MonitoringSignalKind(signal.kind).value}:{signal.signal_id}",
    )


__all__ = [
    "MonitoringAlertRouter",
    "MonitoringRouteDestination",
    "MonitoringRouteResult",
]
