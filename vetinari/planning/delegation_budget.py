"""Step 3 of Foreman's planning phase: enforce per-node subagent dispatch and redrive budgets."""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_MAX_DISPATCHES: int = 5
DEFAULT_MAX_REDRIVES: int = 3


class DelegationBudgetExceededError(ValueError):
    """Raised when a node's dispatch or redrive budget is exhausted."""


@dataclass
class DelegationBudget:
    """Per-node subagent dispatch budget with fail-closed rejection."""

    node_id: str
    max_dispatches: int = DEFAULT_MAX_DISPATCHES
    max_redrives: int = DEFAULT_MAX_REDRIVES
    _dispatch_count: int = field(default=0, init=False, repr=False)
    _redrive_count: int = field(default=0, init=False, repr=False)

    def __repr__(self) -> str:
        return (
            "DelegationBudget("
            f"node_id={self.node_id!r}, remaining_dispatches={self.remaining_dispatches}, "
            f"remaining_redrives={self.remaining_redrives})"
        )

    @property
    def remaining_dispatches(self) -> int:
        """Dispatches remaining before the budget is exhausted."""
        return max(self.max_dispatches - self._dispatch_count, 0)

    @property
    def remaining_redrives(self) -> int:
        """Redrives remaining before the budget is exhausted."""
        return max(self.max_redrives - self._redrive_count, 0)

    def dispatch(self) -> None:
        """Record one subagent dispatch, raising if the dispatch budget is exhausted.

        Raises:
            DelegationBudgetExceededError: If no dispatches remain.
        """
        if self._dispatch_count >= self.max_dispatches:
            raise DelegationBudgetExceededError(
                f"Delegation budget exceeded for node '{self.node_id}': "
                f"{self._dispatch_count}/{self.max_dispatches} dispatches used"
            )
        self._dispatch_count += 1

    def redrive(self) -> None:
        """Record one node redrive, raising if the redrive budget is exhausted.

        Raises:
            DelegationBudgetExceededError: If no redrives remain.
        """
        if self._redrive_count >= self.max_redrives:
            raise DelegationBudgetExceededError(
                f"Delegation budget exceeded for node '{self.node_id}': "
                f"{self._redrive_count}/{self.max_redrives} redrives used"
            )
        self._redrive_count += 1


__all__ = [
    "DEFAULT_MAX_DISPATCHES",
    "DEFAULT_MAX_REDRIVES",
    "DelegationBudget",
    "DelegationBudgetExceededError",
]
