"""Recovery and checkpoint facade methods for durable execution."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from vetinari.orchestration.durable_execution_recovery import (
    answer_paused_questions as _answer_paused_questions,
)
from vetinari.orchestration.durable_execution_recovery import (
    cleanup_completed as _cleanup_completed,
)
from vetinari.orchestration.durable_execution_recovery import (
    emit_event as _emit_event_fn,
)
from vetinari.orchestration.durable_execution_recovery import (
    get_execution_status as _get_execution_status,
)
from vetinari.orchestration.durable_execution_recovery import (
    get_paused_questions as _get_paused_questions,
)
from vetinari.orchestration.durable_execution_recovery import (
    handle_layer_failure as _handle_layer_failure_fn,
)
from vetinari.orchestration.durable_execution_recovery import (
    list_checkpoints as _list_checkpoints,
)
from vetinari.orchestration.durable_execution_recovery import (
    load_checkpoint as _load_checkpoint,
)
from vetinari.orchestration.durable_execution_recovery import (
    record_learning as _record_learning,
)
from vetinari.orchestration.durable_execution_recovery import (
    recover_execution as _recover_execution,
)
from vetinari.orchestration.durable_execution_recovery import (
    recover_incomplete_executions as _recover_incomplete_executions,
)
from vetinari.orchestration.durable_execution_recovery import (
    save_checkpoint as _save_checkpoint,
)
from vetinari.orchestration.durable_execution_recovery import (
    save_paused_questions as _save_paused_questions,
)
from vetinari.orchestration.execution_graph import ExecutionGraph, ExecutionTaskNode

if TYPE_CHECKING:
    from vetinari.orchestration.checkpoint_store import CheckpointStore

    _answer_paused_questions: Callable[[Any, str, list[str]], None]
    _cleanup_completed: Callable[[Any, int], int]
    _emit_event_fn: Callable[[Any, str, str, dict[str, Any], str], None]
    _get_execution_status: Callable[[Any, str], dict[str, Any] | None]
    _get_paused_questions: Callable[[Any, str], list[dict[str, Any]]]
    _handle_layer_failure_fn: Callable[[Any, ExecutionGraph, list[ExecutionTaskNode]], None]
    _list_checkpoints: Callable[[Any], list[str]]
    _load_checkpoint: Callable[[Any, str], ExecutionGraph | None]
    _recover_execution: Callable[[Any, str], dict[str, Any]]
    _recover_incomplete_executions: Callable[[Any, Callable | None], list[dict[str, Any]]]
    _save_checkpoint: Callable[[Any, str, ExecutionGraph], None]


class _DurableExecutionRecoveryMixin:
    """Expose checkpoint, recovery, event, and learning helpers on the engine."""

    if TYPE_CHECKING:
        _active_executions: dict[str, ExecutionGraph]
        _checkpoint_store: CheckpointStore
        _db: Any
        _execution_lock: threading.Lock

    @staticmethod
    def _record_learning(task: ExecutionTaskNode, task_id: str, output: Any) -> None:
        """Record task outcome for learning pipeline."""
        _record_learning(task, task_id, output)

    def _handle_layer_failure(self, graph: ExecutionGraph, failed_tasks: list[ExecutionTaskNode]) -> None:
        """Cancel transitive dependants of failed tasks."""
        _handle_layer_failure_fn(self, graph, failed_tasks)

    def _emit_event(
        self,
        event_type: str,
        task_id: str,
        data: dict[str, Any],
        execution_id: str = "",
    ) -> None:
        """Emit and persist an execution event."""
        if not execution_id:
            with self._execution_lock:
                for plan_id, graph in self._active_executions.items():
                    if task_id in graph.nodes:
                        execution_id = plan_id
                        break
        _emit_event_fn(self, event_type, task_id, data, execution_id)

    def _save_checkpoint(self, plan_id: str, graph: ExecutionGraph) -> None:
        """Persist execution state to SQLite."""
        _save_checkpoint(self, plan_id, graph)

    def load_checkpoint(self, plan_id: str) -> ExecutionGraph | None:
        """Load persisted execution graph."""
        return _load_checkpoint(self, plan_id)

    def save_paused_questions(
        self,
        execution_id: str,
        questions: list[str],
        task_id: str | None = None,
    ) -> str:
        """Pause pipeline and persist user questions. Returns question_id."""
        return _save_paused_questions(self, execution_id, questions, task_id)

    def answer_paused_questions(self, question_id: str, answers: list[str]) -> None:
        """Store answers for paused questions, enabling pipeline resume.

        Args:
            question_id: The question set identifier returned by ``pause_for_input``.
            answers: Ordered list of answer strings matching the original questions.
        """
        _answer_paused_questions(self, question_id, answers)

    def get_paused_questions(self, execution_id: str) -> list[dict[str, Any]]:
        """Return all unanswered questions for an execution."""
        return _get_paused_questions(self, execution_id)

    def recover_execution(self, plan_id: str) -> dict[str, Any]:
        """Resume an execution from its last checkpoint."""
        return _recover_execution(self, plan_id)

    def get_execution_status(self, plan_id: str) -> dict[str, Any] | None:
        """Return current status dict for an active or checkpointed execution."""
        return _get_execution_status(self, plan_id)

    def list_checkpoints(self) -> list[str]:
        """Return sorted list of all persisted execution IDs."""
        return _list_checkpoints(self)

    def recover_incomplete_executions(
        self,
        task_handler: Callable | None = None,
    ) -> list[dict[str, Any]]:
        """Find and resume all incomplete executions."""
        return _recover_incomplete_executions(self, task_handler)

    def cleanup_completed(self, max_age_days: int = 30) -> int:
        """Delete completed executions older than *max_age_days* days."""
        return _cleanup_completed(self, max_age_days)

    def list_retention_candidates(self, older_than_seconds: float) -> list[str]:
        """Return execution IDs that finished more than *older_than_seconds* ago.

        Delegates to ``CheckpointStore.list_retention_candidates`` which queries
        ``completed_at`` and ``terminal_status`` to identify executions eligible
        for deletion. Only executions that reached a terminal state (COMPLETED
        or FAILED) are returned; in-progress executions are never included.

        Args:
            older_than_seconds: Age threshold in seconds.

        Returns:
            List of execution IDs eligible for retention cleanup.
        """
        return self._checkpoint_store.list_retention_candidates(older_than_seconds)
