"""Lifecycle, pruning, and aggregate stats for the plan-tracking memory store."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import sys
from typing import TYPE_CHECKING, Any

from vetinari.database import get_connection
from vetinari.memory.plan_pruning import (
    PLAN_RETENTION_DAYS,
    PLAN_RETENTION_OWNER_REF,
    PlanPruneError,
)
from vetinari.memory.plan_pruning import (
    prune_old_plans as _prune_old_plans,
)

logger = logging.getLogger("vetinari.memory.plan_tracking")


class PlanTrackingLifecycleMixin:
    """Manage pruning, cleanup, and aggregate stats for plan tracking."""

    if TYPE_CHECKING:
        _json_data: Any
        use_json_fallback: Any

    def prune_old_plans(
        self,
        retention_days: int = PLAN_RETENTION_DAYS,
        *,
        dry_run: bool = False,
        owner_ref: str = PLAN_RETENTION_OWNER_REF,
    ) -> int:
        """Remove plan records older than *retention_days*.

        Returns:
            Number of pruned plans.

        Raises:
            PlanPruneError: If owner proof is missing or the prune receipt
                cannot be written before deletion.
        """
        return _prune_old_plans(self, retention_days, dry_run=dry_run, owner_ref=owner_ref)

    def close(self) -> None:
        """Close the underlying database connection and release resources.

        Delegates to the unified database module's thread-local connection
        management. After calling this, the next operation will re-open the
        connection via ``get_connection()``.
        """
        from vetinari.database import close_connection

        with contextlib.suppress(Exception):
            close_connection()

    def __del__(self) -> None:
        """Safety-net cleanup if close() was not called explicitly."""
        plan_tracking_module = sys.modules.get("vetinari.memory.plan_tracking")
        contextlib_module = getattr(plan_tracking_module, "contextlib", contextlib)
        suppress = getattr(contextlib_module, "suppress", None)
        if suppress is None:
            return
        with suppress(Exception):
            self.close()

    def get_memory_stats(self) -> dict[str, Any]:
        """Get aggregate statistics about stored plan data.

        Returns:
            Dict with plan/subtask/model counts and storage type.
        """
        if self.use_json_fallback:
            return {
                "total_plans": len(self._json_data["plans"]),
                "total_subtasks": len(self._json_data["subtasks"]),
                "total_model_records": len(self._json_data["model_performance"]),
                "storage_type": "json",
                "recovery_needed": bool(getattr(self, "_json_recovery_needed", False)),
            }

        try:
            conn = get_connection()
            plan_count = conn.execute("SELECT COUNT(*) FROM PlanHistory").fetchone()[0]
            subtask_count = conn.execute("SELECT COUNT(*) FROM SubtaskMemory").fetchone()[0]
            model_count = conn.execute("SELECT COUNT(*) FROM ModelPerformance").fetchone()[0]

            return {
                "total_plans": plan_count,
                "total_subtasks": subtask_count,
                "total_model_records": model_count,
                "storage_type": "sqlite",
            }

        except sqlite3.Error as e:
            logger.error("Failed to get memory stats: %s", e)
            return {}


__all__ = ["PLAN_RETENTION_DAYS", "PLAN_RETENTION_OWNER_REF", "PlanPruneError", "PlanTrackingLifecycleMixin"]
