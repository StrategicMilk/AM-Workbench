"""Fail-closed approval diffs for governed Workbench promotions.

The module is import-safe and side-effect free. Callers provide already-read
Workbench records and evidence references; this package does not own storage.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, TypeVar

from vetinari.workbench.evals import EvalResult
from vetinari.workbench.proposals import WorkbenchProposal, WorkbenchProposalKind
from vetinari.workbench.traces import WorkbenchTrace


class ApprovalDiffRejected(PermissionError):
    """Raised when a promotion mutation is attempted without a clean diff."""


class ApprovalDiffTarget(str, Enum):
    """Promotion classes that must carry one governed approval diff."""

    MODEL_DEFAULT = "model_default"
    PROMPT_PROMOTION = "prompt_promotion"
    DATASET_PROMOTION = "dataset_promotion"
    ROUTE_POLICY_CHANGE = "route_policy_change"
    TRAINING_RECIPE_ACTIVATION = "training_recipe_activation"
    AUTOMATION_PROMOTION = "automation_promotion"


class ApprovalDiffStatus(str, Enum):
    """Review status for an approval diff."""

    BLOCKED = "blocked"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class DiffDimension(str, Enum):
    """Dimensions reviewers must see before approving a promotion."""

    OUTPUT_BEHAVIOR = "output_behavior"
    PROMPT = "prompt"
    MODEL = "model"
    ROUTE = "route"
    RETRIEVAL = "retrieval"
    TOOLS = "tools"
    COST = "cost"
    LATENCY = "latency"
    SAFETY = "safety"
    EVAL_SCORE = "eval_score"
    AFFECTED_ASSETS = "affected_assets"
    ROLLBACK_TARGET = "rollback_target"
    POLICY_GATES = "policy_gates"


APPROVAL_DIFF_REQUIRED_DIMENSIONS: frozenset[DiffDimension] = frozenset(DiffDimension)


class ApprovalDiffGate(str, Enum):
    """Fail-closed gate categories used by the approval diff evaluator."""

    PROVENANCE = "provenance"
    CONFIDENCE = "confidence"
    SAFETY = "safety"
    BUDGET = "budget"
    AUTHORITY = "authority"
    PERSISTED_STATE = "persisted_state"
    ROLLBACK = "rollback"
    POLICY = "policy"


@dataclass(frozen=True, slots=True)
class ApprovalDiffEvidenceRef:
    """Source-backed evidence reference used in an approval diff."""

    ref_id: str
    kind: str
    source: str
    summary: str

    def __post_init__(self) -> None:
        _require_text(self.ref_id, "ref_id")
        _require_text(self.kind, "kind")
        _require_text(self.source, "source")
        _require_text(self.summary, "summary")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ApprovalDiffEvidenceRef(ref_id={self.ref_id!r}, kind={self.kind!r}, source={self.source!r})"


@dataclass(frozen=True, slots=True)
class DiffEntry:
    """Before/after row for one required approval dimension."""

    dimension: DiffDimension
    before: str
    after: str
    evidence_refs: tuple[str, ...]
    risk: str = "medium"

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, DiffDimension):
            raise ValueError("dimension must be DiffDimension")
        _require_text(self.before, "before")
        _require_text(self.after, "after")
        _require_text(self.risk, "risk")
        _require_text_tuple(self.evidence_refs, "evidence_refs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension.value,
            "before": self.before,
            "after": self.after,
            "evidence_refs": list(self.evidence_refs),
            "risk": self.risk,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DiffEntry(dimension={self.dimension!r}, before={self.before!r}, after={self.after!r})"


@dataclass(frozen=True, slots=True)
class ApprovalDiffDecision:
    """Reviewer decision attached to an approval diff."""

    status: ApprovalDiffStatus
    decided_by: str
    decided_at_utc: str
    rationale: str
    authority_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.status, ApprovalDiffStatus):
            raise ValueError("status must be ApprovalDiffStatus")
        if self.status in {ApprovalDiffStatus.APPROVED, ApprovalDiffStatus.REJECTED}:
            _require_text(self.decided_by, "decided_by")
            _require_text(self.decided_at_utc, "decided_at_utc")
            _require_text(self.rationale, "rationale")
            _require_text_tuple(self.authority_refs, "authority_refs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "decided_by": self.decided_by,
            "decided_at_utc": self.decided_at_utc,
            "rationale": self.rationale,
            "authority_refs": list(self.authority_refs),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ApprovalDiffDecision(status={self.status!r}, decided_by={self.decided_by!r}, decided_at_utc={self.decided_at_utc!r})"


@dataclass(frozen=True, slots=True)
class ApprovalDiff:
    """Reusable approval diff connecting evidence, review, and promotion."""

    diff_id: str
    target: ApprovalDiffTarget
    proposal_id: str
    project_id: str
    baseline_ref: str
    candidate_ref: str
    entries: tuple[DiffEntry, ...]
    evidence: tuple[ApprovalDiffEvidenceRef, ...]
    provenance_refs: tuple[str, ...]
    confidence: float
    safety_verdict: str
    budget_verdict: str
    policy_gate_refs: tuple[str, ...]
    rollback_target_ref: str
    affected_assets: tuple[str, ...]
    persisted_state_refs: tuple[str, ...]
    human_review_refs: tuple[str, ...] = ()
    promotion_decision_refs: tuple[str, ...] = ()
    status: ApprovalDiffStatus = ApprovalDiffStatus.READY_FOR_REVIEW
    decision: ApprovalDiffDecision | None = None

    def __post_init__(self) -> None:
        _require_text(self.diff_id, "diff_id")
        _require_text(self.proposal_id, "proposal_id")
        _require_text(self.project_id, "project_id")
        _require_text(self.baseline_ref, "baseline_ref")
        _require_text(self.candidate_ref, "candidate_ref")
        if not isinstance(self.target, ApprovalDiffTarget):
            raise ValueError("target must be ApprovalDiffTarget")
        if not isinstance(self.status, ApprovalDiffStatus):
            raise ValueError("status must be ApprovalDiffStatus")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        if not self.entries:
            raise ValueError("entries must be non-empty")
        if not self.evidence:
            raise ValueError("evidence must be non-empty")
        _require_optional_text_tuple(self.provenance_refs, "provenance_refs")
        _require_text(self.safety_verdict, "safety_verdict")
        _require_text(self.budget_verdict, "budget_verdict")
        _require_optional_text_tuple(self.policy_gate_refs, "policy_gate_refs")
        _require_text(self.rollback_target_ref, "rollback_target_ref")
        _require_optional_text_tuple(self.affected_assets, "affected_assets")
        _require_optional_text_tuple(self.persisted_state_refs, "persisted_state_refs")
        if self.human_review_refs:
            _require_text_tuple(self.human_review_refs, "human_review_refs")
        if self.promotion_decision_refs:
            _require_text_tuple(self.promotion_decision_refs, "promotion_decision_refs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "diff_id": self.diff_id,
            "target": self.target.value,
            "proposal_id": self.proposal_id,
            "project_id": self.project_id,
            "baseline_ref": self.baseline_ref,
            "candidate_ref": self.candidate_ref,
            "entries": [entry.to_dict() for entry in self.entries],
            "evidence": [ref.to_dict() for ref in self.evidence],
            "provenance_refs": list(self.provenance_refs),
            "confidence": self.confidence,
            "safety_verdict": self.safety_verdict,
            "budget_verdict": self.budget_verdict,
            "policy_gate_refs": list(self.policy_gate_refs),
            "rollback_target_ref": self.rollback_target_ref,
            "affected_assets": list(self.affected_assets),
            "persisted_state_refs": list(self.persisted_state_refs),
            "human_review_refs": list(self.human_review_refs),
            "promotion_decision_refs": list(self.promotion_decision_refs),
            "status": self.status.value,
            "decision": self.decision.to_dict() if self.decision else None,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ApprovalDiff(diff_id={self.diff_id!r}, target={self.target!r}, proposal_id={self.proposal_id!r})"


@dataclass(frozen=True, slots=True)
class ApprovalDiffReview:
    """Fail-closed evaluation result for an approval diff."""

    diff_id: str
    approved: bool
    status: ApprovalDiffStatus
    blockers: tuple[str, ...]
    evidence: Mapping[str, Any]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ApprovalDiffReview(diff_id={self.diff_id!r}, approved={self.approved!r}, status={self.status!r})"


T = TypeVar("T")


def build_approval_diff(
    *,
    diff_id: str,
    target: ApprovalDiffTarget,
    proposal_id: str,
    project_id: str,
    baseline_ref: str,
    candidate_ref: str,
    entries: tuple[DiffEntry, ...],
    evidence: tuple[ApprovalDiffEvidenceRef, ...],
    provenance_refs: tuple[str, ...],
    confidence: float,
    safety_verdict: str,
    budget_verdict: str,
    policy_gate_refs: tuple[str, ...],
    rollback_target_ref: str,
    affected_assets: tuple[str, ...],
    persisted_state_refs: tuple[str, ...],
    human_review_refs: tuple[str, ...] = (),
    promotion_decision_refs: tuple[str, ...] = (),
    status: ApprovalDiffStatus = ApprovalDiffStatus.READY_FOR_REVIEW,
    decision: ApprovalDiffDecision | None = None,
) -> ApprovalDiff:
    """Construct an approval diff through the runtime validation path.

    Returns:
        Newly constructed approval diff value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    diff = ApprovalDiff(
        diff_id=diff_id,
        target=target,
        proposal_id=proposal_id,
        project_id=project_id,
        baseline_ref=baseline_ref,
        candidate_ref=candidate_ref,
        entries=entries,
        evidence=evidence,
        provenance_refs=provenance_refs,
        confidence=confidence,
        safety_verdict=safety_verdict,
        budget_verdict=budget_verdict,
        policy_gate_refs=policy_gate_refs,
        rollback_target_ref=rollback_target_ref,
        affected_assets=affected_assets,
        persisted_state_refs=persisted_state_refs,
        human_review_refs=human_review_refs,
        promotion_decision_refs=promotion_decision_refs,
        status=status,
        decision=decision,
    )
    review = evaluate_approval_diff(diff)
    if review.blockers and status is ApprovalDiffStatus.APPROVED:
        raise ApprovalDiffRejected(f"approval diff cannot be approved: {list(review.blockers)}")
    return diff


def build_approval_diff_from_proposal(
    proposal: WorkbenchProposal,
    *,
    project_id: str,
    baseline_ref: str,
    candidate_ref: str,
    entries: tuple[DiffEntry, ...],
    traces: tuple[WorkbenchTrace, ...],
    evals: tuple[EvalResult, ...],
    human_review_refs: tuple[str, ...],
    policy_gate_refs: tuple[str, ...],
    rollback_target_ref: str,
    persisted_state_refs: tuple[str, ...],
    confidence: float,
    safety_verdict: str = "passed",
    budget_verdict: str = "within_budget",
    promotion_decision_refs: tuple[str, ...] = (),
    status: ApprovalDiffStatus = ApprovalDiffStatus.READY_FOR_REVIEW,
    decision: ApprovalDiffDecision | None = None,
) -> ApprovalDiff:
    """Create an approval diff from already-loaded Workbench records.

    Returns:
        Newly constructed approval diff from proposal value.
    """
    return build_approval_diff(
        diff_id=f"approval-diff:{proposal.proposal_id}",
        target=_target_from_proposal_kind(proposal.kind),
        proposal_id=proposal.proposal_id,
        project_id=project_id,
        baseline_ref=baseline_ref,
        candidate_ref=candidate_ref,
        entries=entries,
        evidence=_evidence_from_proposal_records(
            traces=traces,
            evals=evals,
            human_review_refs=human_review_refs,
            promotion_decision_refs=promotion_decision_refs,
        ),
        provenance_refs=(f"proposal:{proposal.proposal_id}",),
        confidence=confidence,
        safety_verdict=safety_verdict,
        budget_verdict=budget_verdict,
        policy_gate_refs=policy_gate_refs,
        rollback_target_ref=rollback_target_ref,
        affected_assets=tuple(proposal.affected_assets),
        persisted_state_refs=persisted_state_refs,
        human_review_refs=human_review_refs,
        promotion_decision_refs=promotion_decision_refs,
        status=status,
        decision=decision,
    )


def _evidence_from_proposal_records(
    *,
    traces: tuple[WorkbenchTrace, ...],
    evals: tuple[EvalResult, ...],
    human_review_refs: tuple[str, ...],
    promotion_decision_refs: tuple[str, ...],
) -> tuple[ApprovalDiffEvidenceRef, ...]:
    return (
        *(
            ApprovalDiffEvidenceRef(
                trace.trace_id, "trace", "vetinari.workbench.traces", f"trace for run {trace.run_id}"
            )
            for trace in traces
        ),
        *(
            ApprovalDiffEvidenceRef(
                result.eval_id, "eval", "vetinari.workbench.evals", f"eval for asset {result.asset_id}"
            )
            for result in evals
        ),
        *(
            ApprovalDiffEvidenceRef(ref, "human_review", "workbench-review", "human review authority reference")
            for ref in human_review_refs
        ),
        *(
            ApprovalDiffEvidenceRef(ref, "promotion_decision", "workbench-promotion", "promotion decision reference")
            for ref in promotion_decision_refs
        ),
    )


def evaluate_approval_diff(diff: ApprovalDiff, *, min_confidence: float = 0.75) -> ApprovalDiffReview:
    """Return a fail-closed decision for one approval diff.

    Returns:
        ApprovalDiffReview value produced by evaluate_approval_diff().
    """
    blockers: list[str] = []
    evidence_refs = {ref.ref_id for ref in diff.evidence}
    entry_dimensions = {entry.dimension for entry in diff.entries}
    missing_dimensions = APPROVAL_DIFF_REQUIRED_DIMENSIONS - entry_dimensions
    if missing_dimensions:
        blockers.append("missing_dimensions:" + ",".join(sorted(item.value for item in missing_dimensions)))
    blockers.extend(
        f"entry_evidence_unresolved:{entry.dimension.value}"
        for entry in diff.entries
        if not set(entry.evidence_refs).issubset(evidence_refs)
    )

    evidence_kinds = {ref.kind for ref in diff.evidence}
    if not {"trace", "eval", "human_review"}.issubset(evidence_kinds):
        blockers.append("missing_source_backing")
    if not diff.provenance_refs:
        blockers.append(ApprovalDiffGate.PROVENANCE.value)
    if diff.confidence < min_confidence:
        blockers.append(ApprovalDiffGate.CONFIDENCE.value)
    if diff.safety_verdict not in {"passed", "acceptable"}:
        blockers.append(ApprovalDiffGate.SAFETY.value)
    if diff.budget_verdict not in {"within_budget", "approved_over_budget"}:
        blockers.append(ApprovalDiffGate.BUDGET.value)
    if not diff.policy_gate_refs:
        blockers.append(ApprovalDiffGate.POLICY.value)
    if not diff.rollback_target_ref:
        blockers.append(ApprovalDiffGate.ROLLBACK.value)
    if not diff.persisted_state_refs:
        blockers.append(ApprovalDiffGate.PERSISTED_STATE.value)
    if not diff.affected_assets:
        blockers.append("affected_assets")

    if diff.status is ApprovalDiffStatus.APPROVED and (
        diff.decision is None
        or diff.decision.status is not ApprovalDiffStatus.APPROVED
        or not diff.decision.authority_refs
    ):
        blockers.append(ApprovalDiffGate.AUTHORITY.value)

    unique_blockers = tuple(dict.fromkeys(blockers))
    return ApprovalDiffReview(
        diff_id=diff.diff_id,
        approved=diff.status is ApprovalDiffStatus.APPROVED and not unique_blockers,
        status=diff.status,
        blockers=unique_blockers,
        evidence={
            "dimension_count": len(entry_dimensions),
            "evidence_count": len(diff.evidence),
            "evidence_kinds": tuple(sorted(evidence_kinds)),
            "confidence": diff.confidence,
            "safety_verdict": diff.safety_verdict,
            "budget_verdict": diff.budget_verdict,
        },
    )


def require_governed_promotion_review(diff: ApprovalDiff, apply_change: Callable[[ApprovalDiff], T]) -> T:
    """Run a promotion mutation callback only after a clean approved diff.

    Args:
        diff: Diff value consumed by require_governed_promotion_review().
        apply_change: Apply change value consumed by require_governed_promotion_review().

    Returns:
        T value produced by require_governed_promotion_review().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    review = evaluate_approval_diff(diff)
    if not review.approved:
        raise ApprovalDiffRejected(f"governed promotion blocked: {list(review.blockers)}")
    return apply_change(diff)


def _target_from_proposal_kind(kind: WorkbenchProposalKind) -> ApprovalDiffTarget:
    if kind is WorkbenchProposalKind.MODEL_DEFAULT:
        return ApprovalDiffTarget.MODEL_DEFAULT
    if kind is WorkbenchProposalKind.PROMPT_VERSION:
        return ApprovalDiffTarget.PROMPT_PROMOTION
    if kind is WorkbenchProposalKind.DATASET_REVISION:
        return ApprovalDiffTarget.DATASET_PROMOTION
    if kind is WorkbenchProposalKind.POLICY_CHANGE:
        return ApprovalDiffTarget.ROUTE_POLICY_CHANGE
    if kind is WorkbenchProposalKind.PIPELINE_ACTIVATION:
        return ApprovalDiffTarget.TRAINING_RECIPE_ACTIVATION
    return ApprovalDiffTarget.AUTOMATION_PROMOTION


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _require_text_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise ValueError(f"{field_name} must be a non-empty tuple")
    for value in values:
        _require_text(value, f"{field_name} entry")


def _require_optional_text_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{field_name} must be a tuple")
    for value in values:
        _require_text(value, f"{field_name} entry")


__all__ = [
    "APPROVAL_DIFF_REQUIRED_DIMENSIONS",
    "ApprovalDiff",
    "ApprovalDiffDecision",
    "ApprovalDiffEvidenceRef",
    "ApprovalDiffGate",
    "ApprovalDiffRejected",
    "ApprovalDiffReview",
    "ApprovalDiffStatus",
    "ApprovalDiffTarget",
    "DiffDimension",
    "DiffEntry",
    "build_approval_diff",
    "build_approval_diff_from_proposal",
    "evaluate_approval_diff",
    "require_governed_promotion_review",
]
