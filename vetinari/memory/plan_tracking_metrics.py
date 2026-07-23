"""Model performance operations for the plan-tracking memory store."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.database import get_connection

logger = logging.getLogger("vetinari.memory.plan_tracking")


class PlanTrackingMetricsMixin:
    """Persist and retrieve model performance metrics."""

    if TYPE_CHECKING:
        _json_data: Any
        _update_model_perf_json: Any
        use_json_fallback: Any

    def get_model_performance(self, model_id: str, task_type: str) -> dict[str, Any] | None:
        """Retrieve model performance record for a given model and task type.

        Args:
            model_id: The model id.
            task_type: The task type.

        Returns:
            Performance record dict, or None if not found.
        """
        if self.use_json_fallback:
            key = f"{model_id}:{task_type}"
            return self._json_data.get("model_performance", {}).get(key)
        try:
            conn = get_connection()
            row = conn.execute(
                "SELECT * FROM ModelPerformance WHERE model_id = ? AND task_type = ?",
                (model_id, task_type),
            ).fetchone()
            return dict(row) if row else None
        except sqlite3.Error as e:
            logger.warning("get_model_performance failed: %s", e)
            return None

    def update_model_performance(
        self,
        model_id: str,
        task_type: str,
        success_or_dict: bool | dict[str, Any] | None = None,
        latency: float = 0.0,
    ) -> bool:
        """Update model performance metrics.

        Accepts two call signatures:
          - Alternative: update_model_performance(model_id, task_type, success: bool, latency: float)
          - New:         update_model_performance(model_id, task_type, data: dict)

        Args:
            model_id: The model id.
            task_type: The task type.
            success_or_dict: Boolean success flag or dict with performance data.
            latency: The latency in seconds.

        Returns:
            True if successful, False otherwise.
        """
        parsed_update = self._normalize_model_performance_update(model_id, task_type, success_or_dict, latency)
        if parsed_update is None:
            return False
        success, latency = parsed_update
        if self.use_json_fallback:
            return self._update_model_perf_json(model_id, task_type, success, latency)
        return self._update_model_performance_sqlite(model_id, task_type, success, latency)

    @staticmethod
    def _normalize_model_performance_update(
        model_id: str,
        task_type: str,
        success_or_dict: bool | dict[str, Any] | None,
        latency: float,
    ) -> tuple[bool, float] | None:
        if isinstance(success_or_dict, dict):
            data = success_or_dict
            if data.get("success_rate") is None:
                logger.warning(
                    "update_model_performance refused missing success_rate for model=%s task_type=%s",
                    model_id,
                    task_type,
                )
                return None
            success = float(data["success_rate"]) >= 0.5
            latency = float(data.get("avg_latency", latency))
        elif success_or_dict is None:
            logger.warning(
                "update_model_performance refused missing success flag for model=%s task_type=%s",
                model_id,
                task_type,
            )
            return None
        else:
            success = bool(success_or_dict)
        return success, latency

    def _update_model_performance_sqlite(
        self,
        model_id: str,
        task_type: str,
        success: bool,
        latency: float,
    ) -> bool:
        try:
            conn = get_connection()
            row = conn.execute(
                """
                SELECT * FROM ModelPerformance
                WHERE model_id = ? AND task_type = ?
                """,
                (model_id, task_type),
            ).fetchone()
            self._upsert_model_performance(conn, row, model_id, task_type, success, latency)
            conn.commit()
            return True
        except sqlite3.Error as e:
            logger.error("Failed to update model performance: %s", e)
            return False

    def _upsert_model_performance(
        self,
        conn: sqlite3.Connection,
        row: Any,
        model_id: str,
        task_type: str,
        success: bool,
        latency: float,
    ) -> None:
        if row:
            self._update_existing_model_performance(conn, row, model_id, task_type, success, latency)
            return
        conn.execute(
            """
            INSERT INTO ModelPerformance
            (model_id, task_type, success_rate, avg_latency, total_uses, last_used_at)
            VALUES (?, ?, ?, ?, 1, ?)
        """,
            (model_id, task_type, 1.0 if success else 0.0, latency, datetime.now(timezone.utc).isoformat()),
        )

    @staticmethod
    def _update_existing_model_performance(
        conn: sqlite3.Connection,
        row: Any,
        model_id: str,
        task_type: str,
        success: bool,
        latency: float,
    ) -> None:
        new_success_rate = (row["success_rate"] * row["total_uses"] + (1 if success else 0)) / (row["total_uses"] + 1)
        new_latency = (row["avg_latency"] * row["total_uses"] + latency) / (row["total_uses"] + 1)
        conn.execute(
            """
            UPDATE ModelPerformance
            SET success_rate = ?, avg_latency = ?,
                total_uses = ?, last_used_at = ?
            WHERE model_id = ? AND task_type = ?
        """,
            (
                new_success_rate,
                new_latency,
                row["total_uses"] + 1,
                datetime.now(timezone.utc).isoformat(),
                model_id,
                task_type,
            ),
        )
