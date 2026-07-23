"""Plan validation compatibility helpers."""

from __future__ import annotations

import logging
from typing import Any

from vetinari.planning.plan_validator import validate_plan as _validate_plan

logger = logging.getLogger(__name__)


def validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate a plan mapping.

    Args:
        plan: Plan mapping with a ``tasks`` collection.

    Returns:
        The validated plan mapping.

    Raises:
        ValueError: If duplicate task ids are present.
    """
    tasks = plan.get("tasks", [])
    ids = [task.get("id") for task in tasks if isinstance(task, dict)]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate task ids are not allowed")
    try:
        _validate_plan(plan)
    except TypeError as exc:
        raise RuntimeError("legacy plan validator rejected mapping input") from exc
    return plan


__all__ = ["validate_plan"]
