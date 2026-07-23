"""Persistence and recovery helpers for :mod:`vetinari.a2a.executor`."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.a2a.executor_models import (
    STATUS_ACKNOWLEDGED,
    STATUS_FAILED,
    STATUS_ORPHANED,
    STATUS_PENDING,
    STATUS_RUNNING,
    A2AResult,
    A2ATask,
)
from vetinari.privacy.envelope import extract_privacy_envelope, require_privacy_envelope, wrap_for_persistence

logger = logging.getLogger(__name__)


def _wrap_a2a_payload(payload: dict[str, Any], *, task_id: str, source: str) -> dict[str, Any]:
    return wrap_for_persistence(
        payload,
        privacy_class="subject_data",
        subject_id=task_id,
        retention_days=30,
        source=source,
        erasure_token=f"{source}:{task_id}",
        redaction_applied=True,
    )


def _load_a2a_payload(raw: str | None, *, task_id: str, source: str) -> dict[str, Any]:
    if not raw:
        return {}
    loaded = json.loads(raw)
    require_privacy_envelope(loaded)
    envelope = extract_privacy_envelope(loaded)
    if envelope.get("source") != source:
        raise ValueError(f"A2A payload {task_id} has wrong privacy source {envelope.get('source')!r}")
    if envelope.get("privacy_class") != "subject_data":
        raise ValueError(f"A2A payload {task_id} must be subject_data")
    if envelope.get("subject_id") != task_id:
        raise ValueError(f"A2A payload {task_id} has wrong privacy subject {envelope.get('subject_id')!r}")
    payload = loaded.get("payload")
    if not isinstance(payload, dict):
        raise ValueError(f"A2A payload {task_id} must contain a mapping payload")
    return payload


class A2APersistenceMixin:
    """Provide durable task state and startup recovery behavior."""

    if TYPE_CHECKING:
        execute: Any

    _db_available: bool
    _recovery_blocked_reason: str
    _recovery_run: bool

    def _run_startup_recovery(self) -> None:
        """Attempt to re-execute tasks interrupted before the previous shutdown."""
        if getattr(self, "_recovery_run", False):
            return
        self._recovery_run = True

        if not getattr(self, "_db_available", False):
            self._recovery_blocked_reason = "a2a task persistence unavailable; startup recovery cannot run"
            logger.error(
                "A2A startup recovery blocked: task persistence is unavailable; "
                "degraded task acknowledgements will fail closed until persistence is restored"
            )
            return

        pending = self.recover_pending_tasks()
        if not pending:
            return

        logger.warning(
            "Recovered %d interrupted A2A task(s) from previous run; attempting re-execution",
            len(pending),
        )

        for task in pending:
            prior_status = task.status
            try:
                result = self.execute(task)
                if result.status == STATUS_ACKNOWLEDGED:
                    logger.warning(
                        "A2A task id=%s (previously %s) is still unexecutable after restart; marking as orphaned",
                        task.task_id,
                        prior_status,
                    )
                    self._persist_orphaned(task.task_id)
                else:
                    logger.info(
                        "A2A task id=%s recovered with status=%s",
                        task.task_id,
                        result.status,
                    )
            except Exception as exc:
                logger.exception(
                    "A2A task id=%s raised during recovery re-execution; marking as failed: %s",
                    task.task_id,
                    exc,
                )
                failed_result = A2AResult(
                    task_id=task.task_id,
                    status=STATUS_FAILED,
                    error=f"Recovery re-execution failed: {exc}",
                )
                self._persist_result(task.task_id, failed_result)

    def _persist_orphaned(self, task_id: str) -> None:
        """Transition a task row to ``STATUS_ORPHANED`` in the database.

        Args:
            task_id: Identifier of the task to transition.
        """
        if not getattr(self, "_db_available", False):
            return
        try:
            from vetinari.database import get_connection

            now = datetime.now(timezone.utc).isoformat()
            conn = get_connection()
            conn.execute(
                "UPDATE a2a_tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (STATUS_ORPHANED, now, task_id),
            )
            conn.commit()
        except Exception:
            logger.warning(
                "Failed to persist orphaned status for A2A task %s; status inconsistency possible",
                task_id,
            )

    def _init_persistence(self) -> None:
        """Create the ``a2a_tasks`` table for durable task state."""
        try:
            from vetinari.database import get_connection

            conn = get_connection()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS a2a_tasks (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    input_json TEXT,
                    output_json TEXT,
                    error TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()
            self._db_available = True
        except Exception:
            logger.error("A2A task persistence unavailable - tasks will not survive restart", exc_info=True)
            self._recovery_blocked_reason = "a2a task persistence unavailable"
            self._db_available = False

    def _persist_task(self, task: A2ATask) -> None:
        """Persist task state to SQLite on a best-effort basis.

        Args:
            task: Task state to persist.
        """
        if not getattr(self, "_db_available", False):
            return
        try:
            from vetinari.database import get_connection

            now = datetime.now(timezone.utc).isoformat()
            conn = get_connection()
            conn.execute(
                """INSERT OR REPLACE INTO a2a_tasks
                   (task_id, task_type, status, input_json, error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.task_id,
                    task.task_type,
                    task.status,
                    json.dumps(_wrap_a2a_payload(task.input_data, task_id=task.task_id, source="a2a.task.input")),
                    "",
                    now,
                    now,
                ),
            )
            conn.commit()
        except Exception:
            logger.warning(
                "Failed to persist A2A task %s; task will not survive restart",
                task.task_id,
            )

    def _persist_result(self, task_id: str, result: A2AResult) -> None:
        """Persist task result to SQLite on a best-effort basis.

        Args:
            task_id: Identifier of the task to update.
            result: Result state to persist.
        """
        if not getattr(self, "_db_available", False):
            return
        try:
            from vetinari.database import get_connection

            now = datetime.now(timezone.utc).isoformat()
            conn = get_connection()
            conn.execute(
                """UPDATE a2a_tasks
                   SET status = ?, output_json = ?, error = ?, updated_at = ?
                   WHERE task_id = ?""",
                (
                    result.status,
                    json.dumps(
                        _wrap_a2a_payload(
                            result.output_data,
                            task_id=task_id,
                            source="a2a.task.output",
                        )
                    ),
                    result.error,
                    now,
                    task_id,
                ),
            )
            conn.commit()
        except Exception:
            logger.warning(
                "Failed to persist A2A result for task %s; result lost on restart",
                task_id,
            )

    def recover_pending_tasks(self) -> list[A2ATask]:
        """Recover tasks in pending, running, or acknowledged state from the database.

        Returns:
            List of A2ATask objects that need to be re-executed.
        """
        if not getattr(self, "_db_available", False):
            return []
        try:
            from vetinari.database import get_connection

            conn = get_connection()
            rows = conn.execute(
                "SELECT task_id, task_type, status, input_json FROM a2a_tasks WHERE status IN (?, ?, ?)",
                (STATUS_PENDING, STATUS_RUNNING, STATUS_ACKNOWLEDGED),
            ).fetchall()
            tasks = []
            for row in rows:
                try:
                    input_data = _load_a2a_payload(row[3], task_id=row[0], source="a2a.task.input")
                except Exception as exc:
                    logger.warning(
                        "A2A task %s has unrecoverable persisted input; marking orphaned: %s",
                        row[0],
                        exc,
                    )
                    self._persist_orphaned(row[0])
                    continue
                tasks.append(
                    A2ATask(
                        task_id=row[0],
                        task_type=row[1],
                        status=row[2],
                        input_data=input_data,
                    )
                )
            if tasks:
                logger.info("Recovered %d pending A2A tasks from database", len(tasks))
            return tasks
        except Exception:
            logger.warning("A2A task recovery failed; no tasks will be resumed")
            return []

    def _has_db_available(self) -> bool:
        """Return whether persistence is currently available.

        Returns:
            True when the database was initialized successfully.
        """
        return bool(getattr(self, "_db_available", False))
