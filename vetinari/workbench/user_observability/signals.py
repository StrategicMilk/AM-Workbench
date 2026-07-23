"""Fail-closed user-input observability contracts for AM Workbench."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any

logger = logging.getLogger(__name__)


class UserSignalKind(str, Enum):
    """User-signal categories promoted by the Workbench charter."""

    FRICTION = "friction"
    REPEATED_WORKFLOW = "repeated_workflow"
    PREFERENCE_CANDIDATE = "preference_candidate"
    IGNORED_RECOMMENDATION = "ignored_recommendation"
    DELEGATION_DESIRE = "delegation_desire"
    TRUST_BOUNDARY = "trust_boundary"
    QUESTION_DEBT = "question_debt"
    RECOMMENDATION_FEEDBACK = "recommendation_feedback"
    EFFORT_ACCOUNTING = "effort_accounting"


class UserSignalSource(str, Enum):
    """How the signal was observed."""

    EXPLICIT = "explicit"
    IMPLICIT = "implicit"


class UserSignalAction(str, Enum):
    """Actions a caller may ask the observability layer to take."""

    DRAFT = "draft"
    QUEUE_REVIEW = "queue_review"
    ACTIVATE = "activate"


class UserSignalBlocker(str, Enum):
    """Machine-readable fail-closed blockers."""

    COLLECTION_DISABLED = "collection_disabled"
    REVOKED_SIGNAL = "revoked_signal"
    OPTED_OUT_KIND = "opted_out_kind"
    MISSING_EVIDENCE = "missing_evidence"
    MISSING_PROVENANCE = "missing_provenance"
    MISSING_AUTHORITY = "missing_authority"
    INVALID_KIND = "invalid_kind"
    INVALID_SOURCE = "invalid_source"
    INVALID_ACTION = "invalid_action"
    INVALID_CONFIDENCE = "invalid_confidence"
    INVALID_TIMESTAMP = "invalid_timestamp"
    STALE_PROVENANCE = "stale_provenance"
    NON_LOCAL_STORAGE = "non_local_storage"
    INVALID_RETENTION = "invalid_retention"
    IMPLICIT_COLLECTION_DISABLED = "implicit_collection_disabled"
    SILENT_HIGH_IMPACT_ACTIVATION = "silent_high_impact_activation"
    SENSITIVE_WITHOUT_LOCAL_ONLY = "sensitive_without_local_only"


@dataclass(frozen=True, slots=True)
class UserSignalPolicy:
    """Privacy, retention, opt-out, and activation guardrails."""

    collection_enabled: bool = True
    implicit_collection_enabled: bool = True
    local_only: bool = True
    retention_days: int = 30
    revoked_signal_ids: tuple[str, ...] = ()
    opted_out_kinds: tuple[UserSignalKind | str, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UserSignalPolicy(collection_enabled={self.collection_enabled!r}, implicit_collection_enabled={self.implicit_collection_enabled!r}, local_only={self.local_only!r})"


@dataclass(frozen=True, slots=True)
class UserInputSignal:
    """One explicit or implicit signal about user friction or preferences."""

    signal_id: str
    kind: UserSignalKind | str
    source: UserSignalSource | str
    project_id: str
    actor_id: str
    summary: str
    evidence_refs: tuple[str, ...]
    captured_at_utc: str
    confidence: float | int | str
    provenance_ref: str
    authority_ref: str = ""
    target_ref: str = ""
    high_impact: bool = False
    contains_sensitive_data: bool = False
    requested_action: UserSignalAction | str = UserSignalAction.DRAFT
    effort_minutes: float | int = 0

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UserInputSignal(signal_id={self.signal_id!r}, kind={self.kind!r}, source={self.source!r})"


@dataclass(frozen=True, slots=True)
class UserSignalAssessment:
    """Fail-closed assessment of one signal."""

    signal_id: str
    kind: UserSignalKind | None
    source: UserSignalSource | None
    accepted: bool
    degraded: bool
    confidence: float | None
    blockers: tuple[UserSignalBlocker, ...]
    allowed_actions: tuple[UserSignalAction, ...]

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["kind"] = self.kind.value if self.kind else None
        payload["source"] = self.source.value if self.source else None
        payload["blockers"] = [blocker.value for blocker in self.blockers]
        payload["allowed_actions"] = [action.value for action in self.allowed_actions]
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UserSignalAssessment(signal_id={self.signal_id!r}, kind={self.kind!r}, source={self.source!r})"


@dataclass(frozen=True, slots=True)
class UserObservabilitySnapshot:
    """Operator-readable rollup for the Workbench user-observability panel."""

    project_id: str
    accepted_count: int
    degraded_count: int
    friction_map: tuple[dict[str, Any], ...] = ()
    automation_candidates: tuple[dict[str, Any], ...] = ()
    preference_drafts: tuple[dict[str, Any], ...] = ()
    frustration_ledger: tuple[dict[str, Any], ...] = ()
    trust_boundary_map: tuple[dict[str, Any], ...] = ()
    question_debt_meter: dict[str, Any] = field(default_factory=dict)
    recommendation_quality_dashboard: dict[str, Any] = field(default_factory=dict)
    user_effort_accounting: dict[str, Any] = field(default_factory=dict)
    degraded_signals: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UserObservabilitySnapshot(project_id={self.project_id!r}, accepted_count={self.accepted_count!r}, degraded_count={self.degraded_count!r})"


def assess_user_signal(
    signal: UserInputSignal,
    policy: UserSignalPolicy,
    *,
    now_utc: datetime | None = None,
) -> UserSignalAssessment:
    """Assess a signal and fail closed when privacy, evidence, or authority is missing.

    Args:
        signal: Signal value consumed by assess_user_signal().
        policy: Policy value consumed by assess_user_signal().
        now_utc: Now utc value consumed by assess_user_signal().

    Returns:
        UserSignalAssessment value produced by assess_user_signal().
    """
    blockers: list[UserSignalBlocker] = []
    kind = _coerce_kind(signal.kind, blockers)
    source = _coerce_source(signal.source, blockers)
    requested_action = _coerce_action(signal.requested_action, blockers)
    confidence = _coerce_confidence(signal.confidence, blockers)

    if not policy.collection_enabled:
        blockers.append(UserSignalBlocker.COLLECTION_DISABLED)
    if signal.signal_id in policy.revoked_signal_ids:
        blockers.append(UserSignalBlocker.REVOKED_SIGNAL)
    opted_out = {_enum_value(value) for value in policy.opted_out_kinds}
    if kind is not None and kind.value in opted_out:
        blockers.append(UserSignalBlocker.OPTED_OUT_KIND)
    if source is UserSignalSource.IMPLICIT and not policy.implicit_collection_enabled:
        blockers.append(UserSignalBlocker.IMPLICIT_COLLECTION_DISABLED)
    if not policy.local_only:
        blockers.append(UserSignalBlocker.NON_LOCAL_STORAGE)
    if policy.retention_days <= 0:
        blockers.append(UserSignalBlocker.INVALID_RETENTION)
    if signal.contains_sensitive_data and not policy.local_only:
        blockers.append(UserSignalBlocker.SENSITIVE_WITHOUT_LOCAL_ONLY)
    if not tuple(ref for ref in signal.evidence_refs if str(ref).strip()):
        blockers.append(UserSignalBlocker.MISSING_EVIDENCE)
    if not signal.provenance_ref.strip():
        blockers.append(UserSignalBlocker.MISSING_PROVENANCE)

    captured_at = _parse_utc(signal.captured_at_utc)
    if captured_at is None:
        blockers.append(UserSignalBlocker.INVALID_TIMESTAMP)
    else:
        current = now_utc if now_utc is not None else datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        if captured_at > current or (current - captured_at).days > policy.retention_days:
            blockers.append(UserSignalBlocker.STALE_PROVENANCE)

    implicit_or_high_impact = source is UserSignalSource.IMPLICIT or signal.high_impact
    if requested_action is UserSignalAction.ACTIVATE and implicit_or_high_impact:
        blockers.append(UserSignalBlocker.SILENT_HIGH_IMPACT_ACTIVATION)
    if signal.high_impact and not signal.authority_ref.strip():
        blockers.append(UserSignalBlocker.MISSING_AUTHORITY)

    allowed_actions = (UserSignalAction.DRAFT, UserSignalAction.QUEUE_REVIEW)
    if not implicit_or_high_impact and not blockers:
        allowed_actions = (*allowed_actions, UserSignalAction.ACTIVATE)

    unique_blockers = tuple(dict.fromkeys(blockers))
    return UserSignalAssessment(
        signal_id=signal.signal_id,
        kind=kind,
        source=source,
        accepted=not unique_blockers,
        degraded=bool(unique_blockers),
        confidence=confidence,
        blockers=unique_blockers,
        allowed_actions=allowed_actions,
    )


def build_user_observability_snapshot(
    signals: tuple[UserInputSignal, ...] | list[UserInputSignal],
    policy: UserSignalPolicy,
    *,
    project_id: str,
    now_utc: datetime | None = None,
) -> UserObservabilitySnapshot:
    """Build maps and dashboards from accepted signals without mutating defaults.

    Args:
        signals: Signals value consumed by build_user_observability_snapshot().
        policy: Policy value consumed by build_user_observability_snapshot().
        project_id: Project identifier that scopes the operation.
        now_utc: Now utc value consumed by build_user_observability_snapshot().

    Returns:
        Newly constructed user observability snapshot value.
    """
    accepted, degraded = _partition_signal_assessments(signals, policy, now_utc)
    friction = _accepted_rows(accepted, UserSignalKind.FRICTION)
    repeated = _automation_rows(accepted)
    preference_drafts = _accepted_rows(accepted, UserSignalKind.PREFERENCE_CANDIDATE)
    trust_boundaries = _accepted_rows(accepted, UserSignalKind.TRUST_BOUNDARY)
    question_debt = _accepted_rows(accepted, UserSignalKind.QUESTION_DEBT)
    recommendation_feedback = _accepted_rows(
        accepted,
        UserSignalKind.IGNORED_RECOMMENDATION,
        UserSignalKind.RECOMMENDATION_FEEDBACK,
    )
    effort_signals = [signal for signal, _assessment in accepted if float(signal.effort_minutes) > 0]

    total_effort = sum(max(float(signal.effort_minutes), 0.0) for signal in effort_signals)
    positive_feedback = sum(1 for row in recommendation_feedback if row["confidence"] >= 0.7)
    return UserObservabilitySnapshot(
        project_id=project_id,
        accepted_count=len(accepted),
        degraded_count=len(degraded),
        friction_map=tuple(friction),
        automation_candidates=tuple(repeated),
        preference_drafts=tuple(preference_drafts),
        frustration_ledger=(*friction, *question_debt),
        trust_boundary_map=tuple(trust_boundaries),
        question_debt_meter={
            "open_question_count": len(question_debt),
            "highest_confidence": max((row["confidence"] for row in question_debt), default=0.0),
        },
        recommendation_quality_dashboard={
            "feedback_count": len(recommendation_feedback),
            "positive_feedback_count": positive_feedback,
            "ignored_count": sum(
                1 for signal, assessment in accepted if assessment.kind is UserSignalKind.IGNORED_RECOMMENDATION
            ),
        },
        user_effort_accounting={
            "signal_count": len(effort_signals),
            "total_effort_minutes": total_effort,
        },
        degraded_signals=tuple(degraded),
    )


def _partition_signal_assessments(
    signals: tuple[UserInputSignal, ...] | list[UserInputSignal],
    policy: UserSignalPolicy,
    now_utc: datetime | None,
) -> tuple[list[tuple[UserInputSignal, UserSignalAssessment]], list[dict[str, Any]]]:
    accepted: list[tuple[UserInputSignal, UserSignalAssessment]] = []
    degraded: list[dict[str, Any]] = []
    for signal in signals:
        assessment = assess_user_signal(signal, policy, now_utc=now_utc)
        if assessment.accepted:
            accepted.append((signal, assessment))
        else:
            degraded.append({
                "signal_id": signal.signal_id,
                "summary": signal.summary,
                "blockers": [blocker.value for blocker in assessment.blockers],
            })
    return accepted, degraded


def _accepted_rows(
    accepted: list[tuple[UserInputSignal, UserSignalAssessment]],
    *kinds: UserSignalKind,
) -> list[dict[str, Any]]:
    return [_signal_row(signal, assessment) for signal, assessment in accepted if assessment.kind in kinds]


def _automation_rows(
    accepted: list[tuple[UserInputSignal, UserSignalAssessment]],
) -> list[dict[str, Any]]:
    return [
        _automation_candidate(signal, assessment)
        for signal, assessment in accepted
        if assessment.kind in {UserSignalKind.REPEATED_WORKFLOW, UserSignalKind.DELEGATION_DESIRE}
    ]


def _signal_row(signal: UserInputSignal, assessment: UserSignalAssessment) -> dict[str, Any]:
    return {
        "signal_id": signal.signal_id,
        "kind": assessment.kind.value if assessment.kind else None,
        "source": assessment.source.value if assessment.source else None,
        "summary": signal.summary,
        "target_ref": signal.target_ref,
        "confidence": assessment.confidence or 0.0,
        "evidence_refs": list(signal.evidence_refs),
        "allowed_actions": [action.value for action in assessment.allowed_actions],
    }


def _automation_candidate(signal: UserInputSignal, assessment: UserSignalAssessment) -> dict[str, Any]:
    row = _signal_row(signal, assessment)
    row["status"] = "approval_required" if signal.high_impact else "draft_only"
    row["activation_allowed"] = UserSignalAction.ACTIVATE in assessment.allowed_actions
    return row


def _enum_value(value: UserSignalKind | str) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _coerce_kind(value: UserSignalKind | str, blockers: list[UserSignalBlocker]) -> UserSignalKind | None:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return value if isinstance(value, UserSignalKind) else UserSignalKind(raw_value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        blockers.append(UserSignalBlocker.INVALID_KIND)
        return None


def _coerce_source(value: UserSignalSource | str, blockers: list[UserSignalBlocker]) -> UserSignalSource | None:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return value if isinstance(value, UserSignalSource) else UserSignalSource(raw_value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        blockers.append(UserSignalBlocker.INVALID_SOURCE)
        return None


def _coerce_action(value: UserSignalAction | str, blockers: list[UserSignalBlocker]) -> UserSignalAction | None:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return value if isinstance(value, UserSignalAction) else UserSignalAction(raw_value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        blockers.append(UserSignalBlocker.INVALID_ACTION)
        return None


def _coerce_confidence(value: float | int | str, blockers: list[UserSignalBlocker]) -> float | None:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        blockers.append(UserSignalBlocker.INVALID_CONFIDENCE)
        return None
    if not isfinite(confidence) or confidence < 0 or confidence > 1:
        blockers.append(UserSignalBlocker.INVALID_CONFIDENCE)
        return None
    return confidence


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "UserInputSignal",
    "UserObservabilitySnapshot",
    "UserSignalAction",
    "UserSignalAssessment",
    "UserSignalBlocker",
    "UserSignalKind",
    "UserSignalPolicy",
    "UserSignalSource",
    "assess_user_signal",
    "build_user_observability_snapshot",
]
