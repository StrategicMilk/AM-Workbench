"""Governed adaptive tuning contracts for AM Workbench."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.api.responses import json_safe as _json_safe


class AdaptiveTuningError(ValueError):
    """Raised when adaptive tuning state cannot be trusted."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


class FrictionSignalKind(str, Enum):
    """Friction signal kinds accepted by the adaptive tuning layer."""

    WRONG_MENU_NAVIGATION = "wrong_menu_navigation"
    MISCLICK = "misclick"
    ABANDONED_FLOW = "abandoned_flow"
    LONG_PAUSE = "long_pause"
    REOPENED_FLOW = "reopened_flow"
    PINNED_SURFACE = "pinned_surface"
    UNDO_RETRY_LOOP = "undo_retry_loop"
    PROMPT_CORRECTION = "prompt_correction"
    REGENERATED_OUTPUT = "regenerated_output"
    ACCEPTED_OUTPUT = "accepted_output"
    APPROVAL_HABIT = "approval_habit"
    SEARCHED_SETTINGS = "searched_settings"
    HARDWARE_ADVISORY = "hardware_advisory"
    NETWORK_ADVISORY = "network_advisory"


class EvidenceStatus(str, Enum):
    """Trust state for one normalized evidence item."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class EvidenceBlocker(str, Enum):
    """Machine-readable fail-closed evidence blockers."""

    MISSING_EVIDENCE = "missing_evidence"
    MISSING_SCOPE = "missing_scope"
    MISSING_PROVENANCE = "missing_provenance"
    PRIVATE_EVIDENCE = "private_evidence"
    STALE_EVIDENCE = "stale_evidence"
    UNREADABLE_EVIDENCE = "unreadable_evidence"
    LOW_CONFIDENCE = "low_confidence"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence"
    DENIED_AUTHORITY = "denied_authority"
    INVALID_TIMESTAMP = "invalid_timestamp"
    HOST_OR_NETWORK_MUTATION = "host_or_network_mutation"


class AdaptationTarget(str, Enum):
    """Surfaces an adaptive proposal may mention."""

    LOCAL_UI_DEFAULT = "local_ui_default"
    LOCAL_SHORTCUT = "local_shortcut"
    LOCAL_REVIEW_LAYOUT = "local_review_layout"
    PROFILE_FACT = "profile_fact"
    SENSITIVE_CONTEXT = "sensitive_context"
    FACTUAL_TRUTH = "factual_truth"
    TRAINING_DATUM = "training_datum"
    MODEL_ROUTE = "model_route"
    PROJECT_DEFAULT = "project_default"
    AUTOMATION = "automation"
    AGENT_ROUTE = "agent_route"
    RESOURCE_POLICY = "resource_policy"
    NETWORK_ROUTE = "network_route"
    HOST_SETTING = "host_setting"
    OS_SETTING = "os_setting"


PROTECTED_SILENT_TARGETS: frozenset[AdaptationTarget] = frozenset({
    AdaptationTarget.PROFILE_FACT,
    AdaptationTarget.SENSITIVE_CONTEXT,
    AdaptationTarget.FACTUAL_TRUTH,
    AdaptationTarget.TRAINING_DATUM,
    AdaptationTarget.MODEL_ROUTE,
    AdaptationTarget.PROJECT_DEFAULT,
    AdaptationTarget.AUTOMATION,
    AdaptationTarget.AGENT_ROUTE,
    AdaptationTarget.RESOURCE_POLICY,
    AdaptationTarget.NETWORK_ROUTE,
    AdaptationTarget.HOST_SETTING,
    AdaptationTarget.OS_SETTING,
})
HOST_MUTATION_TARGETS: frozenset[AdaptationTarget] = frozenset({
    AdaptationTarget.HOST_SETTING,
    AdaptationTarget.OS_SETTING,
    AdaptationTarget.NETWORK_ROUTE,
})


class RiskTier(str, Enum):
    """Risk tier for proposed adaptive changes."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class HypothesisStatus(str, Enum):
    """Lifecycle state for an adaptive hypothesis."""

    PENDING = "pending"
    ACTIVE = "active"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    FORGOTTEN = "forgotten"
    DECAYED = "decayed"
    REVOKED = "revoked"


class ControlAction(str, Enum):
    """Explicit review controls exposed to users."""

    ALLOW = "allow"
    REJECT = "reject"
    EDIT = "edit"
    FORGET = "forget"
    REVOKE = "revoke"
    PREVIEW = "preview"
    ROLLBACK = "rollback"
    POLICY_OVERRIDE = "policy_override"


class ProposalState(str, Enum):
    """Admission state for a proposal."""

    BLOCKED = "blocked"
    NEEDS_PREVIEW = "needs_preview"
    NEEDS_APPROVAL = "needs_approval"
    NEEDS_TESTS = "needs_tests"
    NEEDS_ROLLBACK = "needs_rollback"
    NEEDS_PROMOTION_EVIDENCE = "needs_promotion_evidence"
    AUTO_APPLICABLE = "auto_applicable"
    APPROVED = "approved"


@dataclass(frozen=True, slots=True)
class EvidenceScope:
    """Bounded scope where one evidence item may be used."""

    project_id: str
    surface: str
    workflow_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.project_id, "project_id")
        _require_text(self.surface, "surface")

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)


@dataclass(frozen=True, slots=True)
class FrictionObservation:
    """Raw local observation before fail-closed normalization."""

    observation_id: str
    kind: FrictionSignalKind | str
    summary: str
    scope: EvidenceScope | None
    observed_at_utc: str
    confidence: float
    evidence_refs: tuple[str, ...]
    provenance_ref: str
    private: bool = False
    unreadable: bool = False
    contradicted_by: tuple[str, ...] = ()
    denied: bool = False
    target: AdaptationTarget | str | None = None

    def __post_init__(self) -> None:
        _require_text(self.observation_id, "observation_id")
        _require_text(self.summary, "summary")
        object.__setattr__(self, "kind", _coerce_enum(FrictionSignalKind, self.kind, "signal-kind-unknown"))
        if self.target is not None:
            object.__setattr__(self, "target", _coerce_enum(AdaptationTarget, self.target, "target-unknown"))
        _parse_utc(self.observed_at_utc, "observed_at_utc")
        _require_confidence(self.confidence, "confidence")

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"FrictionObservation(observation_id={self.observation_id!r}, kind={self.kind!r}, summary={self.summary!r})"
        )


@dataclass(frozen=True, slots=True)
class NormalizedEvidence:
    """Fail-closed evidence item used by hypotheses."""

    evidence_id: str
    kind: FrictionSignalKind
    summary: str
    scope: EvidenceScope | None
    observed_at_utc: str
    confidence: float
    evidence_refs: tuple[str, ...]
    provenance_ref: str
    status: EvidenceStatus
    blockers: tuple[EvidenceBlocker, ...] = ()
    target: AdaptationTarget | None = None

    def __post_init__(self) -> None:
        _require_text(self.evidence_id, "evidence_id")
        _require_text(self.summary, "summary")
        if not isinstance(self.kind, FrictionSignalKind):
            raise AdaptiveTuningError("signal-kind-invalid")
        _parse_utc(self.observed_at_utc, "observed_at_utc")
        _require_confidence(self.confidence, "confidence")
        if self.status is EvidenceStatus.ACCEPTED:
            if self.scope is None:
                raise AdaptiveTuningError("accepted-evidence-missing-scope", self.evidence_id)
            _require_text_tuple(self.evidence_refs, "evidence_refs")
            _require_text(self.provenance_ref, "provenance_ref")
            if self.blockers:
                raise AdaptiveTuningError("accepted-evidence-has-blockers", self.evidence_id)

    @property
    def accepted(self) -> bool:
        """Whether the evidence is usable."""
        return self.status is EvidenceStatus.ACCEPTED

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"NormalizedEvidence(evidence_id={self.evidence_id!r}, kind={self.kind!r}, summary={self.summary!r})"


@dataclass(frozen=True, slots=True)
class RollbackRequirement:
    """Rollback proof required before high-risk proposal activation."""

    required: bool
    rollback_ref: str = ""
    readiness_checked: bool = False

    def satisfied(self) -> bool:
        """Return whether rollback evidence exists when required."""
        return not self.required or (bool(self.rollback_ref.strip()) and self.readiness_checked)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    """Before/after evidence for governed promotion."""

    baseline_ref: str
    candidate_ref: str
    measured_at_utc: str
    representative: bool
    outcome_positive: bool
    skipped: bool = False

    def __post_init__(self) -> None:
        _require_text(self.baseline_ref, "baseline_ref")
        _require_text(self.candidate_ref, "candidate_ref")
        _parse_utc(self.measured_at_utc, "measured_at_utc")

    def trusted(self, *, now_utc: datetime, stale_after_days: int) -> bool:
        """Return whether promotion evidence is fresh, representative, and positive.

        Returns:
            bool value produced by trusted().
        """
        measured = _parse_utc(self.measured_at_utc, "measured_at_utc")
        return (
            not self.skipped
            and self.representative
            and self.outcome_positive
            and (now_utc - measured).days <= stale_after_days
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PromotionEvidence(baseline_ref={self.baseline_ref!r}, candidate_ref={self.candidate_ref!r}, measured_at_utc={self.measured_at_utc!r})"


@dataclass(frozen=True, slots=True)
class PreviewPacket:
    """Inspectable preview data for a proposed change."""

    proposal_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    changed_dimensions: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.proposal_id, "proposal_id")
        if not self.changed_dimensions:
            raise AdaptiveTuningError("preview-changes-missing", self.proposal_id)

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PreviewPacket(proposal_id={self.proposal_id!r}, before={self.before!r}, after={self.after!r})"


@dataclass(frozen=True, slots=True)
class LocalChangeProposal:
    """Reviewable proposal derived from a hypothesis."""

    proposal_id: str
    hypothesis_id: str
    target: AdaptationTarget | str
    risk_tier: RiskTier | str
    title: str
    summary: str
    preview: PreviewPacket | None
    approval_ref: str = ""
    tests_ref: str = ""
    rollback: RollbackRequirement = field(default_factory=lambda: RollbackRequirement(required=True))
    promotion_evidence: PromotionEvidence | None = None
    requested_auto_apply: bool = False

    def __post_init__(self) -> None:
        _require_text(self.proposal_id, "proposal_id")
        _require_text(self.hypothesis_id, "hypothesis_id")
        _require_text(self.title, "title")
        _require_text(self.summary, "summary")
        object.__setattr__(self, "target", _coerce_enum(AdaptationTarget, self.target, "target-unknown"))
        object.__setattr__(self, "risk_tier", _coerce_enum(RiskTier, self.risk_tier, "risk-tier-unknown"))

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"LocalChangeProposal(proposal_id={self.proposal_id!r}, hypothesis_id={self.hypothesis_id!r}, target={self.target!r})"


@dataclass(frozen=True, slots=True)
class AdaptiveHypothesis:
    """Inspectable hypothesis created from repeated friction evidence."""

    hypothesis_id: str
    title: str
    status: HypothesisStatus | str
    scope: EvidenceScope
    evidence: tuple[NormalizedEvidence, ...]
    confidence: float
    created_at_utc: str
    last_observed_at_utc: str
    decay_after_days: int
    controls: tuple[ControlAction, ...] = tuple(ControlAction)
    proposal: LocalChangeProposal | None = None
    fail_closed_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.hypothesis_id, "hypothesis_id")
        _require_text(self.title, "title")
        object.__setattr__(self, "status", _coerce_enum(HypothesisStatus, self.status, "hypothesis-status-unknown"))
        if not self.evidence:
            raise AdaptiveTuningError("hypothesis-evidence-missing", self.hypothesis_id)
        if any(not item.accepted for item in self.evidence):
            raise AdaptiveTuningError("hypothesis-rejected-evidence", self.hypothesis_id)
        _require_confidence(self.confidence, "confidence")
        _parse_utc(self.created_at_utc, "created_at_utc")
        _parse_utc(self.last_observed_at_utc, "last_observed_at_utc")
        if self.decay_after_days <= 0:
            raise AdaptiveTuningError("decay-window-invalid", self.hypothesis_id)
        if set(self.controls) != set(ControlAction):
            raise AdaptiveTuningError("hypothesis-controls-incomplete", self.hypothesis_id)

    def decayed(self, *, now_utc: datetime) -> bool:
        """Return whether the hypothesis has aged out.

        Returns:
            bool value produced by decayed().
        """
        observed = _parse_utc(self.last_observed_at_utc, "last_observed_at_utc")
        return (now_utc - observed).days > self.decay_after_days

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AdaptiveHypothesis(hypothesis_id={self.hypothesis_id!r}, title={self.title!r}, status={self.status!r})"


@dataclass(frozen=True, slots=True)
class UserControlDecision:
    """Persisted user control action."""

    hypothesis_id: str
    action: ControlAction | str
    decided_at_utc: str
    actor_ref: str
    rationale: str = ""

    def __post_init__(self) -> None:
        _require_text(self.hypothesis_id, "hypothesis_id")
        object.__setattr__(self, "action", _coerce_enum(ControlAction, self.action, "control-action-unknown"))
        _parse_utc(self.decided_at_utc, "decided_at_utc")
        _require_text(self.actor_ref, "actor_ref")

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UserControlDecision(hypothesis_id={self.hypothesis_id!r}, action={self.action!r}, decided_at_utc={self.decided_at_utc!r})"


@dataclass(frozen=True, slots=True)
class AdaptiveTuningPolicyDecision:
    """Fail-closed admission decision for a proposal."""

    proposal_id: str
    state: ProposalState
    allowed: bool
    blockers: tuple[str, ...]
    required_authority: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AdaptiveTuningPolicyDecision(proposal_id={self.proposal_id!r}, state={self.state!r}, allowed={self.allowed!r})"


def _coerce_enum(enum_type: type[Enum], value: Enum | str, reason: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return enum_type(str(raw_value))
    except ValueError as exc:
        raise AdaptiveTuningError(reason, str(value)) from exc


def _parse_utc(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise AdaptiveTuningError("timestamp-required", field_name)
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise AdaptiveTuningError("timestamp-invalid", field_name) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AdaptiveTuningError("text-required", field_name)


def _require_text_tuple(value: tuple[str, ...], field_name: str) -> None:
    if (
        not isinstance(value, tuple)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise AdaptiveTuningError("text-tuple-required", field_name)


def _require_confidence(value: object, field_name: str) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
        raise AdaptiveTuningError("confidence-invalid", field_name)
