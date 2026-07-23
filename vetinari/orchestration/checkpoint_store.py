"""Checkpoint persistence layer for durable execution.

Provides the SQLite-backed storage primitives used by ``DurableExecutionEngine``
to survive crashes and resume interrupted plans.

Pipeline role: Plan -> DurableExecution -> **CheckpointStore** (persist) -> Verify -> Learn.
This is the storage half of the durable execution system; the engine half lives in
``durable_execution.py``.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import _PROJECT_ROOT
from vetinari.orchestration.checkpoint_store_db import (
    _SCHEMA_SQL as _SCHEMA_SQL,
)
from vetinari.orchestration.checkpoint_store_db import (
    CheckpointDatabaseManager,
    _normalize_task_checkpoint_row,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    """An immutable record of a single state transition in execution history."""

    event_id: str
    event_type: str  # task_started, task_completed, task_failed, etc.
    task_id: str
    timestamp: str
    data: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"ExecutionEvent(event_id={self.event_id!r}, event_type={self.event_type!r}, "
            f"task_id={self.task_id!r}, timestamp={self.timestamp!r})"
        )


@dataclass
class Checkpoint:
    """A point-in-time snapshot of an in-progress execution graph."""

    checkpoint_id: str
    plan_id: str
    created_at: str
    graph_state: dict[str, Any]
    completed_tasks: list[str]
    running_tasks: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"Checkpoint(checkpoint_id={self.checkpoint_id!r}, plan_id={self.plan_id!r}, "
            f"completed_tasks={len(self.completed_tasks)}, "
            f"running_tasks={len(self.running_tasks)})"
        )


class CheckpointStore:
    """Persistence facade for durable execution state.

    Wraps ``CheckpointDatabaseManager`` and exposes higher-level methods for saving,
    loading, and querying execution checkpoints, events, and paused questions.
    ``DurableExecutionEngine`` owns one of these and calls it on every state
    transition to ensure crash-safe durability.

    Args:
        checkpoint_dir: Optional directory path for standalone SQLite databases
            (used in tests). When ``None``, production code stores data in the
            consolidated ``vetinari.database`` database (ADR-0072).
    """

    def __init__(self, checkpoint_dir: Path | None = None) -> None:
        if checkpoint_dir is None:
            db_path = None
        else:
            db_path = checkpoint_dir / "execution_state.db"
        self._db = CheckpointDatabaseManager(db_path)

    # ------------------------------------------------------------------
    # Event persistence
    # ------------------------------------------------------------------

    def save_event(self, event: ExecutionEvent, execution_id: str = "") -> None:
        """Persist an execution event to SQLite for audit trail and crash recovery.

        Failures are logged at WARNING and swallowed — event persistence must
        not interrupt task execution.

        Args:
            event: The event to persist.
            execution_id: Optional execution ID for foreign key linkage.
        """
        try:
            self._db.execute(
                """INSERT OR IGNORE INTO execution_events
                   (event_id, execution_id, event_type, task_id, timestamp, data_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    execution_id,
                    event.event_type,
                    event.task_id,
                    event.timestamp,
                    json.dumps(event.data),
                ),
            )
        except Exception:
            logger.warning("Failed to persist event %s to SQLite", event.event_id, exc_info=True)

    # ------------------------------------------------------------------
    # Checkpoint save / load
    # ------------------------------------------------------------------

    def save_checkpoint(
        self,
        plan_id: str,
        graph_dict: dict[str, Any],
        pipeline_state: str,
        task_rows: list[tuple],
        now: str,
        completed_at: str | None = None,
        terminal_status: str | None = None,
    ) -> None:
        """Persist a plan's execution graph state to SQLite (atomic, crash-safe).

        When the plan has reached a terminal state, pass ``completed_at`` and
        ``terminal_status`` so retention and cleanup queries can identify and
        act on finished executions.  Both default to ``None`` for in-progress
        checkpoints so partially-complete rows are never mistaken for finished ones.

        Args:
            plan_id: The plan identifier.
            graph_dict: Serialized graph dict from ``ExecutionGraph.to_dict()``.
            pipeline_state: Current pipeline state string (e.g. ``"executing"``).
            task_rows: Pre-built parameter tuples for the ``task_checkpoints`` upsert.
            now: ISO-format UTC timestamp for ``updated_at``.
            completed_at: ISO-format UTC timestamp to write as ``completed_at`` when
                terminal; ``None`` for in-progress checkpoints.
            terminal_status: Enum value string (e.g. ``"completed"``) to write when
                terminal; ``None`` for in-progress checkpoints.
        """
        state_sql = (
            """INSERT INTO execution_state
                   (execution_id, goal, pipeline_state, task_dag_json, created_at, updated_at,
                    completed_at, terminal_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(execution_id) DO UPDATE SET
                   pipeline_state = excluded.pipeline_state,
                   task_dag_json = excluded.task_dag_json,
                   updated_at = excluded.updated_at,
                   completed_at = excluded.completed_at,
                   terminal_status = excluded.terminal_status""",
            (
                plan_id,
                graph_dict.get("goal", ""),
                pipeline_state,
                json.dumps(graph_dict),
                graph_dict.get("created_at", now),
                now,
                completed_at,
                terminal_status,
            ),
        )
        normalized_task_rows = [_normalize_task_checkpoint_row(row) for row in task_rows]
        checkpoint_many = (
            (
                """INSERT INTO task_checkpoints
                   (task_id, execution_id, node_id, superstep_index, substep_index, agent_type, mode, status,
                    input_json, output_json, failure_json, manifest_hash, started_at, completed_at,
                    retry_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(execution_id, task_id) DO UPDATE SET
                       node_id = excluded.node_id,
                       superstep_index = excluded.superstep_index,
                       substep_index = excluded.substep_index,
                       agent_type = excluded.agent_type,
                       mode = excluded.mode,
                       status = excluded.status,
                       input_json = excluded.input_json,
                       output_json = excluded.output_json,
                       failure_json = excluded.failure_json,
                       manifest_hash = excluded.manifest_hash,
                       started_at = excluded.started_at,
                       completed_at = excluded.completed_at,
                       retry_count = excluded.retry_count""",
                normalized_task_rows,
            )
            if normalized_task_rows
            else None
        )
        self._db.execute_in_transaction(
            statements=[state_sql],
            many_statements=[checkpoint_many] if checkpoint_many else None,
        )

    def load_checkpoint_graph_json(self, plan_id: str) -> str | None:
        """Load the raw serialized graph JSON for a plan from SQLite.

        Args:
            plan_id: Plan identifier to query.

        Returns:
            JSON string if a checkpoint exists, or None.
        """
        rows = self._db.execute(
            "SELECT task_dag_json FROM execution_state WHERE execution_id = ?",
            (plan_id,),
        )
        if not rows or not rows[0][0]:
            return None
        return rows[0][0]

    def list_checkpoint_ids(self) -> list[str]:
        """Return all plan IDs that have persisted checkpoints.

        Returns:
            Sorted list of execution IDs.
        """
        rows = self._db.execute("SELECT execution_id FROM execution_state")
        return sorted({r[0] for r in rows})

    def find_incomplete_ids(self, completed_state: str, failed_state: str) -> list[str]:
        """Query for execution IDs whose pipeline state is neither completed nor failed.

        Args:
            completed_state: The string value of the completed status enum.
            failed_state: The string value of the failed status enum.

        Returns:
            List of plan IDs pending recovery.
        """
        rows = self._db.execute(
            "SELECT execution_id FROM execution_state WHERE pipeline_state NOT IN (?, ?)",
            (completed_state, failed_state),
        )
        return [r[0] for r in rows]

    def list_failed_task_recovery_points(self, execution_id: str) -> list[dict[str, Any]]:
        """Return failed or cancelled task checkpoints for targeted recovery.

        Args:
            execution_id: The durable execution ID to inspect.

        Returns:
            Task recovery records ordered by superstep and node ID.
        """
        rows = self._db.execute(
            """SELECT task_id, node_id, superstep_index, substep_index, status, failure_json, retry_count
               FROM task_checkpoints
               WHERE execution_id = ? AND status IN ('failed', 'cancelled', 'blocked')
               ORDER BY superstep_index, node_id, task_id""",
            (execution_id,),
        )
        recovery_points: list[dict[str, Any]] = []
        for task_id, node_id, superstep_index, substep_index, status, failure_json, retry_count in rows:
            failure = json.loads(failure_json) if failure_json else None
            recovery_points.append({
                "task_id": task_id,
                "node_id": node_id,
                "superstep_index": superstep_index,
                "substep_index": substep_index,
                "status": status,
                "failure": failure,
                "retry_count": retry_count,
            })
        return recovery_points

    def find_completed_before(self, cutoff_iso: str) -> list[str]:
        """Find execution IDs that completed at or before a cutoff timestamp.

        Args:
            cutoff_iso: ISO-format UTC timestamp; executions completed no later than
                this value are returned.

        Returns:
            List of execution IDs eligible for cleanup.
        """
        rows = self._db.execute(
            "SELECT execution_id FROM execution_state WHERE completed_at IS NOT NULL AND completed_at <= ?",
            (cutoff_iso,),
        )
        return [r[0] for r in rows]

    def list_retention_candidates(self, older_than_seconds: float) -> list[str]:
        """Return execution IDs that finished more than *older_than_seconds* ago.

        Queries for rows where ``completed_at`` is set and older than the
        derived cutoff.  Only rows that have a ``terminal_status`` value (i.e.
        rows written by the updated ``save_checkpoint``) are returned, so
        in-progress executions whose ``completed_at`` is NULL are never
        included.

        Args:
            older_than_seconds: Age threshold in seconds.  Executions whose
                ``completed_at`` timestamp is older than this are candidates
                for deletion.

        Returns:
            List of execution IDs eligible for retention cleanup.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        return self.find_completed_before(cutoff)

    def delete_execution(self, exec_id: str) -> None:
        """Remove all persisted state for a completed execution.

        Args:
            exec_id: The execution ID to purge from the database.
        """
        self._db.execute_in_transaction(
            statements=[
                ("DELETE FROM execution_events WHERE execution_id = ?", (exec_id,)),
                ("DELETE FROM task_checkpoints WHERE execution_id = ?", (exec_id,)),
                ("DELETE FROM paused_questions WHERE execution_id = ?", (exec_id,)),
                ("DELETE FROM execution_state WHERE execution_id = ?", (exec_id,)),
            ],
        )

    # ------------------------------------------------------------------
    # Pause / resume for clarification questions
    # ------------------------------------------------------------------

    def save_paused_questions(
        self,
        execution_id: str,
        questions: list[str],
        task_id: str | None = None,
    ) -> str:
        """Persist a set of questions that require user input before execution can resume.

        Inserts a stub ``execution_state`` row if one does not already exist so
        the foreign key constraint is satisfied.

        Args:
            execution_id: Execution being paused.
            questions: Questions to surface to the user.
            task_id: Optional task that triggered the pause.

        Returns:
            A unique ``question_id`` for later answer retrieval.
        """
        question_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            """INSERT OR IGNORE INTO execution_state
               (execution_id, goal, pipeline_state, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (execution_id, "", "paused", now, now),
        )
        self._db.execute(
            """INSERT INTO paused_questions (question_id, execution_id, task_id, questions_json, asked_at)
               VALUES (?, ?, ?, ?, ?)""",
            (question_id, execution_id, task_id, json.dumps(questions), now),
        )
        return question_id

    def answer_paused_questions(self, question_id: str, answers: list[str]) -> None:
        """Record user answers for a paused question set, enabling pipeline resume.

        Args:
            question_id: The question set identifier.
            answers: User-provided answers in order.
        """
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "UPDATE paused_questions SET answers_json = ?, answered_at = ? WHERE question_id = ?",
            (json.dumps(answers), now, question_id),
        )

    def get_paused_questions(self, execution_id: str) -> list[dict[str, Any]]:
        """Retrieve all paused question sets for an execution.

        Args:
            execution_id: Execution to query.

        Returns:
            List of question dicts with ``question_id``, ``task_id``,
            ``questions``, ``answers``, ``asked_at``, and ``answered_at``.
        """
        rows = self._db.execute(
            "SELECT question_id, task_id, questions_json, answers_json, asked_at, answered_at "
            "FROM paused_questions WHERE execution_id = ?",
            (execution_id,),
        )
        return [
            {
                "question_id": r[0],
                "task_id": r[1],
                "questions": json.loads(r[2]),
                "answers": json.loads(r[3]) if r[3] else None,
                "asked_at": r[4],
                "answered_at": r[5],
            }
            for r in rows
        ]

    def close(self) -> None:
        """Close the underlying database connection."""
        self._db.close()


# -- Module-level constant for the default checkpoint directory ----------------
#    Used by DurableExecutionEngine when no explicit directory is provided.
DEFAULT_CHECKPOINT_DIR: Path = _PROJECT_ROOT / "vetinari_checkpoints"
