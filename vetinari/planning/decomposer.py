"""Planning decomposition compatibility helpers."""

from __future__ import annotations

from typing import Any

from vetinari.errors import FailClosedError


def decompose(goal: str, *, depth: int = 0, **_: Any) -> list[dict[str, Any]]:
    """Decompose a goal into task dictionaries.

    Args:
        goal: Goal text.
        depth: Recursive decomposition depth.
        **_: Reserved keyword arguments.

    Returns:
        Decomposed task list.

    Raises:
        FailClosedError: If the goal is blank or the requested decomposition
            depth is negative.
    """
    if not goal or not goal.strip():
        raise FailClosedError(
            "planning.decomposer.goal",
            "goal text is required before decomposition",
            recovery="provide a non-empty planning goal",
        )
    if depth < 0:
        raise FailClosedError(
            "planning.decomposer.depth",
            "decomposition depth cannot be negative",
            recovery="restart decomposition at depth 0 or higher",
        )
    return [{"id": f"task-{depth}", "description": goal, "depth": depth}]


__all__ = ["decompose"]
