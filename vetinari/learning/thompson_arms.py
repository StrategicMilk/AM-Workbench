"""Thompson Sampling data classes — ThompsonBetaArm and ThompsonTaskContext.

These are the pure-data types used by :class:`ThompsonSamplingSelector` and
its helper modules.  Extracted from ``model_selector.py`` to keep that file
under the 550-line ceiling.
"""

from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.ontology import SuccessSignal


@dataclass
class ThompsonBetaArm:
    """Beta distribution arm for a model+task_type pair.

    Maintains a Bayesian posterior over the probability of success for a
    given (model, task_type) combination.  Uses a Beta(2, 2) prior — slightly
    more skeptical than Beta(1, 1) to accelerate convergence on new arms.

    Args:
        model_id: Model identifier string.
        task_type: Task type string (e.g., "coding", "review").
        alpha: Success pseudo-count (quality-weighted).
        beta: Failure pseudo-count.
        total_pulls: Total number of observations.
        last_updated: ISO-8601 timestamp of the last update.
    """

    model_id: str
    task_type: str
    alpha: float = 2.0
    beta: float = 2.0
    total_pulls: int = 0
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"BetaArm(model_id={self.model_id!r}, task_type={self.task_type!r}, total_pulls={self.total_pulls!r})"

    @property
    def mean(self) -> float:
        """Expected value of the Beta distribution.

        Returns:
            E[Beta(alpha, beta)] = alpha / (alpha + beta).
        """
        return self.alpha / (self.alpha + self.beta)

    def sample(self) -> float:
        """Sample from the Beta distribution.

        Uses Python's built-in ``random.betavariate``.  Falls back to the
        distribution mean if the parameters are degenerate (e.g., both zero).

        Returns:
            A float in [0, 1] drawn from Beta(alpha, beta).
        """
        try:
            return random.betavariate(self.alpha, self.beta)
        except ValueError:
            logger.warning(
                "Beta sampling failed (alpha=%.4f, beta=%.4f) — returning mean %.4f instead of sampling",
                self.alpha,
                self.beta,
                self.mean,
            )
            return self.mean

    def update(self, quality_score: float, success: bool) -> None:
        """Update the arm based on an observed outcome.

        Prefer ``update_from_signal()`` when a typed SuccessSignal is available —
        it ensures the quality weight is clamped and originates from the gate pipeline.

        Args:
            quality_score: Quality score in [0, 1].  Weights the alpha/beta
                increment so higher-quality results have more influence.
            success: Whether the task was considered successful.
        """
        quality_score = max(0.0, min(1.0, quality_score))
        if success:
            self.alpha += quality_score
        else:
            self.beta += max(quality_score, 1.0 - quality_score)
        self.total_pulls += 1
        self.last_updated = datetime.now(timezone.utc).isoformat()

    def update_from_signal(self, signal: SuccessSignal) -> None:
        """Update the arm from a typed SuccessSignal produced by the quality gate pipeline.

        This is the canonical update path. ``SuccessSignal.from_quality_score()`` or
        ``gate_to_signal()`` in ``vetinari.ontology`` are the approved ways to create
        signals, ensuring the quality weight is clamped and semantically correct.

        Args:
            signal: Typed signal from the quality gate pipeline.
        """
        self.update(signal.quality_weight, signal.success)


@dataclass
class ThompsonTaskContext:
    """Features that inform model selection — the "context" in contextual bandit.

    Args:
        task_type: Type of task (code, research, architecture, review, etc.).
        estimated_complexity: Complexity rating 1-10 from intake.
        prompt_length: Token count in the task description.
        domain: Domain (python, javascript, infrastructure, etc.).
        requires_reasoning: Whether multi-step logic is needed.
        requires_creativity: Whether open-ended generation is needed.
        requires_precision: Whether exact syntax/structured output is needed.
        file_count: Number of files in scope.
    """

    task_type: str = "general"
    estimated_complexity: int = 5
    prompt_length: int = 0
    domain: str = "general"
    requires_reasoning: bool = False
    requires_creativity: bool = False
    requires_precision: bool = False
    file_count: int = 0

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"ThompsonTaskContext(task_type={self.task_type!r},"
            f" estimated_complexity={self.estimated_complexity!r},"
            f" domain={self.domain!r})"
        )

    def to_bucket(self) -> int:
        """Hash context features into a discrete bucket for arm lookup.

        Produces approximately 50 distinct buckets — enough to differentiate
        "coding+simple" from "coding+complex" without making arms too sparse.

        Returns:
            Bucket index (0-49).
        """
        if self.estimated_complexity <= 3:
            complexity_bin = "lo"
        elif self.estimated_complexity <= 7:
            complexity_bin = "mid"
        else:
            complexity_bin = "hi"
        key = f"{self.task_type}:{complexity_bin}:{self.domain}:{self.requires_reasoning}"
        # Use MD5 (truncated to int) for a stable, process-restart-safe bucket
        # hash.  Python's built-in hash() is randomised per interpreter run
        # (PYTHONHASHSEED), which would produce different bucket assignments
        # for the same context across restarts, breaking arm lookup.
        digest = int(hashlib.md5(key.encode("utf-8"), usedforsecurity=False).hexdigest(), 16)
        return digest % 50
