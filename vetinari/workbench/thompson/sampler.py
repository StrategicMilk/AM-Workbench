"""Deterministic Thompson-style sampler used by completeness checks."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ThompsonArm:
    """Beta-distribution arm scored with deterministic posterior jitter."""

    arm_id: str
    alpha: float = 1.0
    beta: float = 1.0

    def sample(self, *, seed: int) -> float:
        """Return a deterministic Thompson-style score for this arm and seed.

        Returns:
            The posterior mean with stable seed jitter for tie-breaking.

        Raises:
            ValueError: if alpha or beta are not positive.
        """
        if self.alpha <= 0 or self.beta <= 0:
            raise ValueError("alpha and beta must be positive")
        posterior_mean = self.alpha / (self.alpha + self.beta)
        digest = hashlib.sha256(f"{self.arm_id}:{seed}".encode()).digest()
        jitter = int.from_bytes(digest[:8], "big") / float(2**64 - 1)
        return (posterior_mean * 0.9) + (jitter * 0.1)


def select_arm(arms: tuple[ThompsonArm, ...], *, seed: int = 0) -> ThompsonArm:
    """Select the arm with the highest deterministic Thompson sample.

    Returns:
        The highest-scoring arm, using arm id as a deterministic tiebreaker.

    Raises:
        ValueError: if no arms are supplied.
    """
    if not arms:
        raise ValueError("at least one arm is required")
    return max(arms, key=lambda arm: (arm.sample(seed=seed), arm.arm_id))


__all__ = ["ThompsonArm", "select_arm"]
