"""Pure adapters from adaptive tuning to dependency-owned authority surfaces."""

from __future__ import annotations

from typing import Any

from vetinari.workbench.adaptive_tuning.contracts import AdaptiveHypothesis, LocalChangeProposal
from vetinari.workbench.hardware import OptimizationProposal
from vetinari.workbench.network import NetworkRoutingDecision
from vetinari.workbench.preferences import PreferenceCard
from vetinari.workbench.user_observability import UserInputSignal

CANONICAL_SIGNAL_TYPES = (UserInputSignal, PreferenceCard, OptimizationProposal, NetworkRoutingDecision)


def canonical_dependency_type_names() -> tuple[str, ...]:
    """Return canonical dependency type names imported by this adapter module."""
    return tuple(item.__name__ for item in CANONICAL_SIGNAL_TYPES)


def proposal_to_improvement_candidate(proposal: LocalChangeProposal) -> dict[str, Any]:
    """Build a promotion-engine candidate payload without mutating the engine."""
    return {
        "candidate_id": proposal.proposal_id,
        "source": "adaptive_tuning",
        "target": proposal.target.value,
        "risk_tier": proposal.risk_tier.value,
        "summary": proposal.summary,
        "requires_measurement": proposal.risk_tier.value == "high",
    }


def proposal_to_approval_request(proposal: LocalChangeProposal) -> dict[str, Any]:
    """Build an approval-chain request payload."""
    return {
        "request_id": proposal.proposal_id,
        "kind": "adaptive_tuning",
        "title": proposal.title,
        "risk_tier": proposal.risk_tier.value,
        "preview": proposal.preview.to_dict() if proposal.preview else None,
        "rollback_required": proposal.rollback.required,
    }


def proposal_to_shadow_rollback(proposal: LocalChangeProposal) -> dict[str, Any]:
    """Build a shadow-undo rollback readiness packet."""
    return {
        "subject_id": proposal.proposal_id,
        "rollback_ref": proposal.rollback.rollback_ref,
        "readiness_checked": proposal.rollback.readiness_checked,
        "caller_must_execute": True,
    }


def proposal_to_artifact_review(proposal: LocalChangeProposal) -> dict[str, Any]:
    """Build an artifact-review packet for a proposal preview.

    Returns:
        dict[str, Any] value produced by proposal_to_artifact_review().
    """
    preview = proposal.preview.to_dict() if proposal.preview else {"before": {}, "after": {}}
    return {
        "subject_id": proposal.proposal_id,
        "kind": "adaptive_tuning_preview",
        "before_artifact": preview.get("before", {}),
        "after_artifact": preview.get("after", {}),
    }


def hypothesis_to_preference_card_draft(hypothesis: AdaptiveHypothesis) -> dict[str, Any]:
    """Build a preference-card draft payload without activating it."""
    return {
        "card_id": f"adaptive:{hypothesis.hypothesis_id}",
        "status": "proposed",
        "statement": hypothesis.title,
        "confidence": hypothesis.confidence,
        "evidence_ids": [item.evidence_id for item in hypothesis.evidence],
        "requires_explicit_consent": True,
    }


def hypothesis_to_hardware_advisory(hypothesis: AdaptiveHypothesis) -> dict[str, Any]:
    """Build an advisory-only hardware payload."""
    return {
        "source": "adaptive_tuning",
        "hypothesis_id": hypothesis.hypothesis_id,
        "advisory_only": True,
        "mutates_host": False,
    }


def hypothesis_to_network_advisory(hypothesis: AdaptiveHypothesis) -> dict[str, Any]:
    """Build an advisory-only network payload."""
    return {
        "source": "adaptive_tuning",
        "hypothesis_id": hypothesis.hypothesis_id,
        "advisory_only": True,
        "mutates_network": False,
    }
