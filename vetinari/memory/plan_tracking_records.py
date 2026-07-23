"""Plan and subtask record operations for the plan-tracking memory store."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.database import get_connection
from vetinari.types import StatusEnum

logger = logging.getLogger("vetinari.memory.plan_tracking")

_MAX_SUBTASK_QUERY_RESULTS = 1_000


class PlanTrackingRecordsMixin:
    """Persist and query plan/subtask execution records."""

    if TYPE_CHECKING:
        _json_data: Any
        _query_plan_json: Any
        _query_subtasks_json: Any
        _save_json: Any
        _write_plan_json: Any
        _write_subtask_json: Any
        use_json_fallback: Any

    def write_plan_history(self, plan_data: dict[str, Any]) -> bool:
        """Write plan history.

        Returns:
            True if successful, False otherwise.
        """
        if self.use_json_fallback:
            return self._write_plan_json(plan_data)

        try:
            conn = get_connection()
            conn.execute(
                """
                INSERT OR REPLACE INTO PlanHistory
                (plan_id, plan_version, goal, updated_at, status, plan_json, plan_explanation_json,
                 chosen_plan_id, plan_justification, risk_score, dry_run, auto_approved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    plan_data.get("plan_id"),
                    plan_data.get("plan_version", 1),
                    plan_data.get("goal"),
                    datetime.now(timezone.utc).isoformat(),
                    plan_data.get("status", "draft"),
                    json.dumps(plan_data.get("plan_json", {})),
                    plan_data.get("plan_explanation_json", ""),
                    plan_data.get("chosen_plan_id"),
                    plan_data.get("plan_justification"),
                    plan_data.get("risk_score", 0.0),
                    plan_data.get("dry_run", False),
                    plan_data.get("auto_approved", False),
                ),
            )
            conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("Failed to write plan: %s", e)
            return False

    def write_subtask_memory(self, subtask_data: dict[str, Any]) -> bool:
        """Write subtask memory.

        Returns:
            True if successful, False otherwise.
        """
        if self.use_json_fallback:
            return self._write_subtask_json(subtask_data)

        try:
            conn = get_connection()
            conn.execute(
                """
                INSERT OR REPLACE INTO SubtaskMemory
                (subtask_id, plan_id, parent_subtask_id, description, depth,
                 status, assigned_model_id, outcome, duration_seconds,
                 cost_estimate, rationale, subtask_explanation_json, domain,
                 quality_score, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    subtask_data.get("subtask_id"),
                    subtask_data.get("plan_id"),
                    subtask_data.get("parent_subtask_id"),
                    subtask_data.get("description"),
                    subtask_data.get("depth", 0),
                    subtask_data.get("status", StatusEnum.PENDING.value),
                    subtask_data.get("assigned_model_id"),
                    subtask_data.get("outcome"),
                    subtask_data.get("duration_seconds"),
                    subtask_data.get("cost_estimate"),
                    subtask_data.get("rationale"),
                    subtask_data.get("subtask_explanation_json", ""),
                    subtask_data.get("domain"),
                    subtask_data.get("quality_score"),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("Failed to write subtask: %s", e)
            return False

    def query_plan_history(
        self,
        plan_id: str | None = None,
        goal_contains: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Query plan history.

        Args:
            plan_id: The plan id.
            goal_contains: The goal contains.
            limit: The limit.

        Returns:
            List of matching plan history records.
        """
        if self.use_json_fallback:
            return self._query_plan_json(plan_id, goal_contains, limit)

        try:
            conn = get_connection()

            if plan_id:
                rows = conn.execute(
                    """
                    SELECT * FROM PlanHistory
                    WHERE plan_id = ?
                    ORDER BY created_at DESC
                """,
                    (plan_id,),
                ).fetchall()
            elif goal_contains:
                rows = conn.execute(
                    """
                    SELECT * FROM PlanHistory
                    WHERE goal LIKE ?
                    ORDER BY created_at DESC
                    LIMIT ?
                """,
                    (f"%{goal_contains}%", limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM PlanHistory
                    ORDER BY created_at DESC
                    LIMIT ?
                """,
                    (limit,),
                ).fetchall()

            return [dict(row) for row in rows]

        except sqlite3.Error as e:
            logger.error("Failed to query plans: %s", e)
            return []

    def query_subtasks(
        self,
        plan_id: str | None = None,
        subtask_id: str | None = None,
        depth: int | None = None,
    ) -> list[dict[str, Any]]:
        """Query subtasks.

        Args:
            plan_id: The plan id.
            subtask_id: The subtask id.
            depth: The depth.

        Returns:
            List of matching subtask records.
        """
        if self.use_json_fallback:
            return self._query_subtasks_json(plan_id, subtask_id, depth)[:_MAX_SUBTASK_QUERY_RESULTS]

        try:
            conn = get_connection()

            if subtask_id:
                rows = conn.execute(
                    """
                    SELECT * FROM SubtaskMemory
                    WHERE subtask_id = ?
                """,
                    (subtask_id,),
                ).fetchall()
            elif plan_id:
                if depth is not None:
                    rows = conn.execute(
                        """
                        SELECT * FROM SubtaskMemory
                        WHERE plan_id = ? AND depth = ?
                        ORDER BY depth, subtask_id
                        LIMIT ?
                    """,
                        (plan_id, depth, _MAX_SUBTASK_QUERY_RESULTS),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM SubtaskMemory
                        WHERE plan_id = ?
                        ORDER BY depth, subtask_id
                        LIMIT ?
                    """,
                        (plan_id, _MAX_SUBTASK_QUERY_RESULTS),
                    ).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM SubtaskMemory
                    ORDER BY created_at DESC
                    LIMIT 100
                """).fetchall()

            return [dict(row) for row in rows]

        except sqlite3.Error as e:
            logger.error("Failed to query subtasks: %s", e)
            return []

    def update_subtask_quality(self, subtask_id: str, quality_score: float = 0.0, succeeded: bool = True) -> bool:
        """Annotate a SubtaskMemory record with a quality score and outcome.

        Args:
            subtask_id: The subtask id.
            quality_score: The quality score.
            succeeded: Whether the subtask succeeded.

        Returns:
            True if successful, False otherwise.
        """
        if self.use_json_fallback:
            subtask = self._json_data.get("subtasks", {}).get(subtask_id, {})
            if subtask:
                subtask["quality_score"] = quality_score
                subtask["outcome"] = StatusEnum.COMPLETED.value if succeeded else StatusEnum.FAILED.value
                subtask["updated_at"] = datetime.now(timezone.utc).isoformat()
                self._save_json()
                return True
            logger.warning(
                "update_subtask_quality: subtask %s not found in JSON store - quality update skipped",
                subtask_id,
            )
            return False
        try:
            conn = get_connection()
            cursor = conn.execute(
                """UPDATE SubtaskMemory
                   SET outcome = ?, quality_score = ?, updated_at = ?
                   WHERE subtask_id = ?""",
                (
                    StatusEnum.COMPLETED.value if succeeded else StatusEnum.FAILED.value,
                    quality_score,
                    datetime.now(timezone.utc).isoformat(),
                    subtask_id,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                logger.warning(
                    "update_subtask_quality: subtask %s not found in SubtaskMemory - quality update skipped",
                    subtask_id,
                )
                return False
            return True
        except sqlite3.Error as e:
            logger.warning("update_subtask_quality failed: %s", e)
            return False
