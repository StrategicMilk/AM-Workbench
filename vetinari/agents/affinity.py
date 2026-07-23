"""Agent affinity registry helpers."""

from __future__ import annotations

_AFFINITY: dict[str, float] = {}


def get_affinity_map() -> dict[str, float]:
    """Return a copy of the live affinity registry.

    Returns:
        Affinity score mapping.
    """
    return dict(_AFFINITY)


def export_affinity() -> dict[str, float]:
    """Return a serializable snapshot of the live affinity registry."""
    return get_affinity_map()


def reset_affinity() -> None:
    """Clear all live affinity scores."""
    _AFFINITY.clear()


def update_affinity(agent_id: str, *, score: float) -> None:
    """Update an agent affinity score.

    Args:
        agent_id: Agent identifier.
        score: Affinity score.
    """
    _AFFINITY[agent_id] = score


class AffinityTestSuite:
    """Test utility that uses the live affinity registry."""

    def current(self) -> dict[str, float]:
        """Return the current live affinity map."""
        return get_affinity_map()


__all__ = ["AffinityTestSuite", "export_affinity", "get_affinity_map", "reset_affinity", "update_affinity"]
