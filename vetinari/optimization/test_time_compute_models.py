"""Data models for test-time compute scaling."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ComputeStepScore:
    """Quality score for a single reasoning step."""

    step_text: str
    score: float
    reasoning: str  # Human-readable explanation of why this score was assigned


StepScore = ComputeStepScore


@dataclass(frozen=True, slots=True)
class ComputeResult:
    """Result produced by TestTimeComputeScaler.scale()."""

    level_used: int
    result: str
    quality_estimate: float
    steps_evaluated: int
    computation_budget_used: float

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"ComputeResult(level_used={self.level_used!r},"
            f" quality_estimate={self.quality_estimate!r},"
            f" steps_evaluated={self.steps_evaluated!r})"
        )


@dataclass
class MCTSNode:
    """A single node in the MCTS search tree."""

    state: str  # Task description at this level
    children: list[MCTSNode] = field(default_factory=list)
    visits: int = 0
    total_value: float = 0.0
    parent: MCTSNode | None = None
    action: str = ""  # Decomposition step that led here

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"MCTSNode(visits={self.visits!r}, total_value={self.total_value!r}, action={self.action!r})"

    @property
    def value(self) -> float:
        """Mean value over all visits.

        Returns:
            Mean reward, or 0.0 if never visited.
        """
        if self.visits == 0:
            return 0.0
        return self.total_value / self.visits
