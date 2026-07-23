"""Planning assignment pass helpers."""

from __future__ import annotations

import copy
from typing import Any


class AssignmentPass:
    """Assign model ids to task dictionaries."""

    def run(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Run assignment over task dictionaries.

        Args:
            tasks: Task dictionaries.

        Returns:
            Assigned task dictionaries.
        """
        assigned_tasks = []
        for task in tasks:
            cloned = copy.deepcopy(task)
            cloned["assigned"] = True
            cloned.setdefault("model_id", "local-default")
            assigned_tasks.append(cloned)
        return assigned_tasks


__all__ = ["AssignmentPass"]
