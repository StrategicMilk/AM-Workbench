"""Fail-closed user-correction decomposition for AM Workbench.

The module is import-safe and writes no state. Callers pass one explicit user
correction plus the governance, consent, scope, and budget signals that make
downstream reuse safe. The result records both emitted derivatives and every
derivative that was suppressed.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.workbench.correction_decomposer import builders as _builders

logger = logging.getLogger(__name__)


class CorrectionKind(str, Enum):
    """User correction classes accepted by the decomposer."""

    EDIT = "edit"
    REJECTION = "rejection"
    APPROVAL = "approval"
    RUBRIC_SCORE = "rubric_score"
    CORRECTED_OUTPUT = "corrected_output"


class CorrectionVisibility(str, Enum):
    """Data visibility controls for downstream reuse."""

    PUBLIC = "public"
    INTERNAL = "internal"
    PRIVATE = "private"
    RESTRICTED = "restricted"


class CorrectionDecompositionStatus(str, Enum):
    """Outcome status for a decomposition request."""

    READY = "ready"
    BLOCKED = "blocked"
    DEGRADED = "degraded"


DERIVATIVE_KINDS: tuple[str, ...] = (
    "feedback_signal",
    "corrected_output",
    "preference_candidate",
    "eval_case",
    "failure_label",
    "method_update_candidate",
    "source_update_candidate",
    "tool_card_update_candidate",
    "annotation_task",
    "training_candidate",
)


@dataclass(frozen=True, slots=True)
class CorrectionConsent:
    """Consent switches for each downstream use family."""

    feedback_allowed: bool = True
    eval_allowed: bool = True
    preference_allowed: bool = False
    annotation_allowed: bool = False
    training_allowed: bool = False

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CorrectionConsent(feedback_allowed={self.feedback_allowed!r}, eval_allowed={self.eval_allowed!r}, preference_allowed={self.preference_allowed!r})"


@dataclass(frozen=True, slots=True)
class CorrectionSafetyReview:
    """Safety and redaction proof available for a user correction."""

    safe_for_eval: bool
    safe_for_training: bool
    failure_labels_allowed: bool
    redaction_ref: str = ""
    safety_review_ref: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CorrectionSafetyReview(safe_for_eval={self.safe_for_eval!r}, safe_for_training={self.safe_for_training!r}, failure_labels_allowed={self.failure_labels_allowed!r})"


@dataclass(frozen=True, slots=True)
class CorrectionBudget:
    """Budget controls for derivative fanout."""

    derivative_slots: int
    training_budget_available: bool


@dataclass(frozen=True, slots=True)
class CorrectionAuthority:
    """Authority references needed before producing higher-impact derivatives."""

    feedback_authority_ref: str
    preference_approval_ref: str = ""
    annotation_approval_ref: str = ""
    training_approval_ref: str = ""
    governance_policy_ref: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CorrectionAuthority(feedback_authority_ref={self.feedback_authority_ref!r}, preference_approval_ref={self.preference_approval_ref!r}, annotation_approval_ref={self.annotation_approval_ref!r})"


@dataclass(frozen=True, slots=True)
class CorrectionScope:
    """Explicit derivative scope and target refs supplied by the caller."""

    allowed_derivatives: tuple[str, ...] = DERIVATIVE_KINDS
    affected_method_ref: str = ""
    affected_source_ref: str = ""
    affected_tool_card_ref: str = ""
    task_shape: str = ""
    model_version: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CorrectionScope(allowed_derivatives={self.allowed_derivatives!r}, affected_method_ref={self.affected_method_ref!r}, affected_source_ref={self.affected_source_ref!r})"


@dataclass(frozen=True, slots=True)
class UserCorrection:
    """One user edit, rejection, approval, rubric score, or corrected output."""

    correction_id: str
    kind: CorrectionKind | str
    project_id: str
    actor_id: str
    summary: str
    target_ref: str
    original_output_ref: str
    corrected_output: str
    evidence_refs: tuple[str, ...]
    provenance_ref: str
    captured_at_utc: str
    confidence: float | int | str
    visibility: CorrectionVisibility | str
    contains_private_content: bool
    persisted_state_ref: str
    consent: CorrectionConsent
    safety: CorrectionSafetyReview | None
    authority: CorrectionAuthority | None
    budget: CorrectionBudget | None
    scope: CorrectionScope
    rubric_score: float | int | None = None

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"UserCorrection(correction_id={self.correction_id!r}, kind={self.kind!r}, project_id={self.project_id!r})"
        )


@dataclass(frozen=True, slots=True)
class CorrectionDerivative:
    """One downstream artifact drafted from a user correction."""

    kind: str
    artifact_id: str
    target_ref: str
    payload: dict[str, Any]
    consent_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "artifact_id": self.artifact_id,
            "target_ref": self.target_ref,
            "payload": self.payload,
            "consent_refs": list(self.consent_refs),
            "evidence_refs": list(self.evidence_refs),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CorrectionDerivative(kind={self.kind!r}, artifact_id={self.artifact_id!r}, target_ref={self.target_ref!r})"


@dataclass(frozen=True, slots=True)
class CorrectionSuppression:
    """One derivative that was intentionally not emitted."""

    kind: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class CorrectionDecomposition:
    """Complete decomposition result with emitted and suppressed derivatives."""

    correction_id: str
    status: CorrectionDecompositionStatus
    derivatives: tuple[CorrectionDerivative, ...]
    suppressed: tuple[CorrectionSuppression, ...]
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "correction_id": self.correction_id,
            "status": self.status.value,
            "derivatives": [derivative.to_dict() for derivative in self.derivatives],
            "suppressed": [suppression.to_dict() for suppression in self.suppressed],
            "blockers": list(self.blockers),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CorrectionDecomposition(correction_id={self.correction_id!r}, status={self.status!r}, derivatives={self.derivatives!r})"


_builders.configure_builders(CorrectionDerivative, CorrectionKind)
_build_feedback_signal = _builders._build_feedback_signal
_build_corrected_output = _builders._build_corrected_output
_build_preference_candidate = _builders._build_preference_candidate
_build_eval_case = _builders._build_eval_case
_build_failure_label = _builders._build_failure_label
_build_method_update_candidate = _builders._build_method_update_candidate
_build_source_update_candidate = _builders._build_source_update_candidate
_build_tool_card_update_candidate = _builders._build_tool_card_update_candidate
_build_annotation_task = _builders._build_annotation_task
_build_training_candidate = _builders._build_training_candidate
_is_positive_correction = _builders._is_positive_correction
_coerce_confidence = _builders._coerce_confidence


def decompose_user_correction(correction: UserCorrection) -> CorrectionDecomposition:
    """Turn one trusted user correction into every safe downstream artifact.

    Returns:
        CorrectionDecomposition value produced by decompose_user_correction().
    """
    base_blockers = _base_blockers(correction)
    if base_blockers:
        return CorrectionDecomposition(
            correction_id=correction.correction_id,
            status=CorrectionDecompositionStatus.BLOCKED,
            derivatives=(),
            suppressed=tuple(CorrectionSuppression(kind, base_blockers) for kind in DERIVATIVE_KINDS),
            blockers=base_blockers,
        )

    emitted: list[CorrectionDerivative] = []
    suppressed: list[CorrectionSuppression] = []
    remaining_slots = correction.budget.derivative_slots if correction.budget is not None else 0

    for kind, builder in (
        ("feedback_signal", _build_feedback_signal),
        ("corrected_output", _build_corrected_output),
        ("preference_candidate", _build_preference_candidate),
        ("eval_case", _build_eval_case),
        ("failure_label", _build_failure_label),
        ("method_update_candidate", _build_method_update_candidate),
        ("source_update_candidate", _build_source_update_candidate),
        ("tool_card_update_candidate", _build_tool_card_update_candidate),
        ("annotation_task", _build_annotation_task),
        ("training_candidate", _build_training_candidate),
    ):
        reasons = _suppression_reasons(correction, kind, remaining_slots)
        if reasons:
            suppressed.append(CorrectionSuppression(kind, reasons))
            continue
        derivative = builder(correction)
        emitted.append(derivative)
        remaining_slots -= 1

    status = CorrectionDecompositionStatus.READY if emitted else CorrectionDecompositionStatus.BLOCKED
    if emitted and suppressed:
        status = CorrectionDecompositionStatus.DEGRADED
    return CorrectionDecomposition(
        correction_id=correction.correction_id,
        status=status,
        derivatives=tuple(emitted),
        suppressed=tuple(suppressed),
        blockers=tuple(reason for item in suppressed for reason in item.reasons),
    )


def _base_blockers(correction: UserCorrection) -> tuple[str, ...]:
    blockers: list[str] = [
        f"{field_name}_missing"
        for field_name in (
            "correction_id",
            "project_id",
            "actor_id",
            "summary",
            "target_ref",
            "original_output_ref",
            "provenance_ref",
            "persisted_state_ref",
        )
        if not str(getattr(correction, field_name, "")).strip()
    ]
    if not _non_empty_tuple(correction.evidence_refs):
        blockers.append("evidence_refs_missing")
    try:
        CorrectionKind(correction.kind)
    except ValueError:
        blockers.append("correction_kind_unknown")
    try:
        CorrectionVisibility(correction.visibility)
    except ValueError:
        blockers.append("visibility_unknown")
    try:
        confidence = _coerce_confidence(correction.confidence)
    except ValueError:
        confidence = None
    if confidence is None:
        blockers.append("confidence_invalid")
    if _parse_utc(correction.captured_at_utc) is None:
        blockers.append("captured_at_utc_invalid")
    if correction.safety is None:
        blockers.append("safety_review_missing")
    elif not correction.safety.safety_review_ref.strip():
        blockers.append("safety_review_ref_missing")
    if correction.budget is None:
        blockers.append("budget_missing")
    elif correction.budget.derivative_slots <= 0:
        blockers.append("derivative_budget_unavailable")
    if correction.authority is None:
        blockers.append("authority_missing")
    elif not correction.authority.feedback_authority_ref.strip():
        blockers.append("feedback_authority_missing")
    if not correction.scope.task_shape.strip():
        blockers.append("task_shape_missing")
    if not correction.scope.model_version.strip():
        blockers.append("model_version_missing")
    return tuple(dict.fromkeys(blockers))


def _suppression_reasons(correction: UserCorrection, kind: str, remaining_slots: int) -> tuple[str, ...]:
    reasons: list[str] = []
    if kind not in correction.scope.allowed_derivatives:
        reasons.append("outside_requested_scope")
    if remaining_slots <= 0:
        reasons.append("derivative_budget_exhausted")
    if kind == "feedback_signal" and not correction.consent.feedback_allowed:
        reasons.append("feedback_consent_missing")
    if kind == "corrected_output" and not correction.corrected_output.strip():
        reasons.append("corrected_output_missing")
    if kind == "preference_candidate":
        if not correction.consent.preference_allowed:
            reasons.append("preference_consent_missing")
        if not correction.authority or not correction.authority.preference_approval_ref.strip():
            reasons.append("preference_authority_missing")
    if kind == "eval_case":
        if not correction.consent.eval_allowed:
            reasons.append("eval_consent_missing")
        if not correction.safety or not correction.safety.safe_for_eval:
            reasons.append("eval_safety_not_approved")
    if kind == "failure_label":
        if not correction.safety or not correction.safety.failure_labels_allowed:
            reasons.append("failure_label_authority_missing")
        if _is_positive_correction(correction):
            reasons.append("no_failure_signal")
    if kind == "method_update_candidate" and not correction.scope.affected_method_ref.strip():
        reasons.append("method_ref_missing")
    if kind == "source_update_candidate" and not correction.scope.affected_source_ref.strip():
        reasons.append("source_ref_missing")
    if kind == "tool_card_update_candidate" and not correction.scope.affected_tool_card_ref.strip():
        reasons.append("tool_card_ref_missing")
    if kind == "annotation_task":
        if not correction.consent.annotation_allowed:
            reasons.append("annotation_consent_missing")
        if not correction.authority or not correction.authority.annotation_approval_ref.strip():
            reasons.append("annotation_authority_missing")
    if kind == "training_candidate":
        reasons.extend(_training_suppression_reasons(correction))
    return tuple(dict.fromkeys(reasons))


def _training_suppression_reasons(correction: UserCorrection) -> tuple[str, ...]:
    reasons: list[str] = []
    if not correction.consent.training_allowed:
        reasons.append("training_consent_missing")
    if not correction.authority or not correction.authority.training_approval_ref.strip():
        reasons.append("training_authority_missing")
    if not correction.budget or not correction.budget.training_budget_available:
        reasons.append("training_budget_unavailable")
    if not correction.safety or not correction.safety.safe_for_training:
        reasons.append("training_safety_not_approved")
    if not correction.safety or not correction.safety.redaction_ref.strip():
        reasons.append("redaction_ref_missing")
    visibility = CorrectionVisibility(correction.visibility)
    private_content = correction.contains_private_content or visibility in {
        CorrectionVisibility.PRIVATE,
        CorrectionVisibility.RESTRICTED,
    }
    if private_content and (not correction.authority or not correction.authority.training_approval_ref.strip()):
        reasons.append("private_content_training_requires_explicit_approval")
    return tuple(dict.fromkeys(reasons))


def _non_empty_tuple(values: tuple[str, ...]) -> bool:
    return isinstance(values, tuple) and any(str(value).strip() for value in values)


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def correction_to_dict(correction: UserCorrection) -> dict[str, Any]:
    """Return a schema-shaped request payload for diagnostics and fixtures.

    Returns:
        dict[str, Any] value produced by correction_to_dict().
    """
    payload = asdict(correction)
    payload["kind"] = CorrectionKind(correction.kind).value
    payload["visibility"] = CorrectionVisibility(correction.visibility).value
    return payload


__all__ = [
    "DERIVATIVE_KINDS",
    "CorrectionAuthority",
    "CorrectionBudget",
    "CorrectionConsent",
    "CorrectionDecomposition",
    "CorrectionDecompositionStatus",
    "CorrectionDerivative",
    "CorrectionKind",
    "CorrectionSafetyReview",
    "CorrectionScope",
    "CorrectionSuppression",
    "CorrectionVisibility",
    "UserCorrection",
    "correction_to_dict",
    "decompose_user_correction",
]
