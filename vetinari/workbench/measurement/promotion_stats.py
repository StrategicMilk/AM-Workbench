"""Promotion statistics for measured Workbench deltas."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean, pstdev

DEFAULT_ALPHA = 0.05
DEFAULT_MINIMUM_EFFECT_SIZE = 0.01
DEFAULT_MINIMUM_PROMOTION_SAMPLES = 3


class PromotionStatsError(ValueError):
    """Raised when promotion statistics cannot be computed."""


@dataclass(frozen=True, slots=True)
class PromotionStats:
    """One-sided promotion statistics for a candidate-vs-baseline comparison."""

    sample_count: int
    minimum_sample_count: int
    mean_delta: float
    effect_size: float
    p_value: float
    alpha: float = DEFAULT_ALPHA
    minimum_effect_size: float = DEFAULT_MINIMUM_EFFECT_SIZE

    def __repr__(self) -> str:
        return (
            "PromotionStats("
            f"sample_count={self.sample_count}, mean_delta={self.mean_delta!r}, "
            f"effect_size={self.effect_size!r}, p_value={self.p_value!r}, "
            f"promotable={self.promotable})"
        )

    @property
    def promotable(self) -> bool:
        """Return true only when sample size, p-value, and effect size all pass."""
        return (
            self.sample_count >= self.minimum_sample_count
            and self.mean_delta > 0
            and self.effect_size >= self.minimum_effect_size
            and self.p_value <= self.alpha
        )


def compute_promotion_stats(
    baseline_values: tuple[float, ...],
    method_values: tuple[float, ...],
    *,
    minimum_sample_count: int = DEFAULT_MINIMUM_PROMOTION_SAMPLES,
    alpha: float = DEFAULT_ALPHA,
    minimum_effect_size: float = DEFAULT_MINIMUM_EFFECT_SIZE,
) -> PromotionStats:
    """Compute deterministic one-sided promotion evidence.

    The implementation uses a normal approximation over paired-prefix deltas so
    it has no optional scipy dependency. Under-sampled inputs fail closed with
    p=1.0 and ``promotable == False``.

    Args:
        baseline_values: Baseline/control metric samples.
        method_values: Candidate method metric samples.
        minimum_sample_count: Minimum paired samples before promotion can pass.
        alpha: Maximum one-sided p-value for promotion.
        minimum_effect_size: Minimum normalized effect size for promotion.

    Returns:
        Deterministic promotion statistics for the paired samples.

    Raises:
        PromotionStatsError: if either sample set is empty.
    """
    if minimum_sample_count < 1:
        raise ValueError("minimum_sample_count must be at least 1")
    sample_count = min(len(baseline_values), len(method_values))
    if sample_count == 0:
        raise PromotionStatsError("baseline and method samples are required")
    paired_deltas = tuple(float(method_values[index]) - float(baseline_values[index]) for index in range(sample_count))
    mean_delta = float(fmean(paired_deltas))
    if sample_count < minimum_sample_count or sample_count < 2:
        return PromotionStats(
            sample_count=sample_count,
            minimum_sample_count=minimum_sample_count,
            mean_delta=mean_delta,
            effect_size=max(0.0, mean_delta),
            p_value=1.0,
            alpha=alpha,
            minimum_effect_size=minimum_effect_size,
        )
    spread = pstdev(paired_deltas)
    if spread == 0.0:
        p_value = 0.0 if mean_delta > 0 else 1.0
        effect_size = abs(mean_delta)
    else:
        standard_error = spread / math.sqrt(sample_count)
        z_score = mean_delta / standard_error
        p_value = 0.5 * math.erfc(z_score / math.sqrt(2.0))
        effect_size = abs(mean_delta) / spread
    return PromotionStats(
        sample_count=sample_count,
        minimum_sample_count=minimum_sample_count,
        mean_delta=mean_delta,
        effect_size=effect_size,
        p_value=max(0.0, min(1.0, p_value)),
        alpha=alpha,
        minimum_effect_size=minimum_effect_size,
    )


__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_MINIMUM_EFFECT_SIZE",
    "DEFAULT_MINIMUM_PROMOTION_SAMPLES",
    "PromotionStats",
    "PromotionStatsError",
    "compute_promotion_stats",
]
