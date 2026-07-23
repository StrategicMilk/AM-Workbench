"""Shared integrity checks for workbench evaluation scores."""

from __future__ import annotations

from math import isfinite
from numbers import Real


def validate_normalized_eval_score(value: float, *, field_name: str) -> float:
    """Return a finite normalized eval score or raise ValueError.

    Returns:
        Validated score as a float.

    Raises:
        ValueError: If the value is non-numeric, non-finite, or outside [0, 1].
    """
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field_name} must be numeric")
    normalized = float(value)
    if not isfinite(normalized):
        raise ValueError(f"{field_name} must be finite")
    if not 0.0 <= normalized <= 1.0:
        raise ValueError(f"{field_name} must be between 0 and 1")
    return normalized


__all__ = ["validate_normalized_eval_score"]
