"""Consolidated math utilities for vector operations and descriptive statistics.

Replaces 6+ duplicate cosine similarity implementations across the codebase.
All vector math should import from here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def _validate_finite_values(values: Sequence[float], *, field_name: str) -> None:
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{field_name}[{index}] must be a finite number")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors.

    Handles zero-norm vectors and mismatched lengths gracefully.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Cosine similarity in [-1, 1], or 0.0 for degenerate inputs.
    """
    _validate_finite_values(a, field_name="a")
    _validate_finite_values(b, field_name="b")
    _validate_finite_values(a, field_name="a")
    _validate_finite_values(b, field_name="b")
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def cosine_distance(a: list[float], b: list[float]) -> float:
    """Compute cosine distance between two vectors.

    Args:
        a: First vector.
        b: Second vector.

    Returns:
        Cosine distance (1 - cosine_similarity), range [0, 2].
    """
    return 1.0 - cosine_similarity(a, b)


def stddev(
    values: list[float],
    mean: float | None = None,
    *,
    sample: bool = False,
) -> float:
    """Compute standard deviation (population or sample).

    Args:
        values: Numeric values.
        mean: Pre-computed mean to avoid recomputation. If None,
            the mean is calculated from *values*.
        sample: If True, use Bessel's correction (divide by n-1)
            for sample standard deviation. Default is population (n).

    Returns:
        Standard deviation, or 0.0 for empty/single-element lists.

    Raises:
        ValueError: If ``values`` or ``mean`` contains a non-finite number.
    """
    _validate_finite_values(values, field_name="values")
    if mean is not None and (isinstance(mean, bool) or not isinstance(mean, (int, float)) or not math.isfinite(mean)):
        raise ValueError("mean must be a finite number")
    if len(values) < 2:
        return 0.0
    mu = mean if mean is not None else sum(values) / len(values)
    denom = (len(values) - 1) if sample else len(values)
    variance = sum((x - mu) ** 2 for x in values) / denom
    return math.sqrt(variance)


def percentile(values: list[float], p: float) -> float:
    """Compute the p-th percentile of a list of values using linear interpolation.

    Args:
        values: Numeric values (need not be sorted).
        p: Percentile in [0, 100].

    Returns:
        The interpolated percentile value.

    Raises:
        ValueError: If values is empty or p is out of range.
    """
    _validate_finite_values(values, field_name="values")
    _validate_finite_values(values, field_name="values")
    if not values:
        msg = "Cannot compute percentile of empty list"
        raise ValueError(msg)
    if not 0.0 <= p <= 100.0:
        msg = f"Percentile must be in [0, 100], got {p}"
        raise ValueError(msg)
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    # Linear interpolation between nearest ranks
    rank = (p / 100.0) * (n - 1)
    lower = int(rank)
    upper = min(lower + 1, n - 1)
    frac = rank - lower
    return sorted_vals[lower] + frac * (sorted_vals[upper] - sorted_vals[lower])
