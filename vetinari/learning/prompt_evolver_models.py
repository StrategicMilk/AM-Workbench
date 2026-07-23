"""Data models for prompt evolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.types import PromptVersionStatus


@dataclass
class PromptVariant:
    """A candidate prompt variant with performance tracking."""

    variant_id: str
    agent_type: str
    prompt_text: str
    is_baseline: bool = False
    trials: int = 0
    total_quality: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    promoted_at: str | None = None
    status: str = PromptVersionStatus.TESTING.value
    metadata: dict[str, Any] = field(default_factory=dict)
    score_history: list[float] = field(default_factory=list)

    def __repr__(self) -> str:
        """Return a compact representation identifying the prompt variant."""
        return f"PromptVariant(variant_id={self.variant_id!r}, agent_type={self.agent_type!r}, status={self.status!r})"

    @property
    def avg_quality(self) -> float:
        """The mean quality score across all recorded trials for this variant."""
        return self.total_quality / max(self.trials, 1)

    def record(self, quality: float) -> None:
        """Record one quality observation for this variant.

        Args:
            quality: Quality score to add to this variant's running average.
        """
        self.trials += 1
        self.total_quality += quality
        self.score_history.append(float(quality))
        if len(self.score_history) > 500:
            self.score_history = self.score_history[-500:]


__all__ = ["PromptVariant"]
