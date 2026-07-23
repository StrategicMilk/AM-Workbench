"""Step Promotion-Gate of the workbench self-improvement pipeline.

Walks pending WorkbenchProposal rows, applies a deterministic
eval/provenance/rollback/taint gate, persists approve or reject as a
status-transitioned WorkbenchProposal append plus a Promotion append plus one
SPINE_EVENT WorkReceipt. Reads outputs/projects/{project_id}/plan_feedback.jsonl
through NonGoalStore.list_plan_feedback so structured planner-reject signals
surface as proposal blockers.

Side effects are limited to PromotionInboxService.append_decision: it calls
WorkbenchSpine.append_proposal and WorkbenchSpine.record_promotion, then emits
one SPINE_EVENT WorkReceipt via WorkReceiptStore.append. Gate evaluation reads
the spine and plan-feedback log only.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.agents.contracts import OutcomeSignal, Provenance
from vetinari.planning.non_goals import NonGoalStore
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.security.fail_closed import sanitize_untrusted_text
from vetinari.types import AgentType, EvidenceBasis
from vetinari.workbench.assets import WorkbenchAsset
from vetinari.workbench.evals import EvalKind, EvalResult
from vetinari.workbench.metadata_spine import WorkbenchSpine, WorkbenchSpineCorrupt
from vetinari.workbench.proposals import Promotion, ProposalStatus, WorkbenchProposal

logger = logging.getLogger(__name__)


_GATE_BLOCKER_MISSING_PROVENANCE = "missing_provenance"
_GATE_BLOCKER_MISSING_EVAL = "missing_eval_evidence"
_GATE_BLOCKER_FAILED_EVAL = "failing_eval_score"
_GATE_BLOCKER_STALE_TAINT = "stale_asset_taint"
_GATE_BLOCKER_MISSING_ROLLBACK = "missing_rollback_plan"
_GATE_BLOCKER_PLAN_FEEDBACK_REFUSED = "plan_feedback_refused"
_GATE_BLOCKER_NON_OPEN_PROPOSAL = "proposal_not_open"
_GATE_BLOCKER_JUDGE_ONLY_EVAL = "judge_only_evidence"
_RECEIPT_ACTOR = AgentType.WORKBENCH
_ROLLBACK_NOTE_KEYS = ("rollback_plan=", "rollback_ref=", "rollback_artifact=")


class PromotionInboxError(Exception):
    """Raised when a promotion cannot be safely listed or decided."""

    def __init__(self, reason: str, *, proposal_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.proposal_id = proposal_id

    def __str__(self) -> str:
        if self.proposal_id is None:
            return f"PromotionInboxError: {self.reason}"
        return f"PromotionInboxError: {self.reason} (proposal_id={self.proposal_id})"


@dataclass(frozen=True, slots=True)
class PromotionGateOutcome:
    """Immutable deterministic promotion-gate verdict."""

    passed: bool
    blockers: tuple[str, ...]
    evidence: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """Rendered approve/reject decision returned to HTTP callers."""

    proposal_id: str
    accepted: bool
    decided_by: str
    rationale: str
    decided_at_utc: str

    def __repr__(self) -> str:
        return (
            f"PromotionDecision(proposal_id={self.proposal_id!r}, "
            f"accepted={self.accepted!r}, decided_by={self.decided_by!r})"
        )


def _sanitize_project_id(project_id: str) -> str:
    """Reject project ids that could escape project-scoped output roots."""
    if "/" in project_id or "\\" in project_id or ".." in project_id or project_id != Path(project_id).name:
        raise ValueError("project_id contains forbidden characters")
    return project_id


class PromotionInboxService:
    """Deterministic promotion gate over the Workbench metadata spine."""

    def __init__(
        self,
        spine: WorkbenchSpine,
        non_goal_store: NonGoalStore,
        *,
        receipt_store: WorkReceiptStore | None = None,
    ) -> None:
        self._spine = spine
        self._non_goal_store = non_goal_store
        self._receipt_store = receipt_store if receipt_store is not None else WorkReceiptStore()
        self._decision_lock = threading.RLock()

    def list_pending_proposals(self, project_id: str) -> list[tuple[WorkbenchProposal, PromotionGateOutcome]]:
        """Return open proposals paired with their current gate verdict.

        Args:
            project_id: Project whose plan-feedback blockers should be read.

        Returns:
            Open proposals that do not already have a decision transition,
            paired with immutable gate verdicts.

        Raises:
            PromotionInboxError: If the project id is unsafe or the spine is unreadable.
        """
        safe_project_id = _sanitize_project_id(project_id)
        try:
            proposals = self._spine.list_proposals(status=ProposalStatus.OPEN)
        except Exception as exc:
            raise PromotionInboxError("spine unreadable while listing proposals") from exc
        return [
            (proposal, self.evaluate_gate(proposal, project_id=safe_project_id))
            for proposal in proposals
            if not self._has_decision_transition(proposal.proposal_id)
        ]

    def evaluate_gate(self, proposal: WorkbenchProposal, *, project_id: str) -> PromotionGateOutcome:
        """Walk a proposal's evidence graph and return a fail-closed verdict.

        Args:
            proposal: Proposal row to evaluate.
            project_id: Project whose plan-feedback blockers should be read.

        Returns:
            A deterministic verdict containing blockers and evidence counts.

        Raises:
            PromotionInboxError: If referenced spine or plan-feedback state cannot be read.
        """
        safe_project_id = _sanitize_project_id(project_id)
        blockers: list[str] = list(proposal.gate.blockers)
        evidence: dict[str, Any] = {
            "proposal_id": proposal.proposal_id,
            "proposal_kind": proposal.kind.value,
            "affected_asset_count": len(proposal.affected_assets),
        }

        matched_assets = self._match_assets(proposal)
        affected_revisions = set(proposal.affected_revisions)
        matched_revisions = {_asset_revision(asset) for asset in matched_assets}
        missing_revisions = affected_revisions - matched_revisions
        provenance_assets = [asset for asset in matched_assets if str(asset.provenance.get("source", "")).strip()]
        evidence["asset_revision_count"] = len(matched_assets)
        evidence["asset_revision_missing_count"] = len(missing_revisions)
        evidence["provenance_asset_count"] = len(provenance_assets)

        if proposal.status is not ProposalStatus.OPEN:
            blockers.append(_GATE_BLOCKER_NON_OPEN_PROPOSAL)
        if missing_revisions or len(provenance_assets) != len(affected_revisions):
            blockers.append(_GATE_BLOCKER_MISSING_PROVENANCE)

        matched_evals = self._match_evals(proposal)
        failing_evals = [result for result in matched_evals if any(not score.passed for score in result.scores)]
        judge_only_evals = [result for result in matched_evals if result.kind is EvalKind.JUDGE_ONLY]
        evidence["eval_count_matched"] = len(matched_evals)
        evidence["eval_count_failing"] = len(failing_evals)
        if not proposal.pre_promotion_evals or not matched_evals:
            blockers.append(_GATE_BLOCKER_MISSING_EVAL)
        if failing_evals:
            blockers.append(_GATE_BLOCKER_FAILED_EVAL)
        if judge_only_evals:
            blockers.append(_GATE_BLOCKER_JUDGE_ONLY_EVAL)

        taint_count = self._count_taints(matched_assets)
        evidence["taint_count"] = taint_count
        if taint_count > 0 or missing_revisions:
            blockers.append(_GATE_BLOCKER_STALE_TAINT)

        rollback_plan_present = _proposal_has_rollback_evidence(proposal)
        evidence["rollback_plan_present"] = rollback_plan_present
        if not rollback_plan_present:
            blockers.append(_GATE_BLOCKER_MISSING_ROLLBACK)

        feedback = self._matching_plan_feedback(safe_project_id, proposal.proposal_id)
        evidence["plan_feedback_match_count"] = len(feedback)
        reasons = tuple(str(row.get("reason_code", "")) for row in feedback if row.get("reason_code"))
        evidence["plan_feedback_reasons"] = reasons
        if feedback:
            blockers.append(_GATE_BLOCKER_PLAN_FEEDBACK_REFUSED)

        unique_blockers = tuple(dict.fromkeys(blockers))
        return PromotionGateOutcome(passed=not unique_blockers, blockers=unique_blockers, evidence=evidence)

    def append_decision(
        self,
        proposal_id: str,
        *,
        accepted: bool,
        decided_by: str,
        rationale: str,
        project_id: str,
    ) -> PromotionDecision:
        """Persist an approve or reject verdict as append-only spine writes.

        Args:
            proposal_id: Original open proposal id.
            accepted: True for approve, False for reject.
            decided_by: Operator identity.
            rationale: Human-readable decision rationale.
            project_id: Project whose spine and receipt logs are written.

        Returns:
            The rendered decision returned to route callers.

        Raises:
            PromotionInboxError: If validation, gate evaluation, or append-only persistence fails.
        """
        safe_project_id, decided_by, rationale = _validated_decision_inputs(
            project_id=project_id,
            proposal_id=proposal_id,
            accepted=accepted,
            decided_by=decided_by,
            rationale=rationale,
        )

        with self._decision_lock:
            proposal = self._latest_open_proposal(proposal_id)
            if self._has_decision_transition(proposal_id):
                raise PromotionInboxError(
                    "proposal already accepted/rejected; cannot re-decide", proposal_id=proposal_id
                )
            verdict = self.evaluate_gate(proposal, project_id=safe_project_id)
            if accepted and not verdict.passed:
                raise PromotionInboxError(
                    f"gate refused approve: blockers={list(verdict.blockers)}",
                    proposal_id=proposal_id,
                )

            decided_at_utc = datetime.now(timezone.utc).isoformat()
            promotion_id = self._promotion_id(proposal_id)
            transitioned = self._transitioned_proposal(
                proposal=proposal,
                accepted=accepted,
                decided_by=decided_by,
                rationale=rationale,
                verdict=verdict,
                decided_at_utc=decided_at_utc,
            )
            promotion = self._promotion_record(
                proposal_id, promotion_id, accepted, decided_at_utc, decided_by, rationale
            )
            self._append_decision_records(proposal_id, transitioned, promotion)

        self._emit_decision_receipt(
            project_id=safe_project_id,
            proposal_id=proposal_id,
            promotion_id=promotion_id,
            accepted=accepted,
            decided_by=decided_by,
        )
        return PromotionDecision(
            proposal_id=proposal_id,
            accepted=accepted,
            decided_by=decided_by,
            rationale=rationale,
            decided_at_utc=decided_at_utc,
        )

    def _transitioned_proposal(
        self,
        *,
        proposal: WorkbenchProposal,
        accepted: bool,
        decided_by: str,
        rationale: str,
        verdict: PromotionGateOutcome,
        decided_at_utc: str,
    ) -> WorkbenchProposal:
        new_status = ProposalStatus.ACCEPTED if accepted else ProposalStatus.REJECTED
        return WorkbenchProposal(
            proposal_id=self._decision_proposal_id(proposal.proposal_id),
            kind=proposal.kind,
            status=new_status,
            affected_assets=proposal.affected_assets,
            affected_revisions=proposal.affected_revisions,
            pre_promotion_evals=proposal.pre_promotion_evals,
            gate=proposal.gate,
            attached_outcome=proposal.attached_outcome,
            opened_at_utc=proposal.opened_at_utc,
            closed_at_utc=decided_at_utc,
            notes=(
                f"decision_for={proposal.proposal_id}; decided_by={decided_by}; "
                f"gate_passed={verdict.passed}; gate_blockers={list(verdict.blockers)}; "
                f"rationale={rationale}"
            ),
        )

    @staticmethod
    def _promotion_record(
        proposal_id: str,
        promotion_id: str,
        accepted: bool,
        decided_at_utc: str,
        decided_by: str,
        rationale: str,
    ) -> Promotion:
        return Promotion(
            promotion_id=promotion_id,
            proposal_id=proposal_id,
            accepted=accepted,
            decided_at_utc=decided_at_utc,
            decided_by=decided_by,
            rationale=rationale,
        )

    def _append_decision_records(
        self,
        proposal_id: str,
        transitioned: WorkbenchProposal,
        promotion: Promotion,
    ) -> None:
        try:
            self._spine.append_proposal(transitioned)
            self._spine.record_promotion(promotion)
        except WorkbenchSpineCorrupt as exc:
            if "duplicate proposal" in str(exc) or "duplicate promotion" in str(exc):
                raise PromotionInboxError(
                    "proposal already accepted/rejected; cannot re-decide",
                    proposal_id=proposal_id,
                ) from exc
            raise PromotionInboxError(str(exc), proposal_id=proposal_id) from exc

    def _latest_open_proposal(self, proposal_id: str) -> WorkbenchProposal:
        try:
            matches = [
                proposal
                for proposal in self._spine.list_proposals(status=ProposalStatus.OPEN)
                if proposal.proposal_id == proposal_id
            ]
        except Exception as exc:
            raise PromotionInboxError("spine unreadable while loading proposal", proposal_id=proposal_id) from exc
        if not matches:
            raise PromotionInboxError("proposal not found in spine", proposal_id=proposal_id)
        return matches[-1]

    def _has_decision_transition(self, proposal_id: str) -> bool:
        transition_id = self._decision_proposal_id(proposal_id)
        try:
            return any(proposal.proposal_id == transition_id for proposal in self._spine.list_proposals())
        except Exception as exc:
            raise PromotionInboxError(
                "spine unreadable while checking decision state", proposal_id=proposal_id
            ) from exc

    def _match_evals(self, proposal: WorkbenchProposal) -> list[EvalResult]:
        cited_ids = {result.eval_id for result in proposal.pre_promotion_evals}
        if not cited_ids:
            return []
        try:
            return [result for result in self._spine.list_evals() if result.eval_id in cited_ids]
        except Exception as exc:
            raise PromotionInboxError("spine evals unreadable", proposal_id=proposal.proposal_id) from exc

    def _match_assets(self, proposal: WorkbenchProposal) -> list[WorkbenchAsset]:
        affected = set(proposal.affected_revisions)
        try:
            assets = self._spine.list_assets()
        except Exception as exc:
            raise PromotionInboxError("spine assets unreadable", proposal_id=proposal.proposal_id) from exc
        return [asset for asset in assets if _asset_revision(asset) in affected]

    @staticmethod
    def _count_taints(assets: list[WorkbenchAsset]) -> int:
        return sum(len(asset.taints) for asset in assets)

    def _matching_plan_feedback(self, project_id: str, proposal_id: str) -> list[dict[str, Any]]:
        try:
            rows = self._non_goal_store.list_plan_feedback(project_id)
        except Exception as exc:
            raise PromotionInboxError("plan feedback unreadable", proposal_id=proposal_id) from exc
        return [
            row
            for row in rows
            if str(row.get("plan_id", "")) == proposal_id
            and str(row.get("decision", "")).upper() in {"REFUSE", "REJECT"}
        ]

    def _emit_decision_receipt(
        self,
        *,
        project_id: str,
        proposal_id: str,
        promotion_id: str,
        accepted: bool,
        decided_by: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        receipt = WorkReceipt(
            project_id=project_id,
            agent_id=f"workbench-promotion:{promotion_id}",
            agent_type=_RECEIPT_ACTOR,
            kind=WorkReceiptKind.SPINE_EVENT,
            outcome=OutcomeSignal(
                passed=True,
                score=1.0 if accepted else 0.0,
                basis=EvidenceBasis.TOOL_EVIDENCE,
                provenance=Provenance(
                    source="vetinari.workbench.promotion_inbox",
                    timestamp_utc=now,
                    tool_name="workbench_promotion_inbox",
                ),
            ),
            started_at_utc=now,
            finished_at_utc=now,
            inputs_summary=f"promotion decide: proposal={proposal_id} accepted={accepted}",
            outputs_summary=f"promotion_id={promotion_id} decided_by={decided_by}",
        )
        self._receipt_store.append(receipt)

    @staticmethod
    def _decision_proposal_id(proposal_id: str) -> str:
        return f"{proposal_id}:decision"

    @staticmethod
    def _promotion_id(proposal_id: str) -> str:
        return f"prom:{proposal_id}"


def _asset_revision(asset: WorkbenchAsset) -> tuple[str, str]:
    return (asset.asset_id, asset.revision)


def _validated_decision_inputs(
    *,
    project_id: str,
    proposal_id: str,
    accepted: bool,
    decided_by: str,
    rationale: str,
) -> tuple[str, str, str]:
    safe_project_id = _sanitize_project_id(project_id)
    try:
        normalized_decider = sanitize_untrusted_text(decided_by, max_length=160)
    except Exception as exc:
        raise PromotionInboxError("decided_by contains unsafe text", proposal_id=proposal_id) from exc
    try:
        normalized_rationale = sanitize_untrusted_text(rationale, max_length=4_000) if rationale.strip() else ""
    except Exception as exc:
        raise PromotionInboxError("rationale contains unsafe text", proposal_id=proposal_id) from exc
    if not normalized_decider:
        raise PromotionInboxError("decided_by is required", proposal_id=proposal_id)
    if not accepted and not normalized_rationale:
        raise PromotionInboxError("rationale is required for REJECT", proposal_id=proposal_id)
    return safe_project_id, normalized_decider, normalized_rationale


def _proposal_has_rollback_evidence(proposal: WorkbenchProposal) -> bool:
    notes = proposal.notes
    for key in _ROLLBACK_NOTE_KEYS:
        if key not in notes:
            continue
        value = notes.split(key, 1)[1].split(";", 1)[0].strip()
        if value and value.lower() not in {"false", "missing", "n/a", "none", "null"}:
            return True
    return False


def evaluate_self_improvement_default_gate(
    proposal: Any,
    *,
    now_utc: datetime | None = None,
) -> PromotionGateOutcome:
    """Adapt self-improvement governance decisions into promotion-inbox outcomes.

    Returns:
        PromotionGateOutcome value produced by evaluate_self_improvement_default_gate().
    """
    from vetinari.workbench.self_improvement import evaluate_improvement_proposal

    decision = evaluate_improvement_proposal(proposal, now_utc=now_utc)
    return PromotionGateOutcome(
        passed=decision.approved,
        blockers=decision.blockers,
        evidence=decision.evidence,
    )


__all__ = [
    "PromotionDecision",
    "PromotionGateOutcome",
    "PromotionInboxError",
    "PromotionInboxService",
    "evaluate_self_improvement_default_gate",
]
