"""Deterministic shadow-test promotion checks for Workbench candidates."""

from __future__ import annotations

from dataclasses import dataclass

from vetinari.workbench.measurement.promotion_stats import PromotionStats, compute_promotion_stats


@dataclass(frozen=True, slots=True)
class ShadowPromotionDecision:
    """Promotion verdict for a shadow comparison."""

    promotable: bool
    stats: PromotionStats
    reason: str


def evaluate_shadow_promotion(
    baseline_values: tuple[float, ...],
    candidate_values: tuple[float, ...],
    *,
    minimum_sample_count: int = 3,
) -> ShadowPromotionDecision:
    """Return a fail-closed promotion verdict for shadow-test observations.

    Args:
        baseline_values: Baseline/control observations.
        candidate_values: Candidate observations.
        minimum_sample_count: Minimum paired samples before promotion can pass.

    Returns:
        Promotion decision backed by deterministic statistics.
    """
    stats = compute_promotion_stats(
        baseline_values,
        candidate_values,
        minimum_sample_count=minimum_sample_count,
    )
    if stats.promotable:
        return ShadowPromotionDecision(True, stats, "minimum_n_p_value_and_effect_size_passed")
    return ShadowPromotionDecision(False, stats, "insufficient_or_not_significant_shadow_evidence")


__all__ = ["ShadowPromotionDecision", "evaluate_shadow_promotion"]
