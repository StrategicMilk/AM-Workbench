"""Governed user-personalization contracts.

The objects in this module separate editable profile facts from trainable
behavior preferences. They are side-effect free and return typed blocked
decisions for policy failures instead of mutating model, prompt, route, or
training artifacts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from .anti_sycophancy import AntiSycophancyGateDecision
from .contract_constants import (
    BLOCKER_CONFLICT,
    BLOCKER_DELETE_REQUESTED,
    BLOCKER_DOWNSTREAM_GATE_FAILED,
    BLOCKER_DOWNSTREAM_GATE_MISSING,
    BLOCKER_EXPIRED,
    BLOCKER_MISSING_ALLOWED_USE,
    BLOCKER_MISSING_AUDIT_TRAIL,
    BLOCKER_MISSING_CONFIDENCE,
    BLOCKER_MISSING_CONSENT,
    BLOCKER_MISSING_DEDUPE,
    BLOCKER_MISSING_DEPENDENCY,
    BLOCKER_MISSING_PROVENANCE,
    BLOCKER_MISSING_REDACTION,
    BLOCKER_MISSING_RETENTION,
    BLOCKER_MISSING_REVOCATION,
    BLOCKER_MISSING_SENSITIVITY_REVIEW,
    BLOCKER_MISSING_SPLIT_FIREWALL,
    BLOCKER_OPAQUE_MODEL_WEIGHTS,
    BLOCKER_PROFILE_FACT,
    BLOCKER_RAW_USER_LOG,
    BLOCKER_REVOKED,
    BLOCKER_SENSITIVE_CONTEXT,
)
from .contract_utils import (
    CONTRACT_PUBLIC_EXPORTS,
    _has_text,
    _normalize_now,
    _parse_utc,
    _require_confidence,
    _require_text,
    _require_tuple_type,
    to_jsonable,
)

SCHEMA_VERSION = 1


class PersonalizationContractError(ValueError):
    """Raised when a personalization contract object is malformed."""


class ProfileRecordKind(str, Enum):
    """Editable profile record categories."""

    USER_FACT = "user_fact"
    SENSITIVE_CONTEXT = "sensitive_context"
    PROJECT_PREFERENCE = "project_preference"


class ProfileRecordStatus(str, Enum):
    """Lifecycle state for editable profile records."""

    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    DELETE_REQUESTED = "delete_requested"
    CONFLICT_REVIEW_NEEDED = "conflict_review_needed"


class AllowedUse(str, Enum):
    """Where a profile card may be used without becoming training data."""

    PROFILE_MEMORY = "profile_memory"
    RUNTIME_CONTEXT = "runtime_context"
    PROJECT_DEFAULT = "project_default"
    SUPPORTING_EVIDENCE = "supporting_evidence"


class CandidateInputKind(str, Enum):
    """Input categories evaluated before training-adjacent promotion."""

    STABLE_BEHAVIOR_PREFERENCE = "stable_behavior_preference"
    PROFILE_FACT = "profile_fact"
    SENSITIVE_CONTEXT = "sensitive_context"
    RAW_USER_LOG = "raw_user_log"
    RAW_PROMPT = "raw_prompt"
    RAW_TRANSCRIPT_SNIPPET = "raw_transcript_snippet"


class TrainingPromotionTarget(str, Enum):
    """Training-adjacent artifacts this pack may nominate, not mutate."""

    EVAL_CASE = "eval_case"
    PREFERENCE_PAIR = "preference_pair"
    PROMPT_CHANGE = "prompt_change"
    ROUTE_RULE = "route_rule"
    MODEL_ADAPTER_CANDIDATE = "model_adapter_candidate"
    OPAQUE_MODEL_WEIGHTS = "opaque_model_weights"


class PersonalizationDecisionStatus(str, Enum):
    """Typed status for profile and candidate decisions."""

    PROFILE_CARD_APPROVED = "profile_card_approved"
    TRAINING_CANDIDATE_APPROVED = "training_candidate_approved"
    BLOCKED = "blocked"
    RECOVERY_NEEDED = "recovery_needed"
    CONFLICT_NEEDED = "conflict_needed"
    DELETED_OR_REVOKED = "deleted_or_revoked"


@dataclass(frozen=True, slots=True)
class PersonalizationProvenanceRef:
    """Source and authority evidence for a user signal or profile fact."""

    source_ref: str
    authority_ref: str
    captured_at_utc: str
    confidence: float

    def __post_init__(self) -> None:
        _require_text(self.source_ref, "source_ref")
        _require_text(self.authority_ref, "authority_ref")
        _parse_utc(self.captured_at_utc, "captured_at_utc")
        _require_confidence(self.confidence, "confidence")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ProvenanceRef(source_ref={self.source_ref!r}, authority_ref={self.authority_ref!r}, captured_at_utc={self.captured_at_utc!r})"


ProvenanceRef = PersonalizationProvenanceRef


@dataclass(frozen=True, slots=True)
class RetentionPolicyRef:
    """Retention, expiry, deletion, and revoke metadata for a profile card."""

    retention_ref: str
    expires_at_utc: str
    delete_ref: str = ""
    revocation_ref: str = ""

    def __post_init__(self) -> None:
        _require_text(self.retention_ref, "retention_ref")
        _parse_utc(self.expires_at_utc, "expires_at_utc")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetentionPolicyRef(retention_ref={self.retention_ref!r}, expires_at_utc={self.expires_at_utc!r}, delete_ref={self.delete_ref!r})"


@dataclass(frozen=True, slots=True)
class AuditTrailRef:
    """Signal-to-decision evidence retained for every governed decision."""

    signal_ref: str
    decision_ref: str
    artifact_ref: str
    captured_at_utc: str

    def __post_init__(self) -> None:
        _require_text(self.signal_ref, "signal_ref")
        _require_text(self.decision_ref, "decision_ref")
        _require_text(self.artifact_ref, "artifact_ref")
        _parse_utc(self.captured_at_utc, "captured_at_utc")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AuditTrailRef(signal_ref={self.signal_ref!r}, decision_ref={self.decision_ref!r}, artifact_ref={self.artifact_ref!r})"


@dataclass(frozen=True, slots=True)
class DependencyGateRefs:
    """Dependency-pack proof references required before candidate approval."""

    memory_firewall_ref: str
    tuning_data_source_ref: str
    model_foundry_ref: str
    approval_diff_ref: str
    improvement_engine_ref: str
    downstream_anti_sycophancy_gate_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "memory_firewall_ref",
            "tuning_data_source_ref",
            "model_foundry_ref",
            "approval_diff_ref",
            "improvement_engine_ref",
            "downstream_anti_sycophancy_gate_ref",
        ):
            if not isinstance(getattr(self, field_name), str):
                raise PersonalizationContractError(f"{field_name} must be a string")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DependencyGateRefs(memory_firewall_ref={self.memory_firewall_ref!r}, tuning_data_source_ref={self.tuning_data_source_ref!r}, model_foundry_ref={self.model_foundry_ref!r})"


@dataclass(frozen=True, slots=True)
class ProfileCard:
    """Editable profile card that cannot directly become training data."""

    card_id: str
    kind: ProfileRecordKind
    subject_ref: str
    statement: str
    status: ProfileRecordStatus
    provenance: ProvenanceRef
    allowed_uses: tuple[AllowedUse, ...]
    retention: RetentionPolicyRef
    audit_trail: tuple[AuditTrailRef, ...]
    sensitivity_label: str
    conflict_ref: str = ""

    def __post_init__(self) -> None:
        _require_text(self.card_id, "card_id")
        _require_text(self.subject_ref, "subject_ref")
        _require_text(self.statement, "statement")
        _require_text(self.sensitivity_label, "sensitivity_label")
        if not isinstance(self.kind, ProfileRecordKind):
            raise PersonalizationContractError("kind must be ProfileRecordKind")
        if not isinstance(self.status, ProfileRecordStatus):
            raise PersonalizationContractError("status must be ProfileRecordStatus")
        if not isinstance(self.provenance, ProvenanceRef):
            raise PersonalizationContractError("provenance must be ProvenanceRef")
        if not isinstance(self.retention, RetentionPolicyRef):
            raise PersonalizationContractError("retention must be RetentionPolicyRef")
        _require_tuple_type(self.allowed_uses, AllowedUse, "allowed_uses")
        _require_tuple_type(self.audit_trail, AuditTrailRef, "audit_trail")
        if self.status is ProfileRecordStatus.CONFLICT_REVIEW_NEEDED:
            _require_text(self.conflict_ref, "conflict_ref")
        if self.status is ProfileRecordStatus.REVOKED:
            _require_text(self.retention.revocation_ref, "retention.revocation_ref")
        if self.status is ProfileRecordStatus.DELETE_REQUESTED:
            _require_text(self.retention.delete_ref, "retention.delete_ref")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return to_jsonable(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ProfileCard(card_id={self.card_id!r}, kind={self.kind!r}, subject_ref={self.subject_ref!r})"


@dataclass(frozen=True, slots=True)
class TrainingGovernanceProof:
    """Gate proof required for a stable behavior preference candidate."""

    consent_ref: str
    redaction_ref: str
    dedupe_ref: str
    split_firewall_ref: str
    sensitivity_review_ref: str
    retention_ref: str
    revocation_check_ref: str
    dependency_refs: DependencyGateRefs

    def blockers(self) -> tuple[str, ...]:
        """Return missing-gate blockers without raising.

        Returns:
            tuple[str, ...] value produced by blockers().
        """
        checks = (
            ("consent_ref", self.consent_ref, BLOCKER_MISSING_CONSENT),
            ("redaction_ref", self.redaction_ref, BLOCKER_MISSING_REDACTION),
            ("dedupe_ref", self.dedupe_ref, BLOCKER_MISSING_DEDUPE),
            ("split_firewall_ref", self.split_firewall_ref, BLOCKER_MISSING_SPLIT_FIREWALL),
            ("sensitivity_review_ref", self.sensitivity_review_ref, BLOCKER_MISSING_SENSITIVITY_REVIEW),
            ("retention_ref", self.retention_ref, BLOCKER_MISSING_RETENTION),
            ("revocation_check_ref", self.revocation_check_ref, BLOCKER_MISSING_REVOCATION),
        )
        blockers = [blocker for _, value, blocker in checks if not _has_text(value)]
        if not isinstance(self.dependency_refs, DependencyGateRefs):
            blockers.append(BLOCKER_MISSING_DEPENDENCY)
        else:
            dependency_values = (
                self.dependency_refs.memory_firewall_ref,
                self.dependency_refs.tuning_data_source_ref,
                self.dependency_refs.model_foundry_ref,
                self.dependency_refs.approval_diff_ref,
                self.dependency_refs.improvement_engine_ref,
                self.dependency_refs.downstream_anti_sycophancy_gate_ref,
            )
            if any(not _has_text(value) for value in dependency_values):
                blockers.append(BLOCKER_MISSING_DEPENDENCY)
        return tuple(dict.fromkeys(blockers))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TrainingGovernanceProof(consent_ref={self.consent_ref!r}, redaction_ref={self.redaction_ref!r}, dedupe_ref={self.dedupe_ref!r})"


@dataclass(frozen=True, slots=True)
class TrainingCandidate:
    """A behavior-preference signal before any downstream artifact mutation."""

    candidate_id: str
    input_kind: CandidateInputKind
    target: TrainingPromotionTarget
    source_signal_ref: str
    summary: str
    stable_preference: bool
    governance: TrainingGovernanceProof
    audit_trail: tuple[AuditTrailRef, ...]
    provenance: tuple[ProvenanceRef, ...]
    confidence: float

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        _require_text(self.source_signal_ref, "source_signal_ref")
        _require_text(self.summary, "summary")
        if not isinstance(self.input_kind, CandidateInputKind):
            raise PersonalizationContractError("input_kind must be CandidateInputKind")
        if not isinstance(self.target, TrainingPromotionTarget):
            raise PersonalizationContractError("target must be TrainingPromotionTarget")
        if not isinstance(self.governance, TrainingGovernanceProof):
            raise PersonalizationContractError("governance must be TrainingGovernanceProof")
        _require_tuple_type(self.audit_trail, AuditTrailRef, "audit_trail")
        _require_tuple_type(self.provenance, ProvenanceRef, "provenance")
        _require_confidence(self.confidence, "confidence")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return to_jsonable(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TrainingCandidate(candidate_id={self.candidate_id!r}, input_kind={self.input_kind!r}, target={self.target!r})"


@dataclass(frozen=True, slots=True)
class PersonalizationDecision:
    """Fail-closed decision for profile storage or training candidate use."""

    subject_id: str
    status: PersonalizationDecisionStatus
    approved: bool
    blockers: tuple[str, ...]
    audit_trail: tuple[AuditTrailRef, ...]
    evidence: dict[str, Any]

    def __post_init__(self) -> None:
        _require_text(self.subject_id, "subject_id")
        if not isinstance(self.status, PersonalizationDecisionStatus):
            raise PersonalizationContractError("status must be PersonalizationDecisionStatus")
        if self.approved and self.blockers:
            raise PersonalizationContractError("approved decisions cannot include blockers")
        if not self.approved and self.status not in {
            PersonalizationDecisionStatus.BLOCKED,
            PersonalizationDecisionStatus.RECOVERY_NEEDED,
            PersonalizationDecisionStatus.CONFLICT_NEEDED,
            PersonalizationDecisionStatus.DELETED_OR_REVOKED,
        }:
            raise PersonalizationContractError("unapproved decisions need a blocked/recovery status")
        _require_tuple_type(self.audit_trail, AuditTrailRef, "audit_trail", allow_empty=True)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation."""
        return to_jsonable(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PersonalizationDecision(subject_id={self.subject_id!r}, status={self.status!r}, approved={self.approved!r})"


def evaluate_profile_card(card: ProfileCard, *, now_utc: datetime | None = None) -> PersonalizationDecision:
    """Approve only editable profile-card storage, never training use.

    Returns:
        PersonalizationDecision value produced by evaluate_profile_card().
    """
    now = _normalize_now(now_utc)
    blockers: list[str] = []
    status = PersonalizationDecisionStatus.PROFILE_CARD_APPROVED

    expires_at = _parse_utc(card.retention.expires_at_utc, "expires_at_utc")
    if expires_at <= now:
        blockers.append(BLOCKER_EXPIRED)
    if card.status is ProfileRecordStatus.REVOKED:
        blockers.append(BLOCKER_REVOKED)
        status = PersonalizationDecisionStatus.DELETED_OR_REVOKED
    if card.status is ProfileRecordStatus.DELETE_REQUESTED:
        blockers.append(BLOCKER_DELETE_REQUESTED)
        status = PersonalizationDecisionStatus.DELETED_OR_REVOKED
    if card.status is ProfileRecordStatus.CONFLICT_REVIEW_NEEDED:
        blockers.append(BLOCKER_CONFLICT)
        status = PersonalizationDecisionStatus.CONFLICT_NEEDED
    if not card.allowed_uses:
        blockers.append(BLOCKER_MISSING_ALLOWED_USE)
    if not card.audit_trail:
        blockers.append(BLOCKER_MISSING_AUDIT_TRAIL)

    unique_blockers = tuple(dict.fromkeys(blockers))
    return PersonalizationDecision(
        subject_id=card.card_id,
        status=status if not unique_blockers else PersonalizationDecisionStatus.BLOCKED,
        approved=not unique_blockers,
        blockers=unique_blockers,
        audit_trail=card.audit_trail,
        evidence={
            "schema_version": SCHEMA_VERSION,
            "record_kind": card.kind.value,
            "allowed_uses": tuple(use.value for use in card.allowed_uses),
            "non_trainable": True,
        },
    )


def evaluate_training_candidate(
    candidate: TrainingCandidate,
    *,
    min_confidence: float = 0.75,
    anti_sycophancy_decision: AntiSycophancyGateDecision | None = None,
    allow_reference_only_anti_sycophancy: bool = False,
) -> PersonalizationDecision:
    """Fail closed unless a stable behavior preference satisfies every gate.

    Returns:
        PersonalizationDecision value produced by evaluate_training_candidate().
    """
    blockers: list[str] = []
    anti_sycophancy_evidence: Any = "missing_decision"

    if candidate.input_kind is CandidateInputKind.RAW_USER_LOG:
        blockers.append(BLOCKER_RAW_USER_LOG)
    if candidate.input_kind is CandidateInputKind.RAW_PROMPT:
        blockers.append(BLOCKER_RAW_USER_LOG)
    if candidate.input_kind is CandidateInputKind.RAW_TRANSCRIPT_SNIPPET:
        blockers.append(BLOCKER_RAW_USER_LOG)
    if candidate.input_kind is CandidateInputKind.PROFILE_FACT:
        blockers.append(BLOCKER_PROFILE_FACT)
    if candidate.input_kind is CandidateInputKind.SENSITIVE_CONTEXT:
        blockers.append(BLOCKER_SENSITIVE_CONTEXT)
    if candidate.target is TrainingPromotionTarget.OPAQUE_MODEL_WEIGHTS:
        blockers.append(BLOCKER_OPAQUE_MODEL_WEIGHTS)
    if candidate.input_kind is not CandidateInputKind.STABLE_BEHAVIOR_PREFERENCE:
        blockers.append("input_kind_not_stable_behavior_preference")
    if not candidate.stable_preference:
        blockers.append("behavior_preference_not_stable")
    if candidate.confidence < min_confidence:
        blockers.append(BLOCKER_MISSING_CONFIDENCE)
    if not candidate.audit_trail:
        blockers.append(BLOCKER_MISSING_AUDIT_TRAIL)
    if not candidate.provenance:
        blockers.append(BLOCKER_MISSING_PROVENANCE)
    blockers.extend(candidate.governance.blockers())
    dependency_refs = candidate.governance.dependency_refs
    if isinstance(dependency_refs, DependencyGateRefs) and not _has_text(
        dependency_refs.downstream_anti_sycophancy_gate_ref
    ):
        blockers.append(BLOCKER_DOWNSTREAM_GATE_MISSING)
    if allow_reference_only_anti_sycophancy:
        anti_sycophancy_evidence = "dependency_ref_only"
    elif anti_sycophancy_decision is None:
        blockers.append(BLOCKER_DOWNSTREAM_GATE_MISSING)
    elif not isinstance(anti_sycophancy_decision, AntiSycophancyGateDecision):
        blockers.append(BLOCKER_DOWNSTREAM_GATE_MISSING)
        anti_sycophancy_evidence = "invalid_decision"
    elif not anti_sycophancy_decision.approved:
        blockers.append(BLOCKER_DOWNSTREAM_GATE_FAILED)
        blockers.extend(f"anti_sycophancy:{blocker}" for blocker in anti_sycophancy_decision.blockers)
        anti_sycophancy_evidence = anti_sycophancy_decision.to_dict()
    else:
        anti_sycophancy_evidence = anti_sycophancy_decision.to_dict()

    unique_blockers = tuple(dict.fromkeys(blockers))
    return PersonalizationDecision(
        subject_id=candidate.candidate_id,
        status=(
            PersonalizationDecisionStatus.BLOCKED
            if unique_blockers
            else PersonalizationDecisionStatus.TRAINING_CANDIDATE_APPROVED
        ),
        approved=not unique_blockers,
        blockers=unique_blockers,
        audit_trail=candidate.audit_trail,
        evidence={
            "schema_version": SCHEMA_VERSION,
            "input_kind": candidate.input_kind.value,
            "target": candidate.target.value,
            "source_signal_ref": candidate.source_signal_ref,
            "profile_facts_trainable": False,
            "raw_logs_trainable": False,
            "downstream_anti_sycophancy_gate": anti_sycophancy_evidence,
        },
    )


def recovery_needed_decision(subject_id: str, blocker: str, message: str) -> PersonalizationDecision:
    """Build a typed recovery-needed decision for state/policy failures.

    Args:
        subject_id: Subject id value consumed by recovery_needed_decision().
        blocker: Blocker value consumed by recovery_needed_decision().
        message: Message value consumed by recovery_needed_decision().

    Returns:
        PersonalizationDecision value produced by recovery_needed_decision().
    """
    _require_text(subject_id, "subject_id")
    _require_text(blocker, "blocker")
    _require_text(message, "message")
    return PersonalizationDecision(
        subject_id=subject_id,
        status=PersonalizationDecisionStatus.RECOVERY_NEEDED,
        approved=False,
        blockers=(blocker,),
        audit_trail=(),
        evidence={"message": message, "schema_version": SCHEMA_VERSION},
    )


__all__ = CONTRACT_PUBLIC_EXPORTS
