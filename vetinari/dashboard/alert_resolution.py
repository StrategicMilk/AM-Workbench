"""Metric resolution helpers for dashboard alert evaluation."""

from __future__ import annotations

from typing import Any


def _resolve_metric(snapshot_dict: dict[str, Any], key: str) -> float | None:
    """Walk a dot-notation key into the snapshot dictionary and return a float.

    Args:
        snapshot_dict: Serialized metrics snapshot.
        key: Dot-notation metric key.

    Returns:
        Float metric value, or None when the path is missing or non-numeric.
    """
    if key == "adapters.failure_rate_percent":
        adapters = snapshot_dict.get("adapters", {})
        if isinstance(adapters, dict):
            total = adapters.get("total_requests")
            failed = adapters.get("total_failed")
            if isinstance(total, (int, float)) and isinstance(failed, (int, float)) and total > 0:
                return (float(failed) / float(total)) * 100.0

    parts = key.split(".")
    node: Any = snapshot_dict
    for part in parts:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    if isinstance(node, (int, float)):
        return float(node)
    return None
