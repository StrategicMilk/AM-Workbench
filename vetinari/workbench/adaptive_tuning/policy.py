"""Fail-closed adaptive tuning policy evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from vetinari.workbench.adaptive_tuning.contracts import (
    HOST_MUTATION_TARGETS,
    PROTECTED_SILENT_TARGETS,
    AdaptationTarget,
    AdaptiveTuningPolicyDecision,
    EvidenceBlocker,
    FrictionObservation,
    LocalChangeProposal,
    ProposalState,
    RiskTier,
)


@dataclass(frozen=True, slots=True)
class AdaptiveTuningPolicy:
    """Conservative defaults for adaptive proposal admission."""

    consent_required: bool = True
    explicit_consent_granted: bool = False
    allow_low_risk_auto_apply: bool = False
    min_confidence: float = 0.65
    evidence_stale_after_days: int = 30
    high_risk_measurement_stale_after_days: int = 14
    medium_risk_requires_preview: bool = True
    medium_risk_requires_approval: bool = True
    high_risk_requires_tests: bool = True
    high_risk_requires_rollback: bool = True
    high_risk_requires_promotion_evidence: bool = True
    anti_sycophancy_non_overridable: bool = True
    allow_host_network_mutation: bool = False

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AdaptiveTuningPolicy(consent_required={self.consent_required!r}, explicit_consent_granted={self.explicit_consent_granted!r}, allow_low_risk_auto_apply={self.allow_low_risk_auto_apply!r})"


def classify_target_risk(target: AdaptationTarget) -> RiskTier:
    """Return the default risk tier for an adaptation target.

    Returns:
        RiskTier value produced by classify_target_risk().
    """
    if target in {
        AdaptationTarget.LOCAL_UI_DEFAULT,
        AdaptationTarget.LOCAL_SHORTCUT,
        AdaptationTarget.LOCAL_REVIEW_LAYOUT,
    }:
        return RiskTier.LOW
    if target in {AdaptationTarget.PROFILE_FACT, AdaptationTarget.SENSITIVE_CONTEXT, AdaptationTarget.FACTUAL_TRUTH}:
        return RiskTier.HIGH
    if target in {AdaptationTarget.TRAINING_DATUM, AdaptationTarget.MODEL_ROUTE, AdaptationTarget.PROJECT_DEFAULT}:
        return RiskTier.HIGH
    if target in {AdaptationTarget.AUTOMATION, AdaptationTarget.AGENT_ROUTE, AdaptationTarget.RESOURCE_POLICY}:
        return RiskTier.HIGH
    if target in {AdaptationTarget.NETWORK_ROUTE, AdaptationTarget.HOST_SETTING, AdaptationTarget.OS_SETTING}:
        return RiskTier.HIGH
    return RiskTier.MEDIUM


def observation_blockers(
    observation: FrictionObservation,
    policy: AdaptiveTuningPolicy,
    *,
    now_utc: datetime | None = None,
) -> tuple[EvidenceBlocker, ...]:
    """Return fail-closed blockers for one raw observation.

    Args:
        observation: Observation value consumed by observation_blockers().
        policy: Policy value consumed by observation_blockers().
        now_utc: Now utc value consumed by observation_blockers().

    Returns:
        tuple[EvidenceBlocker, ...] value produced by observation_blockers().
    """
    current = _utc_now(now_utc)
    blockers: list[EvidenceBlocker] = []
    if observation.scope is None:
        blockers.append(EvidenceBlocker.MISSING_SCOPE)
    if not tuple(ref for ref in observation.evidence_refs if ref.strip()):
        blockers.append(EvidenceBlocker.MISSING_EVIDENCE)
    if not observation.provenance_ref.strip():
        blockers.append(EvidenceBlocker.MISSING_PROVENANCE)
    if observation.private:
        blockers.append(EvidenceBlocker.PRIVATE_EVIDENCE)
    if observation.unreadable:
        blockers.append(EvidenceBlocker.UNREADABLE_EVIDENCE)
    if observation.denied:
        blockers.append(EvidenceBlocker.DENIED_AUTHORITY)
    if observation.confidence < policy.min_confidence:
        blockers.append(EvidenceBlocker.LOW_CONFIDENCE)
    if observation.contradicted_by:
        blockers.append(EvidenceBlocker.CONTRADICTORY_EVIDENCE)
    try:
        observed = datetime.fromisoformat(observation.observed_at_utc.replace("Z", "+00:00"))
    except ValueError:
        blockers.append(EvidenceBlocker.INVALID_TIMESTAMP)
    else:
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if observed.astimezone(timezone.utc) > current:
            blockers.append(EvidenceBlocker.INVALID_TIMESTAMP)
        elif (current - observed.astimezone(timezone.utc)).days > policy.evidence_stale_after_days:
            blockers.append(EvidenceBlocker.STALE_EVIDENCE)
    if observation.target in HOST_MUTATION_TARGETS and not policy.allow_host_network_mutation:
        blockers.append(EvidenceBlocker.HOST_OR_NETWORK_MUTATION)
    return tuple(dict.fromkeys(blockers))


def silent_observation_target_allowed(target: AdaptationTarget) -> bool:
    """Return whether observed behavior may silently activate a target."""
    return target not in PROTECTED_SILENT_TARGETS


def evaluate_proposal_policy(
    proposal: LocalChangeProposal,
    policy: AdaptiveTuningPolicy,
    *,
    now_utc: datetime | None = None,
) -> AdaptiveTuningPolicyDecision:
    """Evaluate a proposal and fail closed when authority is missing.

    Args:
        proposal: Proposal value consumed by evaluate_proposal_policy().
        policy: Policy value consumed by evaluate_proposal_policy().
        now_utc: Now utc value consumed by evaluate_proposal_policy().

    Returns:
        AdaptiveTuningPolicyDecision value produced by evaluate_proposal_policy().
    """
    current = _utc_now(now_utc)
    blockers: list[str] = []
    required: list[str] = []

    if policy.anti_sycophancy_non_overridable and _proposal_overrides_sycophancy_constraint(proposal):
        blockers.append("anti-sycophancy-non-overridable")
        required.append("anti-sycophancy-policy")

    if proposal.target in HOST_MUTATION_TARGETS and not policy.allow_host_network_mutation:
        blockers.append("host-or-network-mutation-forbidden")
        required.append("advisory-only-recommendation")

    if proposal.target in PROTECTED_SILENT_TARGETS and proposal.requested_auto_apply:
        blockers.append("protected-target-cannot-auto-apply")
        required.append("explicit-proposal-path")

    if policy.consent_required and not policy.explicit_consent_granted:
        blockers.append("explicit-consent-required")
        required.append("consent")

    if proposal.risk_tier is RiskTier.LOW:
        if proposal.requested_auto_apply and not policy.allow_low_risk_auto_apply:
            blockers.append("low-risk-auto-apply-disabled")
            required.append("policy-auto-apply-allow")
        state = (
            _state_for_blockers(blockers)
            if blockers
            else (ProposalState.AUTO_APPLICABLE if proposal.requested_auto_apply else ProposalState.APPROVED)
        )
        return AdaptiveTuningPolicyDecision(
            proposal.proposal_id, state, not blockers, tuple(blockers), tuple(dict.fromkeys(required))
        )

    if policy.medium_risk_requires_preview and proposal.preview is None:
        blockers.append("preview-required")
        required.append("preview")
    if policy.medium_risk_requires_approval and not proposal.approval_ref.strip():
        blockers.append("approval-required")
        required.append("approval")

    if proposal.risk_tier is RiskTier.HIGH:
        if policy.high_risk_requires_tests and not proposal.tests_ref.strip():
            blockers.append("tests-required")
            required.append("tests")
        if policy.high_risk_requires_rollback and not proposal.rollback.satisfied():
            blockers.append("rollback-required")
            required.append("rollback")
        if policy.high_risk_requires_promotion_evidence:
            evidence_ok = proposal.promotion_evidence is not None and proposal.promotion_evidence.trusted(
                now_utc=current,
                stale_after_days=policy.high_risk_measurement_stale_after_days,
            )
            if not evidence_ok:
                blockers.append("promotion-evidence-required")
                required.append("promotion-evidence")

    if not blockers:
        return AdaptiveTuningPolicyDecision(
            proposal.proposal_id, ProposalState.APPROVED, True, (), tuple(dict.fromkeys(required))
        )
    state = _state_for_blockers(blockers)
    return AdaptiveTuningPolicyDecision(
        proposal.proposal_id, state, False, tuple(dict.fromkeys(blockers)), tuple(dict.fromkeys(required))
    )


def _state_for_blockers(blockers: list[str]) -> ProposalState:
    if "preview-required" in blockers:
        return ProposalState.NEEDS_PREVIEW
    if "approval-required" in blockers:
        return ProposalState.NEEDS_APPROVAL
    if "tests-required" in blockers:
        return ProposalState.NEEDS_TESTS
    if "rollback-required" in blockers:
        return ProposalState.NEEDS_ROLLBACK
    if "promotion-evidence-required" in blockers:
        return ProposalState.NEEDS_PROMOTION_EVIDENCE
    return ProposalState.BLOCKED


def _proposal_overrides_sycophancy_constraint(proposal: LocalChangeProposal) -> bool:
    text = f"{proposal.title}\n{proposal.summary}".lower()
    override_markers = (
        "anti_sycophancy=false",
        "anti-sycophancy=false",
        "sycophancy_guard=false",
        "sycophancy guard false",
        "disable anti-sycophancy",
        "bypass anti-sycophancy",
    )
    return any(marker in text for marker in override_markers)


def _utc_now(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)
