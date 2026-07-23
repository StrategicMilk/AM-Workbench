"""Step 1 of Foreman's planning phase: capture durable scope, acceptance criteria, and anti-goals before graphing work."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.intake.request_frame import RequestFrame
    from vetinari.planning.plan_reviewer import PlanReviewer
    from vetinari.planning.review_outcome import PlanReviewOutcome


@dataclass(frozen=True, slots=True)
class SpecFrame:
    """Immutable requirements frame passed into plan review and graphing."""

    goal: str
    in_scope: tuple[str, ...] = field(default_factory=tuple)
    out_of_scope: tuple[str, ...] = field(default_factory=tuple)
    acceptance_criteria: tuple[str, ...] = field(default_factory=tuple)
    anti_goals: tuple[str, ...] = field(default_factory=tuple)
    frame_id: str = ""
    worker_escalation_reason: str = ""

    def __post_init__(self) -> None:
        for field_name in ("in_scope", "out_of_scope", "acceptance_criteria", "anti_goals"):
            value = getattr(self, field_name)
            if not isinstance(value, tuple):
                object.__setattr__(self, field_name, tuple(_coerce_strings(value)))

    @classmethod
    def from_request_frame(cls, request_frame: RequestFrame) -> SpecFrame:
        """Create a minimal SpecFrame from an intake RequestFrame."""
        return cls(goal=request_frame.goal)

    def submit_for_review(self, reviewer: PlanReviewer) -> PlanReviewOutcome:
        """Submit this frame to a PlanReviewer using the current reviewer signature."""
        return reviewer.review(asdict(self))

    def __repr__(self) -> str:
        return (
            "SpecFrame("
            f"goal={self.goal!r}, "
            f"in_scope={len(self.in_scope)!r}, "
            f"out_of_scope={len(self.out_of_scope)!r}, "
            f"acceptance_criteria={len(self.acceptance_criteria)!r}, "
            f"anti_goals={len(self.anti_goals)!r})"
        )


def _coerce_strings(value: Iterable[str] | Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)


__all__ = ["SpecFrame"]
