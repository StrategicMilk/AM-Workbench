"""Canonical CPU-tier lifecycle states."""

from __future__ import annotations

CPU_TIER_STATE_UNLOADED = "unloaded"
CPU_TIER_STATE_LOADING = "loading"
CPU_TIER_STATE_SMOKE_TEST = "smoke_test"
CPU_TIER_STATE_READY = "ready"
CPU_TIER_STATE_DEGRADED = "degraded"
CPU_TIER_STATE_RELEASING = "releasing"
CPU_TIER_STATE_RELEASED = "released"

CPU_TIER_STATES = frozenset({
    CPU_TIER_STATE_UNLOADED,
    CPU_TIER_STATE_LOADING,
    CPU_TIER_STATE_SMOKE_TEST,
    CPU_TIER_STATE_READY,
    CPU_TIER_STATE_DEGRADED,
    CPU_TIER_STATE_RELEASING,
    CPU_TIER_STATE_RELEASED,
})

CPU_TIER_RELEASEABLE_TERMINAL_STATES = frozenset({
    CPU_TIER_STATE_RELEASED,
    CPU_TIER_STATE_UNLOADED,
    CPU_TIER_STATE_DEGRADED,
})


def validate_cpu_tier_state(state: str) -> str:
    """Return a known CPU-tier state or fail closed on corrupt state.

    Returns:
        The validated CPU-tier state.

    Raises:
        ValueError: If ``state`` is not in the canonical state set.
    """
    if state not in CPU_TIER_STATES:
        raise ValueError(f"unknown CPU tier state: {state}")
    return state


__all__ = [
    "CPU_TIER_RELEASEABLE_TERMINAL_STATES",
    "CPU_TIER_STATES",
    "CPU_TIER_STATE_DEGRADED",
    "CPU_TIER_STATE_LOADING",
    "CPU_TIER_STATE_READY",
    "CPU_TIER_STATE_RELEASED",
    "CPU_TIER_STATE_RELEASING",
    "CPU_TIER_STATE_SMOKE_TEST",
    "CPU_TIER_STATE_UNLOADED",
    "validate_cpu_tier_state",
]
