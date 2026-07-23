"""Self-improvement governance contracts for AM Workbench."""

from __future__ import annotations

from vetinari.workbench.self_improvement.governance import (
    DefaultChangeTarget,
    EvidenceArtifact,
    GovernanceDecision,
    ImprovementKind,
    ImprovementProposal,
    LearningBoundary,
    SelfImprovementGovernanceMode,
    SelfImprovementRollbackPlan,
    apply_default_change_after_governance,
    evaluate_improvement_proposal,
    is_default_change_approved,
    should_decay_prior,
)

__all__ = [
    "DefaultChangeTarget",
    "EvidenceArtifact",
    "GovernanceDecision",
    "ImprovementKind",
    "ImprovementProposal",
    "LearningBoundary",
    "SelfImprovementGovernanceMode",
    "SelfImprovementRollbackPlan",
    "apply_default_change_after_governance",
    "evaluate_improvement_proposal",
    "is_default_change_approved",
    "should_decay_prior",
]
