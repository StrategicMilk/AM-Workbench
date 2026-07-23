"""A2A task executor: routes incoming A2A tasks to the Vetinari pipeline.

The :class:`VetinariA2AExecutor` is the stable public facade between the
external A2A protocol world and Vetinari's internal three-agent factory
pipeline. Incoming A2A tasks carry a ``task_type`` string; the executor maps
that string to an ``(AgentType, mode)`` pair and dispatches accordingly.
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.a2a.executor_dispatch import A2ADispatchMixin
from vetinari.a2a.executor_models import (
    STATUS_ACKNOWLEDGED,
    STATUS_COMPLETED,
    STATUS_DEGRADED_UNRECOVERABLE,
    STATUS_FAILED,
    STATUS_ORPHANED,
    STATUS_PENDING,
    STATUS_RUNNING,
    A2AResult,
    A2ATask,
    _RoutingTable,
)
from vetinari.a2a.executor_persistence import A2APersistenceMixin
from vetinari.a2a.executor_routing import A2ARoutingMixin

logger = logging.getLogger(__name__)


# Imported at module level so tests and downstream code can patch
# vetinari.a2a.executor.get_two_layer_orchestrator.
try:
    from vetinari.agents.contracts import AgentTask as _ImportedAgentTask
    from vetinari.orchestration.two_layer import get_two_layer_orchestrator as _imported_get_two_layer_orchestrator
except ImportError:  # orchestration layer not available (e.g. stripped install)
    logger.debug("TwoLayerOrchestrator not available; A2A executor will run in acknowledgement-only mode")
    _ImportedAgentTask = None

    def _imported_get_two_layer_orchestrator() -> Any:
        """Return no orchestrator when the orchestration layer is unavailable.

        Returns:
            None, indicating degraded acknowledgement-only mode.
        """
        return None


AgentTask: Any = _ImportedAgentTask


def get_two_layer_orchestrator() -> Any:
    """Return the optional two-layer orchestrator when available.

    Returns:
        The active two-layer orchestrator, or None when unavailable.
    """
    return _imported_get_two_layer_orchestrator()


class VetinariA2AExecutor(A2APersistenceMixin, A2ARoutingMixin, A2ADispatchMixin):
    """Routes incoming A2A tasks to Vetinari's internal pipeline.

    Unknown task types receive a graceful failed result. When the internal
    orchestrator is unavailable, recognized tasks are durably acknowledged
    rather than incorrectly reported as completed. Startup recovery retries
    pending, running, and acknowledged tasks and marks repeatedly unexecutable
    acknowledged tasks as A2A-local orphaned rows for auditability.
    """

    def __init__(self, recover_on_init: bool = True) -> None:
        """Initialise the executor, routing table, persistence, and recovery.

        Args:
            recover_on_init: Whether to attempt recovery of interrupted tasks
                from the database on startup.
        """
        self._routing_table: _RoutingTable = self._build_routing_table()
        self._recovery_blocked_reason = ""
        self._init_persistence()

        if recover_on_init:
            self._run_startup_recovery()

        logger.info(
            "VetinariA2AExecutor initialised with %d route entries",
            len(self._routing_table),
        )

    def execute(self, task: A2ATask) -> A2AResult:
        """Execute an A2A task by routing it to the appropriate pipeline agent.

        Args:
            task: The incoming :class:`A2ATask` to execute.

        Returns:
            A result with completed, failed, or acknowledged status.
        """
        logger.info("Executing A2A task id=%s type=%s", task.task_id, task.task_type)
        task.status = STATUS_RUNNING
        self._persist_task(task)

        route = self._route_to_agent(task.task_type)
        if route is None:
            return self._unknown_task_result(task)

        agent_type, mode = route
        logger.debug(
            "Task id=%s routed to agent=%s mode=%s",
            task.task_id,
            agent_type.value,
            mode,
        )

        try:
            output = self._dispatch(agent_type, mode, task)
            if output.get("_is_acknowledgement_only"):
                result = self._acknowledgement_result(task, output)
                if result.status == STATUS_FAILED:
                    return result
            else:
                task.status = STATUS_COMPLETED
                result = A2AResult(
                    task_id=task.task_id,
                    status=STATUS_COMPLETED,
                    output_data=output,
                )
            self._persist_result(task.task_id, result)
            return result
        except Exception as exc:
            logger.exception("A2A task id=%s failed during dispatch: %s", task.task_id, exc)
            return self._dispatch_exception_result(task, exc)

    def _unknown_task_result(self, task: A2ATask) -> A2AResult:
        logger.warning("No route found for A2A task type '%s'", task.task_type)
        task.status = STATUS_FAILED
        result = A2AResult(
            task_id=task.task_id,
            status=STATUS_FAILED,
            error=f"Unknown task type: '{task.task_type}'. Supported types: {sorted(self._routing_table.keys())}",
        )
        self._persist_result(task.task_id, result)
        return result

    def _acknowledgement_result(self, task: A2ATask, output: dict[str, Any]) -> A2AResult:
        if not self._has_db_available():
            task.status = STATUS_FAILED
            result = A2AResult(
                task_id=task.task_id,
                status=STATUS_FAILED,
                output_data={
                    "agent": output.get("agent"),
                    "mode": output.get("mode"),
                    "task_id": task.task_id,
                    "status": STATUS_DEGRADED_UNRECOVERABLE,
                    "_is_acknowledgement_only": True,
                    "requires_recovery": True,
                },
                error=(
                    "Orchestrator unavailable and A2A task persistence unavailable; "
                    "task was not acknowledged because it cannot be recovered."
                ),
            )
            self._persist_result(task.task_id, result)
            logger.error(
                "A2A task id=%s failed closed: orchestrator unavailable and persistence unavailable",
                task.task_id,
            )
            return result
        task.status = STATUS_ACKNOWLEDGED
        return A2AResult(task_id=task.task_id, status=STATUS_ACKNOWLEDGED, output_data=output)

    def _dispatch_exception_result(self, task: A2ATask, exc: Exception) -> A2AResult:
        task.status = STATUS_FAILED
        result = A2AResult(task_id=task.task_id, status=STATUS_FAILED, error=str(exc))
        self._persist_result(task.task_id, result)
        return result

    @property
    def supported_task_types(self) -> list[str]:
        """Return the sorted list of A2A task type strings this executor handles.

        Returns:
            Sorted list of recognised task type strings.
        """
        return sorted(self._routing_table.keys())


__all__ = [
    "STATUS_ACKNOWLEDGED",
    "STATUS_COMPLETED",
    "STATUS_DEGRADED_UNRECOVERABLE",
    "STATUS_FAILED",
    "STATUS_ORPHANED",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "A2AResult",
    "A2ATask",
    "AgentTask",
    "VetinariA2AExecutor",
    "get_two_layer_orchestrator",
]
