"""Shared data models for the autonomy governor."""

from __future__ import annotations

from dataclasses import dataclass

from vetinari.types import AutonomyLevel, PermissionDecision

_PROMOTION_SUCCESS_RATE = 0.95
_PROMOTION_MIN_ACTIONS = 50


@dataclass(frozen=True, slots=True)
class ActionPolicy:
    """Policy configuration for a single action type.

    Args:
        level: The autonomy level controlling human involvement.
        max_change_pct: Maximum allowed change percentage for bounded actions.
        rollback_on_regression: Whether to auto-rollback if quality regresses.
    """

    level: AutonomyLevel
    max_change_pct: float = 100.0
    rollback_on_regression: bool = False


@dataclass(frozen=True, slots=True)
class TrustRecord:
    """Per-action-type trust tracking for the progressive trust engine.

    Tracks success and failure history to support promotion suggestions and
    automatic demotions.
    """

    total_actions: int = 0
    successful_actions: int = 0
    consecutive_failures: int = 0
    was_demoted: bool = False

    def __repr__(self) -> str:
        return (
            f"TrustRecord(total={self.total_actions}, "
            f"successful={self.successful_actions}, "
            f"consecutive_failures={self.consecutive_failures})"
        )

    @property
    def success_rate(self) -> float:
        """Rolling success rate as a fraction from 0.0 to 1.0."""
        if self.total_actions == 0:
            return 0.0
        return self.successful_actions / self.total_actions

    @property
    def eligible_for_promotion(self) -> bool:
        """Whether this action type meets promotion criteria."""
        return self.total_actions >= _PROMOTION_MIN_ACTIONS and self.success_rate >= _PROMOTION_SUCCESS_RATE


@dataclass(frozen=True, slots=True)
class PendingPromotion:
    """A pending auto-promotion awaiting veto-window expiry.

    Attributes:
        action_type: The action type proposed for promotion.
        current_level: The current autonomy level.
        new_level: The proposed promoted level.
        proposed_at: ISO-8601 timestamp when the proposal was created.
        veto_deadline: ISO-8601 timestamp after which the promotion auto-applies.
    """

    action_type: str
    current_level: AutonomyLevel
    new_level: AutonomyLevel
    proposed_at: str
    veto_deadline: str

    def __repr__(self) -> str:
        return "PendingPromotion(...)"


@dataclass(frozen=True, slots=True)
class PermissionResult:
    """Rich result from a permission request including action tracking data.

    Attributes:
        decision: The permission decision.
        action_type: The action type that was evaluated.
        action_id: Unique approval queue ID, only set when deferred.
        level: The autonomy level that produced this decision.
        policy: The full action policy that was applied.
    """

    decision: PermissionDecision
    action_type: str
    action_id: str | None
    level: AutonomyLevel
    policy: ActionPolicy

    def __repr__(self) -> str:
        return "PermissionResult(...)"


@dataclass(frozen=True, slots=True)
class PromotionSuggestion:
    """A suggestion to promote an action type to a higher autonomy level."""

    action_type: str
    current_level: AutonomyLevel
    suggested_level: AutonomyLevel
    success_rate: float
    total_actions: int

    def __repr__(self) -> str:
        return "PromotionSuggestion(...)"
