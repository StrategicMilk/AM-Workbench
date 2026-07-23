"""Drift detection compatibility helpers."""

from __future__ import annotations

from typing import Any


def detect_drift(*, baseline: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """Detect drift from a baseline mapping.

    Args:
        baseline: Baseline state, if available.
        current: Current state.

    Returns:
        Drift detection result.
    """
    if baseline is None:
        return {
            "drift_detected": False,
            "reason": "no baseline",
            "changed_keys": [],
            "added_keys": sorted(current),
            "removed_keys": [],
        }
    changed_keys = sorted(key for key in baseline if key in current and baseline[key] != current[key])
    added_keys = sorted(key for key in current if key not in baseline)
    removed_keys = sorted(key for key in baseline if key not in current)
    return {
        "drift_detected": bool(changed_keys or added_keys or removed_keys),
        "changed_keys": changed_keys,
        "added_keys": added_keys,
        "removed_keys": removed_keys,
    }


__all__ = ["detect_drift"]
