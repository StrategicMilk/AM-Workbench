"""Lifecycle query and cleanup helpers for durable execution recovery."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.orchestration.durable_execution import DurableExecutionEngine
    from vetinari.orchestration.durable_execution_recovery_protocol import _PausedQuestionsDb

from vetinari.orchestration.durable_execution_checkpointing import load_checkpoint
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


def save_paused_questions(
    engine: _PausedQuestionsDb,
    execution_id: str,
    questions: list[str],
    task_id: str | None = None,
) -> str:
    """Persist questions that require user answers, recording the pause event.

    **Metadata-only storage.** The resume consumer lives in
    ``vetinari.orchestration.clarification.resume_after_clarification``.

    Args:
        engine: The DurableExecutionEngine instance owning the database.
        execution_id: The execution being paused.
        questions: List of questions for the user.
        task_id: Optional task that triggered the pause.

    Returns:
        The question_id for later answer retrieval via ``answer_paused_questions()``.
    """
    question_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    engine._db.execute(
        """INSERT OR IGNORE INTO execution_state
           (execution_id, goal, pipeline_state, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (execution_id, "", "paused", now, now),
    )
    engine._db.execute(
        """INSERT INTO paused_questions
           (question_id, execution_id, task_id, questions_json, asked_at)
           VALUES (?, ?, ?, ?, ?)""",
        (question_id, execution_id, task_id, json.dumps(questions), now),
    )
    logger.info("Pipeline paused: execution=%s, %d questions", execution_id, len(questions))
    return question_id


def answer_paused_questions(engine: DurableExecutionEngine, question_id: str, answers: list[str]) -> None:
    """Store answers for a set of paused questions.

    **Metadata-only storage.** The resume consumer lives in
    ``vetinari.orchestration.clarification.resume_after_clarification``.

    Args:
        engine: The DurableExecutionEngine instance owning the database.
        question_id: The question set to answer.
        answers: User-provided answers in the same order as the questions.
    """
    now = datetime.now(timezone.utc).isoformat()
    engine._db.execute(
        "UPDATE paused_questions SET answers_json = ?, answered_at = ? WHERE question_id = ?",
        (json.dumps(answers), now, question_id),
    )
    logger.info("Questions answered: %s", question_id)


def get_paused_questions(engine: DurableExecutionEngine, execution_id: str) -> list[dict[str, Any]]:
    """Return all paused questions for an execution, with any stored answers.

    **Metadata-only storage.** The resume consumer lives in
    ``vetinari.orchestration.clarification.resume_after_clarification``.

    Args:
        engine: The DurableExecutionEngine instance owning the database.
        execution_id: The execution to query.

    Returns:
        List of question dicts with id, questions, answers, and timestamps.
    """
    rows = engine._db.execute(
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


def recover_execution(engine: DurableExecutionEngine, plan_id: str) -> dict[str, Any]:
    """Recover and continue an execution from its last checkpoint.

    Resets retryable failed tasks (retry_count < max_retries) to PENDING
    and re-executes the plan from the recovered state.

    Args:
        engine: The DurableExecutionEngine instance owning execution state.
        plan_id: The plan identifier to recover.

    Returns:
        Execution result dict identical to what ``execute_plan`` returns,
        or ``{"status": "error", "plan_id": plan_id, "message": "..."}`` if
        no checkpoint exists or the checkpoint is corrupt.
    """
    graph = load_checkpoint(engine, plan_id)

    if not graph:
        return {"status": "error", "plan_id": plan_id, "message": "No checkpoint found"}

    for node in graph.nodes.values():
        if node.status == StatusEnum.RUNNING:
            # RUNNING at recovery time means the process crashed mid-task.
            # Reset to PENDING so the task is retried rather than left stuck.
            node.status = StatusEnum.PENDING
            node.error = "Reset from RUNNING state during recovery — process likely crashed"
        elif node.status == StatusEnum.FAILED and node.retry_count < node.max_retries:
            node.status = StatusEnum.PENDING
            node.error = ""

    incomplete = [
        n for n in graph.nodes.values() if n.status in (StatusEnum.PENDING, StatusEnum.BLOCKED, StatusEnum.FAILED)
    ]
    logger.info("Recovering %s incomplete tasks for plan: %s", len(incomplete), plan_id)

    return engine.execute_plan(graph)


def get_execution_status(engine: DurableExecutionEngine, plan_id: str) -> dict[str, Any] | None:
    """Get the current status of an active or checkpointed execution.

    Args:
        engine: The DurableExecutionEngine instance owning execution state.
        plan_id: The plan identifier to query.

    Returns:
        Dict with plan_id, status, total_tasks, completed count, failed
        count, blocked count, and progress ratio (completed/total).
        Returns None if no active execution or checkpoint is found.
    """
    with engine._execution_lock:
        graph = engine._active_executions.get(plan_id)

    if not graph:
        graph = load_checkpoint(engine, plan_id)

    if not graph:
        return None

    return {
        "plan_id": plan_id,
        "status": graph.status.value,
        "total_tasks": len(graph.nodes),
        StatusEnum.COMPLETED.value: len(graph.get_completed_tasks()),
        StatusEnum.FAILED.value: len(graph.get_failed_tasks()),
        StatusEnum.BLOCKED.value: len(graph.get_blocked_tasks()),
        "progress": (len(graph.get_completed_tasks()) / len(graph.nodes) if graph.nodes else 0),
    }


def list_checkpoints(engine: DurableExecutionEngine) -> list[str]:
    """List all plan IDs with persisted checkpoints.

    Args:
        engine: The DurableExecutionEngine instance owning the database.

    Returns:
        Sorted list of execution IDs from the execution_state table.
    """
    _store = getattr(engine, "_checkpoint_store", None)
    if _store is not None:
        return _store.list_checkpoint_ids()
    rows = engine._db.execute("SELECT execution_id FROM execution_state")
    return sorted({r[0] for r in rows})


def recover_incomplete_executions(
    engine: DurableExecutionEngine,
    task_handler: Callable | None = None,
) -> list[dict[str, Any]]:
    """Find and resume all incomplete executions from persisted checkpoints.

    Queries SQLite for executions whose ``pipeline_state`` is neither
    ``completed`` nor ``failed``, loads each checkpoint, resets retryable
    failed tasks, and re-executes them. Called at startup to ensure
    crash-interrupted work is resumed automatically.

    Args:
        engine: The DurableExecutionEngine instance owning execution state.
        task_handler: Optional default handler. If not provided, tasks
            rely on previously registered handlers.

    Returns:
        List of per-execution result dicts. Empty list when nothing
        needs recovery.
    """
    _store = getattr(engine, "_checkpoint_store", None)
    if _store is not None:
        plan_ids = _store.find_incomplete_ids(StatusEnum.COMPLETED.value, StatusEnum.FAILED.value)
    else:
        rows = engine._db.execute(
            "SELECT execution_id FROM execution_state WHERE pipeline_state NOT IN (?, ?)",
            (StatusEnum.COMPLETED.value, StatusEnum.FAILED.value),
        )
        plan_ids = [r[0] for r in rows] if rows else []

    if not plan_ids:
        logger.info("No incomplete executions found — nothing to recover")
        return []
    logger.info("Found %d incomplete execution(s) to recover: %s", len(plan_ids), plan_ids)

    results: list[dict[str, Any]] = []
    for plan_id in plan_ids:
        try:
            if task_handler:
                engine._task_handlers.setdefault("default", task_handler)

            # Heartbeat staleness is in-process only; across restart the
            # in-memory heartbeat dict is always empty, so is_task_stuck()
            # would always return False and the check would be a no-op.
            # RUNNING-task reset is handled inside recover_execution() which
            # resets every persisted RUNNING node to PENDING unconditionally.

            result = recover_execution(engine, plan_id)
            results.append(result)
            logger.info(
                "Recovered execution %s: completed=%s, failed=%s",
                plan_id,
                result.get(StatusEnum.COMPLETED.value, 0),
                result.get(StatusEnum.FAILED.value, 0),
            )
        except Exception as exc:
            logger.error(
                "Failed to recover execution %s — skipping, other executions will still be attempted: %s",
                plan_id,
                exc,
            )
            results.append({
                "plan_id": plan_id,
                "status": "error",
                "message": f"Recovery failed: {exc}",
            })
    return results


def cleanup_completed(engine: DurableExecutionEngine, max_age_days: int = 30) -> int:
    """Delete completed executions older than max_age_days from SQLite.

    Args:
        engine: The DurableExecutionEngine instance owning the database.
        max_age_days: Remove executions completed more than this many days ago.

    Returns:
        Number of executions deleted.
    """
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()

    _store = getattr(engine, "_checkpoint_store", None)
    if _store is not None:
        ids = _store.find_completed_before(cutoff)
        for exec_id in ids:
            _store.delete_execution(exec_id)
    else:
        rows = engine._db.execute(
            "SELECT execution_id FROM execution_state WHERE completed_at IS NOT NULL AND completed_at <= ?",
            (cutoff,),
        )
        ids = [r[0] for r in rows] if rows else []
        for exec_id in ids:
            engine._db.execute_in_transaction([
                ("DELETE FROM execution_events WHERE execution_id = ?", (exec_id,)),
                ("DELETE FROM task_checkpoints WHERE execution_id = ?", (exec_id,)),
                ("DELETE FROM paused_questions WHERE execution_id = ?", (exec_id,)),
                ("DELETE FROM execution_state WHERE execution_id = ?", (exec_id,)),
            ])

    if not ids:
        return 0
    logger.info("Cleaned up %d completed executions older than %d days", len(ids), max_age_days)
    return len(ids)
