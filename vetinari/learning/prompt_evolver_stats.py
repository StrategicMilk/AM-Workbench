"""Statistical helpers for prompt evolution promotion gates."""

from __future__ import annotations

import hashlib
import logging
import math
from collections.abc import Sequence

logger = logging.getLogger(__name__)


def significance_test(
    baseline_scores: Sequence[float],
    variant_scores: Sequence[float],
    seed: int = 42,
) -> tuple[bool, float]:
    """Return whether the variant is significant plus Cohen's d.

    Args:
        baseline_scores: Quality observations from the current baseline.
        variant_scores: Quality observations from the candidate variant.
        seed: Deterministic seed for the permutation-test fallback.

    Returns:
        A pair of ``(is_significant, cohens_d)``.
    """
    if len(baseline_scores) < 5 or len(variant_scores) < 5:
        return False, 0.0

    mean_b = sum(baseline_scores) / len(baseline_scores)
    mean_v = sum(variant_scores) / len(variant_scores)

    var_b = sum((x - mean_b) ** 2 for x in baseline_scores) / max(len(baseline_scores) - 1, 1)
    var_v = sum((x - mean_v) ** 2 for x in variant_scores) / max(len(variant_scores) - 1, 1)
    pooled_std = math.sqrt((var_b + var_v) / 2) or 1e-9
    cohens_d = abs(mean_v - mean_b) / pooled_std
    if var_b == 0.0 and var_v == 0.0:
        return mean_b != mean_v, cohens_d

    try:
        from scipy import stats

        _, p_value = stats.ttest_ind(baseline_scores, variant_scores)
        return p_value < 0.05, cohens_d
    except ImportError:
        logger.debug("scipy not available, falling back to permutation test for significance", exc_info=True)

    all_scores = list(baseline_scores) + list(variant_scores)
    observed_diff = mean_v - mean_b
    n_b, n_v = len(baseline_scores), len(variant_scores)
    count_extreme = 0
    for _ in range(1000):
        perm = _deterministic_permutation(all_scores, seed, _)
        perm_b = perm[:n_b]
        perm_v = perm[n_b:]
        perm_diff = (sum(perm_v) / n_v) - (sum(perm_b) / n_b)
        if abs(perm_diff) >= abs(observed_diff):
            count_extreme += 1
    p_perm = count_extreme / 1000
    return p_perm < 0.05, cohens_d


def _deterministic_permutation(scores: list[float], seed: int, iteration: int) -> list[float]:
    """Return a reproducible permutation without pseudo-random APIs."""
    indexed = [
        (
            hashlib.sha256(f"{seed}:{iteration}:{index}:{value!r}".encode()).digest(),
            value,
        )
        for index, value in enumerate(scores)
    ]
    indexed.sort(key=lambda item: item[0])
    return [value for _, value in indexed]


test_significance = significance_test
