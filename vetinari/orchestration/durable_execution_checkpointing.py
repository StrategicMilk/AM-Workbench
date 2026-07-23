"""Checkpoint persistence helpers for durable execution recovery."""

from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.orchestration.durable_execution import DurableExecutionEngine

from vetinari.orchestration.execution_graph import ExecutionGraph, ExecutionTaskNode
from vetinari.types import PlanStatus, StatusEnum

logger = logging.getLogger(__name__)


def _learning_success(task: ExecutionTaskNode, output: Any, quality_score: float) -> bool:
    """Derive the learning success flag from task outcome and scored quality."""
    status = getattr(task, "status", None)
    if status in (StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.BLOCKED):
        return False
    if isinstance(output, dict):
        output_status = str(output.get("status", "")).lower()
        if output_status in {StatusEnum.FAILED.value, StatusEnum.CANCELLED.value, StatusEnum.BLOCKED.value, "error"}:
            return False
    return quality_score >= 0.5


def record_learning(task: ExecutionTaskNode, task_id: str, output: Any) -> None:
    """Record task outcome for the learning pipeline (non-fatal side effect).

    Scores the output, records the outcome in the feedback loop, and
    updates Thompson Sampling arms. Silently no-ops on any exception so
    that learning failures never interrupt execution.

    Args:
        task: The completed task node.
        task_id: String task identifier for logging.
        output: Raw handler output for quality scoring.
    """
    try:
        output_str = output if isinstance(output, str) else str(output)[:800]
        model_id = task.input_data.get("assigned_model") or task.assigned_model or "default"
        task_type_str = task.task_type.lower() if hasattr(task, "task_type") and task.task_type else "general"

        from vetinari.learning.quality_scorer import get_quality_scorer

        scorer = get_quality_scorer()
        q_score = scorer.score(
            task_id=task_id,
            model_id=model_id,
            task_type=task_type_str,
            task_description=task.description or "",
            output=output_str,
            use_llm=False,
        )

        from vetinari.learning.feedback_loop import get_feedback_loop

        success = _learning_success(task, output, q_score.overall_score)
        get_feedback_loop().record_outcome(
            task_id=task_id,
            model_id=model_id,
            task_type=task_type_str,
            quality_score=q_score.overall_score,
            success=success,
        )

        from vetinari.learning.model_selector import get_thompson_selector

        get_thompson_selector().update(model_id, task_type_str, q_score.overall_score, success)

        if q_score.overall_score < 0.5:
            logger.warning(
                "[DurableExec] Low quality score %.2f for task %s (model=%s, type=%s) — review output quality",
                q_score.overall_score,
                task_id,
                model_id,
                task_type_str,
            )
    except Exception as _learn_err:
        logger.warning(
            "Learning hook failed for task %s — execution result unaffected: %s",
            task_id,
            _learn_err,
        )


def emit_event(
    engine: DurableExecutionEngine,
    event_type: str,
    task_id: str,
    data: dict[str, Any],
    execution_id: str = "",
) -> None:
    """Emit an execution event and persist it to SQLite.

    Stores events in both the in-memory deque (fast access) and the
    ``execution_events`` table (crash recovery and audit trail).

    Args:
        engine: The DurableExecutionEngine instance owning the database.
        event_type: Type of event (e.g. ``task_started``).
        task_id: The task this event relates to.
        data: Additional event payload.
        execution_id: Optional execution ID for foreign key linkage.
    """
    import json
    import uuid

    from vetinari.orchestration.durable_db import ExecutionEventRecord

    event = ExecutionEventRecord(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        task_id=task_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        data=data,
    )
    engine._event_history.append(event)
    logger.debug("Event: %s - %s", event_type, task_id)

    # Delegate persistence to CheckpointStore so event storage goes through
    # the named facade rather than raw SQL here.  Falls back to the direct
    # _db path if the store is not yet initialised (e.g. during engine startup).
    _store = getattr(engine, "_checkpoint_store", None)
    if _store is not None:
        _store.save_event(event, execution_id)
    else:
        try:
            engine._db.execute(
                """INSERT OR IGNORE INTO execution_events
                   (event_id, execution_id, event_type, task_id, timestamp, data_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    event.event_id,
                    execution_id,
                    event_type,
                    task_id,
                    event.timestamp,
                    json.dumps(data),
                ),
            )
        except Exception:
            logger.warning(
                "Failed to persist event %s to SQLite — execution continues",
                event.event_id,
                exc_info=True,
            )


def handle_layer_failure(
    engine: DurableExecutionEngine,
    graph: ExecutionGraph,
    failed_tasks: list[ExecutionTaskNode],
) -> None:
    """Cancel tasks that depend (transitively) on any failed task.

    Args:
        engine: The DurableExecutionEngine instance used to emit cancellation events.
        graph: The execution graph to update in-place.
        failed_tasks: Tasks that failed in the current layer.
    """
    cancelled_ids: set[str] = {t.id for t in failed_tasks}
    reverse_dependencies: dict[str, list[ExecutionTaskNode]] = defaultdict(list)
    for node in graph.nodes.values():
        for dependency_id in node.depends_on:
            reverse_dependencies[dependency_id].append(node)

    pending_dependency_ids = deque(cancelled_ids)
    visited_dependency_ids = set(cancelled_ids)
    while pending_dependency_ids:
        dependency_id = pending_dependency_ids.popleft()
        for node in reverse_dependencies.get(dependency_id, ()):
            if node.id in cancelled_ids:
                continue
            if node.status in (StatusEnum.COMPLETED, StatusEnum.FAILED, StatusEnum.CANCELLED):
                continue
            failed_dependencies = [dep for dep in node.depends_on if dep in cancelled_ids]
            node.status = StatusEnum.CANCELLED
            cancelled_ids.add(node.id)
            if node.id not in visited_dependency_ids:
                pending_dependency_ids.append(node.id)
                visited_dependency_ids.add(node.id)
            emit_event(
                engine,
                "task_cancelled",
                node.id,
                {
                    "reason": "dependency_failed",
                    "failed_dependencies": failed_dependencies,
                },
            )


def _checkpoint_terminal_values(graph: ExecutionGraph, now: str) -> tuple[str | None, str | None]:
    is_terminal = graph.status in (PlanStatus.COMPLETED, PlanStatus.FAILED)
    return (now if is_terminal else None, graph.status.value if is_terminal else None)


def _task_failure_json(graph: ExecutionGraph, node: ExecutionTaskNode) -> str | None:
    node_error = getattr(node, "error", "")
    if not node_error and node.status not in (StatusEnum.FAILED, StatusEnum.CANCELLED, StatusEnum.BLOCKED):
        return None
    return json.dumps({
        "error": node_error or None,
        "status": node.status.value,
        "failed_dependencies": [
            dep_id
            for dep_id in getattr(node, "depends_on", [])
            if graph.nodes.get(dep_id) and graph.nodes[dep_id].status == StatusEnum.FAILED
        ],
    })


def _checkpoint_task_rows(plan_id: str, graph: ExecutionGraph) -> list[tuple[Any, ...]]:
    return [
        (
            node.id,
            plan_id,
            node.id,
            getattr(graph, "current_layer", 0),
            getattr(node, "task_type", ""),
            "",
            node.status.value,
            json.dumps(node.input_data) if hasattr(node, "input_data") and node.input_data else None,
            json.dumps(node.output_data) if hasattr(node, "output_data") and node.output_data else None,
            _task_failure_json(graph, node),
            None,
            getattr(node, "started_at", None),
            getattr(node, "completed_at", None),
            getattr(node, "retry_count", 0),
        )
        for node in graph.nodes.values()
    ]


def _save_checkpoint_fallback(
    engine: DurableExecutionEngine,
    plan_id: str,
    graph: ExecutionGraph,
    graph_dict: dict[str, Any],
    task_rows: list[tuple[Any, ...]],
    now: str,
    completed_at: str | None,
    terminal_status: str | None,
) -> None:
    engine._db.execute(
        """INSERT INTO execution_state
               (execution_id, goal, pipeline_state, task_dag_json, created_at, updated_at,
                completed_at, terminal_status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(execution_id) DO UPDATE SET
               pipeline_state = excluded.pipeline_state,
               task_dag_json = excluded.task_dag_json,
               updated_at = excluded.updated_at,
               completed_at = COALESCE(execution_state.completed_at, excluded.completed_at),
               terminal_status = COALESCE(execution_state.terminal_status, excluded.terminal_status)""",
        (
            plan_id,
            graph_dict.get("goal", ""),
            graph.status.value,
            json.dumps(graph_dict),
            graph_dict.get("created_at", now),
            now,
            completed_at,
            terminal_status,
        ),
    )
    if task_rows:
        engine._db.executemany(
            """INSERT INTO task_checkpoints
               (task_id, execution_id, node_id, superstep_index, agent_type, mode, status,
                input_json, output_json, failure_json, manifest_hash, started_at, completed_at,
                retry_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(execution_id, task_id) DO UPDATE SET
                   node_id = excluded.node_id,
                   superstep_index = excluded.superstep_index,
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
            task_rows,
        )


def save_checkpoint(engine: DurableExecutionEngine, plan_id: str, graph: ExecutionGraph) -> None:
    """Save a checkpoint of the execution state to SQLite (atomic, crash-safe).

    When the graph has reached a terminal state (COMPLETED or FAILED), also
    writes ``completed_at`` and ``terminal_status`` so retention/cleanup queries
    can find and act on finished executions.

    Args:
        engine: The DurableExecutionEngine instance owning the database.
        plan_id: The plan identifier.
        graph: The execution graph to persist.
    """
    now = datetime.now(timezone.utc).isoformat()
    graph_dict = graph.to_dict()
    completed = [t.id for t in graph.get_completed_tasks()]
    running = [t.id for t in graph.nodes.values() if t.status == StatusEnum.RUNNING]
    completed_at, terminal_status = _checkpoint_terminal_values(graph, now)
    task_rows = _checkpoint_task_rows(plan_id, graph)

    # Route all writes through _checkpoint_store (single connection) to prevent
    # "database is locked" errors when engine._db and _checkpoint_store._db both
    # try to write the same SQLite file concurrently (ADR-0073).
    _store = getattr(engine, "_checkpoint_store", None)
    if _store is not None:
        _store.save_checkpoint(
            plan_id,
            graph_dict,
            graph.status.value,
            task_rows,
            now,
            completed_at=completed_at,
            terminal_status=terminal_status,
        )
    else:
        _save_checkpoint_fallback(engine, plan_id, graph, graph_dict, task_rows, now, completed_at, terminal_status)

    logger.debug(
        "Checkpoint saved: plan=%s, completed=%d, running=%d",
        plan_id,
        len(completed),
        len(running),
    )


def load_checkpoint(engine: DurableExecutionEngine, plan_id: str) -> ExecutionGraph | None:
    """Load a checkpoint from SQLite to resume execution.

    Args:
        engine: The DurableExecutionEngine instance owning the database.
        plan_id: The plan identifier to resume.

    Returns:
        The restored ExecutionGraph, or None if no checkpoint exists.
    """
    _store = getattr(engine, "_checkpoint_store", None)
    if _store is not None:
        raw_json = _store.load_checkpoint_graph_json(plan_id)
    else:
        rows = engine._db.execute(
            "SELECT task_dag_json FROM execution_state WHERE execution_id = ?",
            (plan_id,),
        )
        raw_json = rows[0][0] if rows and rows[0][0] else None

    if not raw_json:
        logger.warning("No checkpoint found for plan: %s", plan_id)
        return None

    try:
        graph_data = json.loads(raw_json)
        graph = ExecutionGraph(
            plan_id=graph_data["plan_id"],
            goal=graph_data["goal"],
            created_at=graph_data["created_at"],
            updated_at=graph_data["updated_at"],
            status=PlanStatus(graph_data["status"]),
            current_layer=graph_data.get("current_layer", 0),
            completed_count=graph_data.get("completed_count", 0),
            failed_count=graph_data.get("failed_count", 0),
        )

        for node_id, node_data in graph_data["nodes"].items():
            graph.nodes[node_id] = ExecutionTaskNode.from_dict(node_data)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.warning(
            "Checkpoint for plan %s is corrupt or has an invalid schema — returning None"
            " (cannot recover this checkpoint; a fresh run is required). Detail: %s",
            plan_id,
            exc,
        )
        return None

    with engine._execution_lock:
        engine._active_executions[plan_id] = graph

    logger.info("Loaded checkpoint from SQLite for plan: %s", plan_id)
    return graph
