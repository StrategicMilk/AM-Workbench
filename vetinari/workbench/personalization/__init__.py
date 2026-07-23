"""Public contracts for governed Workbench user personalization."""

from __future__ import annotations

from vetinari.workbench.personalization.contracts import (
    AllowedUse,
    AuditTrailRef,
    CandidateInputKind,
    DependencyGateRefs,
    PersonalizationContractError,
    PersonalizationDecision,
    PersonalizationDecisionStatus,
    ProfileCard,
    ProfileRecordKind,
    ProfileRecordStatus,
    ProvenanceRef,
    RetentionPolicyRef,
    TrainingCandidate,
    TrainingGovernanceProof,
    TrainingPromotionTarget,
    evaluate_profile_card,
    evaluate_training_candidate,
)
from vetinari.workbench.personalization.runtime import (
    PersonalizationProfileStore,
    PersonalizationRuntimeError,
    UserPersonalizationPolicy,
    evaluate_candidate_with_policy,
    load_personalization_policy,
)

__all__ = [
    "AllowedUse",
    "AuditTrailRef",
    "CandidateInputKind",
    "DependencyGateRefs",
    "PersonalizationContractError",
    "PersonalizationDecision",
    "PersonalizationDecisionStatus",
    "PersonalizationProfileStore",
    "PersonalizationRuntimeError",
    "ProfileCard",
    "ProfileRecordKind",
    "ProfileRecordStatus",
    "ProvenanceRef",
    "RetentionPolicyRef",
    "TrainingCandidate",
    "TrainingGovernanceProof",
    "TrainingPromotionTarget",
    "UserPersonalizationPolicy",
    "evaluate_candidate_with_policy",
    "evaluate_profile_card",
    "evaluate_training_candidate",
    "load_personalization_policy",
]
