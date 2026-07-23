"""Specialist model cards for narrow agent-callable tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

BLOCKER_CALLER_NOT_ALLOWED = "caller_not_allowed"
BLOCKER_CONFIDENCE_BELOW_ABSTAIN = "confidence_below_abstain"
BLOCKER_TASK_SCOPE_MISMATCH = "task_scope_mismatch"


class SpecialistModelError(ValueError):
    """Raised when a specialist model contract is missing required proof."""


class SpecialistTask(str, Enum):
    """Specialist tasks that are narrow enough for agent tool calls."""

    FAILURE_CAUSE_CLASSIFICATION = "failure_cause_classification"
    PROMPT_INJECTION_DETECTION = "prompt_injection_detection"
    SOURCE_QUALITY_CLASSIFICATION = "source_quality_classification"
    RETRIEVAL_RERANKING = "retrieval_reranking"
    PLAN_QUALITY_DISCRIMINATION = "plan_quality_discrimination"
    ROUTE_CLASSIFICATION = "route_classification"


class SpecialistCallOutcome(str, Enum):
    """Agent behavior that becomes calibration data."""

    ACCEPTED = "accepted"
    OVERRIDDEN = "overridden"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class SpecialistModelCard:
    """Governed specialist model card for one typed tool binding."""

    card_id: str
    model_ref: str
    task: SpecialistTask
    task_contract: str
    input_schema_ref: str
    output_schema_ref: str
    confidence_calibration_ref: str
    abstain_threshold: float
    allowed_callers: tuple[str, ...]
    fallback_behavior: str
    eval_suite_ref: str
    known_failure_modes: tuple[str, ...]
    safety_ref: str
    budget_ref: str
    authority_ref: str
    provenance_ref: str
    persisted_state_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "card_id",
            "model_ref",
            "task_contract",
            "input_schema_ref",
            "output_schema_ref",
            "confidence_calibration_ref",
            "fallback_behavior",
            "eval_suite_ref",
            "safety_ref",
            "budget_ref",
            "authority_ref",
            "provenance_ref",
            "persisted_state_ref",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.task, SpecialistTask):
            raise SpecialistModelError("task must be a SpecialistTask")
        _require_string_tuple(self.allowed_callers, "allowed_callers")
        _require_string_tuple(self.known_failure_modes, "known_failure_modes")
        if self.abstain_threshold < 0 or self.abstain_threshold > 1:
            raise SpecialistModelError("abstain_threshold must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible card payload.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["task"] = self.task.value
        payload["allowed_callers"] = list(self.allowed_callers)
        payload["known_failure_modes"] = list(self.known_failure_modes)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SpecialistModelCard(card_id={self.card_id!r}, model_ref={self.model_ref!r}, task={self.task!r})"


@dataclass(frozen=True, slots=True)
class SpecialistCallDecision:
    """Fail-closed call decision for a specialist card."""

    card_id: str
    task: SpecialistTask
    caller: str
    approved: bool
    blockers: tuple[str, ...]
    fallback_behavior: str
    confidence_calibration_ref: str

    def __post_init__(self) -> None:
        _require_text(self.card_id, "card_id")
        _require_text(self.caller, "caller")
        _require_text(self.fallback_behavior, "fallback_behavior")
        _require_text(self.confidence_calibration_ref, "confidence_calibration_ref")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)
        if self.approved and self.blockers:
            raise SpecialistModelError("approved specialist call cannot include blockers")
        if not self.approved and not self.blockers:
            raise SpecialistModelError("blocked specialist call requires blockers")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SpecialistCallDecision(card_id={self.card_id!r}, task={self.task!r}, caller={self.caller!r})"


@dataclass(frozen=True, slots=True)
class SpecialistCalibrationEvent:
    """Agent acceptance, override, or ignore feedback for future calibration."""

    event_id: str
    card_id: str
    caller: str
    outcome: SpecialistCallOutcome
    confidence: float
    evidence_ref: str

    def __post_init__(self) -> None:
        for field_name in ("event_id", "card_id", "caller", "evidence_ref"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.outcome, SpecialistCallOutcome):
            raise SpecialistModelError("outcome must be a SpecialistCallOutcome")
        if self.confidence < 0 or self.confidence > 1:
            raise SpecialistModelError("confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible calibration event.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["outcome"] = self.outcome.value
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"SpecialistCalibrationEvent(event_id={self.event_id!r}, card_id={self.card_id!r}, caller={self.caller!r})"
        )


def decide_specialist_call(
    card: SpecialistModelCard,
    *,
    requested_task: SpecialistTask | str,
    caller: str,
    confidence: float,
) -> SpecialistCallDecision:
    """Approve a specialist invocation only inside its declared task and caller scope.

    Returns:
        SpecialistCallDecision value produced by decide_specialist_call().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(card, SpecialistModelCard):
        raise SpecialistModelError("card must be a SpecialistModelCard")
    _require_text(caller, "caller")
    task = SpecialistTask(requested_task)
    blockers: list[str] = []
    if task != card.task:
        blockers.append(BLOCKER_TASK_SCOPE_MISMATCH)
    if caller not in card.allowed_callers:
        blockers.append(BLOCKER_CALLER_NOT_ALLOWED)
    if confidence < card.abstain_threshold:
        blockers.append(BLOCKER_CONFIDENCE_BELOW_ABSTAIN)
    return SpecialistCallDecision(
        card_id=card.card_id,
        task=card.task,
        caller=caller,
        approved=not blockers,
        blockers=tuple(blockers),
        fallback_behavior=card.fallback_behavior,
        confidence_calibration_ref=card.confidence_calibration_ref,
    )


def record_specialist_feedback(
    card: SpecialistModelCard,
    *,
    caller: str,
    outcome: SpecialistCallOutcome | str,
    confidence: float,
    evidence_ref: str,
) -> SpecialistCalibrationEvent:
    """Record agent behavior as calibration data without mutating global state.

    Returns:
        Outcome produced by record_specialist_feedback().
    """
    _require_text(caller, "caller")
    selected_outcome = SpecialistCallOutcome(outcome)
    return SpecialistCalibrationEvent(
        event_id=f"{card.card_id}:{caller}:{selected_outcome.value}",
        card_id=card.card_id,
        caller=caller,
        outcome=selected_outcome,
        confidence=confidence,
        evidence_ref=evidence_ref,
    )


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SpecialistModelError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise SpecialistModelError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise SpecialistModelError(f"{field_name} must contain non-empty strings")


__all__ = [
    "BLOCKER_CALLER_NOT_ALLOWED",
    "BLOCKER_CONFIDENCE_BELOW_ABSTAIN",
    "BLOCKER_TASK_SCOPE_MISMATCH",
    "SpecialistCalibrationEvent",
    "SpecialistCallDecision",
    "SpecialistCallOutcome",
    "SpecialistModelCard",
    "SpecialistModelError",
    "SpecialistTask",
    "decide_specialist_call",
    "record_specialist_feedback",
]
