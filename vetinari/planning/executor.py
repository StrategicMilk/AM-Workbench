"""Plan execution compatibility helpers."""

from __future__ import annotations

from typing import Any


def resume_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Resume schedulable plan tasks.

    Args:
        plan: Plan mapping containing task dictionaries.

    Returns:
        Scheduling result with unblocked task ids.
    """
    tasks = plan.get("tasks", [])
    blocked = {task.get("id") for task in tasks if isinstance(task, dict) and task.get("status") == "blocked"}
    scheduled = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        dependencies = set(task.get("dependencies", []))
        if task.get("status") == "pending" and not dependencies.intersection(blocked):
            scheduled.append(task.get("id"))
    return {"scheduled": scheduled}


__all__ = ["resume_plan"]
