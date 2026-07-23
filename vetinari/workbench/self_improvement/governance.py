"""Fail-closed governance for Workbench self-improvement proposals."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _parse_utc(value: str, field_name: str) -> datetime:
    _require_non_empty(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone")
    return parsed.astimezone(timezone.utc)


class ImprovementKind(str, Enum):
    """Sources of workbench self-improvement proposals."""

    FEEDBACK_RULE = "feedback_rule"
    FAILURE_RECOVERY = "failure_recovery"
    IMPLICIT_PRIOR = "implicit_prior"
    PROMPT_MUTATION = "prompt_mutation"
    ROUTE_CHANGE = "route_change"
    MODEL_CHOICE = "model_choice"
    METHOD_CHANGE = "method_change"
    BACKEND_TUNING = "backend_tuning"


class SelfImprovementGovernanceMode(str, Enum):
    """Execution modes before a proposal is allowed to change defaults."""

    SHADOW = "shadow"
    CANARY = "canary"
    LIVE = "live"


class DefaultChangeTarget(str, Enum):
    """Runtime default classes that require promotion-gate approval."""

    PROMPT = "prompt"
    ROUTE = "route"
    MODEL = "model"
    METHOD = "method"
    BACKEND = "backend"
    POLICY = "policy"


@dataclass(frozen=True, slots=True)
class EvidenceArtifact:
    """One measured evidence item attached to a proposal."""

    artifact_id: str
    kind: str
    summary: str
    captured_at_utc: str
    supports_candidate: bool
    negative_result: bool = False
    contamination_checked: bool = False

    def __post_init__(self) -> None:
        _require_non_empty(self.artifact_id, "artifact_id")
        _require_non_empty(self.kind, "kind")
        _require_non_empty(self.summary, "summary")
        _parse_utc(self.captured_at_utc, "captured_at_utc")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvidenceArtifact(artifact_id={self.artifact_id!r}, kind={self.kind!r}, summary={self.summary!r})"


@dataclass(frozen=True, slots=True)
class LearningBoundary:
    """Workspace, domain, user, sensitivity, model, and task boundaries."""

    workspace_id: str
    domain: str
    user_scope: str
    data_sensitivity: str
    allowed_model_versions: tuple[str, ...]
    task_shape: str

    def __post_init__(self) -> None:
        _require_non_empty(self.workspace_id, "workspace_id")
        _require_non_empty(self.domain, "domain")
        _require_non_empty(self.user_scope, "user_scope")
        _require_non_empty(self.data_sensitivity, "data_sensitivity")
        _require_non_empty(self.task_shape, "task_shape")
        if self.data_sensitivity == "unknown":
            raise ValueError("data_sensitivity must be classified")
        if not self.allowed_model_versions:
            raise ValueError("allowed_model_versions must be non-empty")
        for version in self.allowed_model_versions:
            _require_non_empty(version, "allowed_model_versions entry")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"LearningBoundary(workspace_id={self.workspace_id!r}, domain={self.domain!r}, user_scope={self.user_scope!r})"


@dataclass(frozen=True, slots=True)
class SelfImprovementRollbackPlan:
    """Rollback metadata required before promotion-gated default changes."""

    target_ref: str
    owner: str
    steps: tuple[str, ...]
    expires_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.target_ref, "target_ref")
        _require_non_empty(self.owner, "owner")
        if not self.steps:
            raise ValueError("rollback steps must be non-empty")
        for step in self.steps:
            _require_non_empty(step, "rollback step")
        _parse_utc(self.expires_at_utc, "expires_at_utc")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"SelfImprovementRollbackPlan(target_ref={self.target_ref!r}, owner={self.owner!r}, steps={self.steps!r})"
        )


@dataclass(frozen=True, slots=True)
class ImprovementProposal:
    """Self-improvement candidate with evidence, risk, rollback, and expiry."""

    proposal_id: str
    kind: ImprovementKind
    target: DefaultChangeTarget
    baseline_ref: str
    candidate_ref: str
    evidence: tuple[EvidenceArtifact, ...]
    risk: str
    impacted_assets: tuple[str, ...]
    rollback: SelfImprovementRollbackPlan | None
    mode: SelfImprovementGovernanceMode
    boundary: LearningBoundary | None
    model_version: str
    task_shape: str
    opened_at_utc: str
    expires_at_utc: str
    promotion_decision_id: str = ""
    promotion_approved: bool = False

    def __post_init__(self) -> None:
        _require_non_empty(self.proposal_id, "proposal_id")
        _require_non_empty(self.baseline_ref, "baseline_ref")
        _require_non_empty(self.candidate_ref, "candidate_ref")
        _require_non_empty(self.risk, "risk")
        _require_non_empty(self.model_version, "model_version")
        _require_non_empty(self.task_shape, "task_shape")
        _parse_utc(self.opened_at_utc, "opened_at_utc")
        _parse_utc(self.expires_at_utc, "expires_at_utc")
        if not self.impacted_assets:
            raise ValueError("impacted_assets must be non-empty")
        for asset in self.impacted_assets:
            _require_non_empty(asset, "impacted_assets entry")
        if self.promotion_approved:
            _require_non_empty(self.promotion_decision_id, "promotion_decision_id")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ImprovementProposal(proposal_id={self.proposal_id!r}, kind={self.kind!r}, target={self.target!r})"


@dataclass(frozen=True, slots=True)
class GovernanceDecision:
    """Deterministic proposal verdict used by promotion and tuning gates."""

    proposal_id: str
    approved: bool
    blockers: tuple[str, ...]
    evidence: Mapping[str, Any]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"GovernanceDecision(proposal_id={self.proposal_id!r}, approved={self.approved!r}, blockers={self.blockers!r})"


def should_decay_prior(
    *,
    prior_model_version: str,
    prior_task_shape: str,
    proposal: ImprovementProposal,
) -> bool:
    """Return true when prior evidence no longer matches model or task shape."""
    return prior_model_version != proposal.model_version or prior_task_shape != proposal.task_shape


def evaluate_improvement_proposal(
    proposal: ImprovementProposal,
    *,
    now_utc: datetime | None = None,
) -> GovernanceDecision:
    """Evaluate a proposal without mutating defaults or shared state.

    Returns:
        GovernanceDecision value produced by evaluate_improvement_proposal().
    """
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    blockers: list[str] = []
    evidence: dict[str, Any] = {
        "proposal_id": proposal.proposal_id,
        "kind": proposal.kind.value,
        "target": proposal.target.value,
        "mode": proposal.mode.value,
        "evidence_count": len(proposal.evidence),
        "impacted_asset_count": len(proposal.impacted_assets),
    }

    if not proposal.evidence:
        blockers.append("missing_evidence")
    if not any(item.supports_candidate for item in proposal.evidence):
        blockers.append("missing_candidate_support")
    if not any(item.negative_result for item in proposal.evidence):
        blockers.append("missing_negative_result_asset")
    if not all(item.contamination_checked for item in proposal.evidence):
        blockers.append("contamination_control_missing")
    if proposal.rollback is None:
        blockers.append("missing_rollback")
    if proposal.boundary is None:
        blockers.append("missing_learning_boundary")
    else:
        if proposal.model_version not in proposal.boundary.allowed_model_versions:
            blockers.append("model_version_drift")
        if proposal.task_shape != proposal.boundary.task_shape:
            blockers.append("task_shape_drift")
        evidence["workspace_id"] = proposal.boundary.workspace_id
        evidence["domain"] = proposal.boundary.domain
        evidence["data_sensitivity"] = proposal.boundary.data_sensitivity

    expires_at = _parse_utc(proposal.expires_at_utc, "expires_at_utc")
    if expires_at <= now:
        blockers.append("proposal_expired")
    if proposal.rollback is not None:
        rollback_expires = _parse_utc(proposal.rollback.expires_at_utc, "rollback.expires_at_utc")
        if rollback_expires <= now:
            blockers.append("rollback_expired")
    if proposal.mode is SelfImprovementGovernanceMode.LIVE and not proposal.promotion_approved:
        blockers.append("promotion_gate_required")

    unique_blockers = tuple(dict.fromkeys(blockers))
    return GovernanceDecision(
        proposal_id=proposal.proposal_id,
        approved=not unique_blockers,
        blockers=unique_blockers,
        evidence=evidence,
    )


def is_default_change_approved(decision: object) -> bool:
    """Return true only for an explicit clean governance approval object.

    Returns:
        Boolean indicating whether is default change approved.
    """
    if not isinstance(decision, GovernanceDecision):
        return False
    return decision.approved is True and decision.blockers == ()


def apply_default_change_after_governance(
    proposal: ImprovementProposal,
    apply_change: Callable[[ImprovementProposal], Any],
    *,
    now_utc: datetime | None = None,
) -> Any:
    """Run a default mutation callback only after fail-closed governance passes.

    Args:
        proposal: Proposal value consumed by apply_default_change_after_governance().
        apply_change: Apply change value consumed by apply_default_change_after_governance().
        now_utc: Now utc value consumed by apply_default_change_after_governance().

    Returns:
        Any value produced by apply_default_change_after_governance().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    decision = evaluate_improvement_proposal(proposal, now_utc=now_utc)
    if not is_default_change_approved(decision):
        raise PermissionError(f"default change blocked: {list(decision.blockers)}")
    return apply_change(proposal)
