"""Typed contracts for the Workbench habit and health rhythm surface."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


class HabitHealthContractError(ValueError):
    """Raised when habit-health data cannot be trusted."""


class HabitHealthSignalKind(str, Enum):
    """Signal categories produced by the habit-health tracker."""

    ROUTINE = "routine"
    CHECK_IN = "check_in"
    MISSED_CADENCE = "missed_cadence"
    ENERGY_FOCUS = "energy_focus"
    AGENT_RHYTHM = "agent_rhythm"
    DOWNSTREAM_PREVIEW = "downstream_preview"
    PRIVACY_REVIEW = "privacy_review"


class HabitHealthScope(str, Enum):
    """Privacy scopes for habit-health records."""

    PERSONAL_WELLNESS = "personal_wellness"
    ROUTINE_CONTENT = "routine_content"
    PROJECT_TELEMETRY = "project_telemetry"
    AGENT_RUN_TELEMETRY = "agent_run_telemetry"
    SENSITIVE_MEMORY_CONTEXT = "sensitive_memory_context"
    UNKNOWN = "unknown"


class NonMedicalBoundary(str, Enum):
    """Statements the tracker is allowed to make about health-like records."""

    DOES_NOT_DIAGNOSE = "does_not_diagnose"
    DOES_NOT_PRESCRIBE = "does_not_prescribe"
    USER_FRAMED_CONTEXT_ONLY = "user_framed_context_only"


class FatigueRisk(str, Enum):
    """Informational user-rhythm labels, not medical conclusions."""

    UNKNOWN = "unknown"
    STEADY = "steady"
    NEEDS_REVIEW = "needs_review"
    HIGH_FRICTION = "high_friction"


@dataclass(frozen=True, slots=True)
class HabitCadence:
    """Expected cadence for a user-defined routine."""

    interval_hours: int
    grace_hours: int = 0
    quiet_window_start: str = ""
    quiet_window_end: str = ""

    def __post_init__(self) -> None:
        if self.interval_hours <= 0:
            raise HabitHealthContractError("cadence interval must be positive")
        if self.grace_hours < 0:
            raise HabitHealthContractError("cadence grace must be non-negative")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HabitCadence(interval_hours={self.interval_hours!r}, grace_hours={self.grace_hours!r}, quiet_window_start={self.quiet_window_start!r})"


@dataclass(frozen=True, slots=True)
class HabitRoutine:
    """A user-authored routine the tracker can monitor."""

    routine_id: str
    user_id: str
    name: str
    cadence: HabitCadence
    scope: HabitHealthScope | str = HabitHealthScope.PERSONAL_WELLNESS
    tags: tuple[str, ...] = ()
    intervention_threshold_missed: int = 2
    source_context: str = ""
    consent_refs: tuple[str, ...] = ()
    provenance_ref: str = ""
    created_at_utc: str = ""

    def __post_init__(self) -> None:
        _require_text(self.routine_id, "routine_id")
        _require_text(self.user_id, "user_id")
        _require_text(self.name, "name")
        object.__setattr__(self, "scope", _coerce_scope(self.scope))
        if self.intervention_threshold_missed < 1:
            raise HabitHealthContractError("intervention threshold must be positive")

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["scope"] = self.scope.value if isinstance(self.scope, HabitHealthScope) else str(self.scope)
        return payload

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> HabitRoutine:
        """Execute the from mapping operation.

        Returns:
            HabitRoutine value produced by from_mapping().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(payload, dict):
            raise HabitHealthContractError("routine payload must be an object")
        cadence_raw = payload.get("cadence") or {}
        cadence = cadence_raw if isinstance(cadence_raw, HabitCadence) else HabitCadence(**cadence_raw)
        return cls(
            routine_id=str(payload.get("routine_id") or f"routine-{uuid4().hex[:12]}"),
            user_id=str(payload.get("user_id", "")),
            name=str(payload.get("name", "")),
            cadence=cadence,
            scope=payload.get("scope", HabitHealthScope.PERSONAL_WELLNESS.value),
            tags=_string_tuple(payload.get("tags", ())),
            intervention_threshold_missed=int(payload.get("intervention_threshold_missed", 2)),
            source_context=str(payload.get("source_context", "")),
            consent_refs=_string_tuple(payload.get("consent_refs", ())),
            provenance_ref=str(payload.get("provenance_ref", "")),
            created_at_utc=str(payload.get("created_at_utc", "")),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HabitRoutine(routine_id={self.routine_id!r}, user_id={self.user_id!r}, name={self.name!r})"


@dataclass(frozen=True, slots=True)
class HabitCheckIn:
    """One user-provided routine check-in or rhythm observation."""

    check_in_id: str
    routine_id: str
    user_id: str
    checked_at_utc: str
    signal_kind: HabitHealthSignalKind | str = HabitHealthSignalKind.CHECK_IN
    scope: HabitHealthScope | str = HabitHealthScope.PERSONAL_WELLNESS
    energy: int | None = None
    focus: int | None = None
    mood_tags: tuple[str, ...] = ()
    agent_run_refs: tuple[str, ...] = ()
    source_context: str = ""
    consent_refs: tuple[str, ...] = ()
    provenance_ref: str = ""
    user_framing: str = ""
    generated_health_claims: tuple[str, ...] = ()
    non_medical_boundary: tuple[NonMedicalBoundary, ...] = (
        NonMedicalBoundary.DOES_NOT_DIAGNOSE,
        NonMedicalBoundary.DOES_NOT_PRESCRIBE,
        NonMedicalBoundary.USER_FRAMED_CONTEXT_ONLY,
    )
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.check_in_id, "check_in_id")
        _require_text(self.routine_id, "routine_id")
        _require_text(self.user_id, "user_id")
        _require_text(self.checked_at_utc, "checked_at_utc")
        object.__setattr__(self, "signal_kind", _coerce_signal_kind(self.signal_kind))
        object.__setattr__(self, "scope", _coerce_scope(self.scope))
        object.__setattr__(
            self,
            "non_medical_boundary",
            tuple(_coerce_non_medical_boundary(boundary) for boundary in self.non_medical_boundary),
        )
        for field_name in ("energy", "focus"):
            value = getattr(self, field_name)
            if value is not None and not 1 <= int(value) <= 5:
                raise HabitHealthContractError(f"{field_name} must be between 1 and 5")
        if self.generated_health_claims and not self.source_context.strip():
            raise HabitHealthContractError("generated health-like claims require user-provided source context")

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["signal_kind"] = self.signal_kind.value
        payload["scope"] = self.scope.value
        payload["non_medical_boundary"] = [boundary.value for boundary in self.non_medical_boundary]
        return payload

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> HabitCheckIn:
        """Execute the from mapping operation.

        Returns:
            HabitCheckIn value produced by from_mapping().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(payload, dict):
            raise HabitHealthContractError("check-in payload must be an object")
        return cls(
            check_in_id=str(payload.get("check_in_id") or f"checkin-{uuid4().hex[:12]}"),
            routine_id=str(payload.get("routine_id", "")),
            user_id=str(payload.get("user_id", "")),
            checked_at_utc=str(payload.get("checked_at_utc", "")),
            signal_kind=payload.get("signal_kind", HabitHealthSignalKind.CHECK_IN.value),
            scope=payload.get("scope", HabitHealthScope.PERSONAL_WELLNESS.value),
            energy=_optional_int(payload.get("energy")),
            focus=_optional_int(payload.get("focus")),
            mood_tags=_string_tuple(payload.get("mood_tags", ())),
            agent_run_refs=_string_tuple(payload.get("agent_run_refs", ())),
            source_context=str(payload.get("source_context", "")),
            consent_refs=_string_tuple(payload.get("consent_refs", ())),
            provenance_ref=str(payload.get("provenance_ref", "")),
            user_framing=str(payload.get("user_framing", "")),
            generated_health_claims=_string_tuple(payload.get("generated_health_claims", ())),
            non_medical_boundary=_non_medical_boundary_tuple(payload.get("non_medical_boundary", ())),
            metadata=dict(payload.get("metadata") or {}),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"HabitCheckIn(check_in_id={self.check_in_id!r}, routine_id={self.routine_id!r}, user_id={self.user_id!r})"
        )


@dataclass(frozen=True, slots=True)
class HabitRhythmSnapshot:
    """Computed view of routine cadence and rhythm state."""

    user_id: str
    generated_at_utc: str
    streak_count: int
    missed_count: int
    stale_routine_ids: tuple[str, ...]
    fatigue_risk: FatigueRisk
    quiet_window_hints: tuple[str, ...] = ()
    agent_run_refs: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["fatigue_risk"] = self.fatigue_risk.value
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HabitRhythmSnapshot(user_id={self.user_id!r}, generated_at_utc={self.generated_at_utc!r}, streak_count={self.streak_count!r})"


@dataclass(frozen=True, slots=True)
class HabitHealthSignal:
    """Consent-scoped signal that downstream systems may review."""

    signal_id: str
    user_id: str
    signal_kind: HabitHealthSignalKind
    scope: HabitHealthScope
    source_context: str
    consent_refs: tuple[str, ...]
    provenance_ref: str
    downstream_use: str
    allowed: bool
    reasons: tuple[str, ...]
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["signal_kind"] = self.signal_kind.value
        payload["scope"] = self.scope.value
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HabitHealthSignal(signal_id={self.signal_id!r}, user_id={self.user_id!r}, signal_kind={self.signal_kind!r})"


def _coerce_scope(value: HabitHealthScope | str) -> HabitHealthScope:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return value if isinstance(value, HabitHealthScope) else HabitHealthScope(raw_value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return HabitHealthScope.UNKNOWN


def _coerce_signal_kind(value: HabitHealthSignalKind | str) -> HabitHealthSignalKind:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return value if isinstance(value, HabitHealthSignalKind) else HabitHealthSignalKind(raw_value)
    except ValueError as exc:
        raise HabitHealthContractError(f"unknown signal kind: {value}") from exc


def _coerce_non_medical_boundary(value: NonMedicalBoundary | str) -> NonMedicalBoundary:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return value if isinstance(value, NonMedicalBoundary) else NonMedicalBoundary(raw_value)
    except ValueError as exc:
        raise HabitHealthContractError(f"unknown non-medical boundary: {value}") from exc


def _non_medical_boundary_tuple(value: object) -> tuple[NonMedicalBoundary, ...]:
    if value in (None, ""):
        return (
            NonMedicalBoundary.DOES_NOT_DIAGNOSE,
            NonMedicalBoundary.DOES_NOT_PRESCRIBE,
            NonMedicalBoundary.USER_FRAMED_CONTEXT_ONLY,
        )
    if isinstance(value, (str, NonMedicalBoundary)):
        return (_coerce_non_medical_boundary(value),)
    return tuple(_coerce_non_medical_boundary(item) for item in value)


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise HabitHealthContractError(f"{field_name} must be non-empty")


def _string_tuple(value: object) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    return tuple(str(item) for item in value if str(item).strip())


__all__ = [
    "FatigueRisk",
    "HabitCadence",
    "HabitCheckIn",
    "HabitHealthContractError",
    "HabitHealthScope",
    "HabitHealthSignal",
    "HabitHealthSignalKind",
    "HabitRhythmSnapshot",
    "HabitRoutine",
    "NonMedicalBoundary",
]
