"""Governance policy path helpers."""

from __future__ import annotations

from pathlib import Path


def get_governor_policy_path() -> Path:
    """Return the governor policy path.

    Returns:
        Policy file path.
    """
    return Path("config") / "governance-policy.yaml"


__all__ = ["get_governor_policy_path"]
