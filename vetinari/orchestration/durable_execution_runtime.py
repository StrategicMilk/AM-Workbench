"""Task runtime methods for durable execution."""

from __future__ import annotations

import concurrent.futures
import contextlib
import logging
import secrets
import time
from collections.abc import Callable
from datetime import datetime, timezone
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from vetinari.concurrency import submit_with_context
from vetinari.events import clarification_requested, get_event_bus
from vetinari.exceptions import ClarificationNeeded
from vetinari.orchestration import clarification
from vetinari.orchestration.execution_graph import ExecutionGraph, ExecutionTaskNode
from vetinari.structured_logging import CorrelationContext, get_plan_id
from vetinari.types import PlanStatus, StatusEnum

logger = logging.getLogger(__name__)
_JITTER_RANDOM = secrets.SystemRandom()


@lru_cache(maxsize=1)
def _get_plan_manager_factory() -> Callable[[], Any]:
    """Resolve the legacy plan-manager factory when task status sync needs it."""
    from vetinari.planning import get_plan_manager

    return get_plan_manager


class _DurableExecutionRuntimeMixin:
    """Run durable execution graphs and individual tasks."""

    if TYPE_CHECKING:
        _active_executions: Any
        _circuit_breaker: Any
        _circuit_breaker_degraded: Any
        _cycle_detector: Any
        _dispatch_step_via_adapter: Any
        _emit_event: Any
        _execution_lock: Any
        _executor: Any
        _handle_layer_failure: Any
        _human_checkpoint: Any
        _on_task_complete: Any
        _on_task_fail: Any
        _on_task_start: Any
        _record_learning: Any
        _save_checkpoint: Any
        _task_handlers: Any
        _workflow_adapter: Any
        create_execution: Any
        record_heartbeat: Any

    @staticmethod
    def _bind_plan_correlation(plan_id: str) -> None:
        """Attach the executing plan ID to structured logging context."""
        try:
            ctx_plan_id = get_plan_id()
            if not ctx_plan_id:
                CorrelationContext.set_plan_id(plan_id)
            elif ctx_plan_id != plan_id:
                logger.debug(
                    "CorrelationContext plan_id=%s differs from execution plan_id=%s - using execution plan_id",
                    ctx_plan_id,
                    plan_id,
                )
        except Exception:
            logger.warning("CorrelationContext plan_id annotation unavailable for plan %s - skipping", plan_id)

    @staticmethod
    def _initial_plan_results(graph: ExecutionGraph) -> dict[str, Any]:
        """Build the mutable result accumulator for one plan run."""
        return {
            "plan_id": graph.plan_id,
            "total_tasks": len(graph.nodes),
            StatusEnum.COMPLETED.value: 0,
            StatusEnum.FAILED.value: 0,
            StatusEnum.PAUSED.value: 0,
            "task_results": {},
        }

    @staticmethod
    def _record_layer_results(results: dict[str, Any], layer_results: dict[str, Any]) -> None:
        """Merge one layer's task results into the plan result accumulator."""
        for task_id, result in layer_results.items():
            results["task_results"][task_id] = result
            status = result.get("status")
            if status == StatusEnum.COMPLETED.value:
                results[StatusEnum.COMPLETED.value] += 1
            elif status == StatusEnum.PAUSED.value or status == StatusEnum.WAITING.value:
                results[StatusEnum.PAUSED.value] += 1
            else:
                results[StatusEnum.FAILED.value] += 1

    def _handle_layer_terminal_state(self, graph: ExecutionGraph, layer: list[ExecutionTaskNode]) -> bool:
        """Handle paused/failed tasks after a layer and return True when execution should stop."""
        paused_tasks = [task for task in layer if task.status in {StatusEnum.PAUSED, StatusEnum.WAITING}]
        if paused_tasks:
            graph.status = PlanStatus.PAUSED
            self._save_checkpoint(graph.plan_id, graph)
            return True
        failed_tasks = [task for task in layer if task.status == StatusEnum.FAILED]
        if failed_tasks:
            self._handle_layer_failure(graph, failed_tasks)
        return False

    def _cleanup_plan_execution(self, plan_id: str) -> None:
        """Remove a plan from active execution state and release clarification locks."""
        with self._execution_lock:
            self._active_executions.pop(plan_id, None)
        clarification.release_execution_lock(plan_id)

    def execute_plan(
        self,
        graph: ExecutionGraph,
        task_handler: Callable | None = None,
    ) -> dict[str, Any]:
        """Execute a plan with durable semantics, layer by layer.

        Args:
            graph: Graph value consumed by execute_plan().
            task_handler: Task handler value consumed by execute_plan().

        Returns:
            Value produced for the caller.
        """
        plan_id = graph.plan_id
        graph.status = PlanStatus.EXECUTING
        self._bind_plan_correlation(plan_id)
        if task_handler:
            self._task_handlers["default"] = task_handler

        self.create_execution(graph)
        results = self._initial_plan_results(graph)
        try:
            layers = graph.get_execution_order()
            for layer_idx, layer in enumerate(layers):
                logger.info("Executing layer %s/%s with %s tasks", layer_idx + 1, len(layers), len(layer))
                self._record_layer_results(results, self._execute_layer(graph, layer))
                if self._handle_layer_terminal_state(graph, layer):
                    break
            if graph.status is not PlanStatus.PAUSED:
                graph.status = PlanStatus.COMPLETED if results[StatusEnum.FAILED.value] == 0 else PlanStatus.FAILED
            self._save_checkpoint(plan_id, graph)
        finally:
            self._cleanup_plan_execution(plan_id)
        return results

    def _execute_layer(self, graph: ExecutionGraph, layer: list[ExecutionTaskNode]) -> dict[str, Any]:
        """Execute a layer of tasks in parallel using the shared thread pool."""
        results: dict[str, Any] = {}
        future_to_task = {
            submit_with_context(self._executor, self._execute_task, graph, task, require_correlation=False): task
            for task in layer
        }
        for future in concurrent.futures.as_completed(future_to_task):
            task = future_to_task[future]
            try:
                results[task.id] = future.result()
            except Exception as exc:
                logger.error("Task %s failed with exception: %s", task.id, exc)
                results[task.id] = {"status": StatusEnum.FAILED.value, "error": str(exc)}
        return results

    def shutdown(self, *, wait: bool = True) -> None:
        """Shut down the shared thread pool."""
        self._executor.shutdown(wait=wait)

    def __del__(self) -> None:
        """Release the thread pool on garbage collection as a safety net."""
        with contextlib.suppress(Exception):
            self._executor.shutdown(wait=False)  # noqa: leak-rule-3 -- GC finalizer cannot block on thread join during interpreter shutdown; the explicit shutdown() method above is the real cleanup path.

    def _preflight_task_execution(self, task: ExecutionTaskNode) -> dict[str, Any] | None:
        """Handle cancellation, cycle detection, and human checkpoints before task execution."""
        task_id = task.id
        if task.status == StatusEnum.CANCELLED:
            logger.info("Task %s is CANCELLED - skipping execution", task_id)
            return {"status": StatusEnum.CANCELLED.value, "reason": "dependency_failed"}
        prior_count = self._cycle_detector.get_count(task_id)
        if prior_count > 0:
            logger.debug("Task %s has been attempted %d time(s)", task_id, prior_count)
        try:
            self._cycle_detector.record_execution(task_id)
        except RuntimeError as cycle_err:
            logger.error("Cycle detected for task %s: %s", task_id, cycle_err)
            task.status = StatusEnum.FAILED
            task.error = str(cycle_err)
            task.completed_at = datetime.now(timezone.utc).isoformat()
            self._emit_event("task_failed", task_id, {"error": str(cycle_err), "reason": "cycle_detected"})
            return {"status": StatusEnum.FAILED.value, "error": str(cycle_err)}
        if self._human_checkpoint.is_checkpoint(task_id) and not self._human_checkpoint.is_approved(task_id):
            logger.info("Task %s is a human checkpoint and has not been approved", task_id)
            task.status = StatusEnum.WAITING
            return {"status": StatusEnum.WAITING.value, "waiting_for": "human_approval", "task_id": task_id}
        return None

    def _start_task_execution(self, task: ExecutionTaskNode) -> None:
        """Mark a task running and run the optional start callback."""
        self._emit_event("task_started", task.id, {"description": task.description})
        task.status = StatusEnum.RUNNING
        task.started_at = datetime.now(timezone.utc).isoformat()
        if self._on_task_start:
            try:
                self._on_task_start(task)
            except Exception as exc:
                logger.warning("Task start callback failed: %s", exc)

    def _fail_missing_handler(self, task: ExecutionTaskNode) -> dict[str, Any]:
        """Fail closed when no handler is registered for a task type."""
        task.status = StatusEnum.FAILED
        task.error = "No handler registered for task type"
        task.completed_at = datetime.now(timezone.utc).isoformat()
        self._emit_event("task_failed", task.id, {"status": StatusEnum.FAILED.value, "error": task.error})
        return {"status": StatusEnum.FAILED.value, "error": task.error}

    @staticmethod
    def _sleep_retry_backoff(attempt: int) -> None:
        """Sleep with exponential backoff and jitter."""
        base_delay = 2**attempt
        time.sleep(base_delay + _JITTER_RANDOM.uniform(0, 1) * base_delay)

    @staticmethod
    def _has_useful_output(output: Any) -> bool:
        """Return whether handler output is useful enough to count as completion."""
        structured_failure = isinstance(output, dict) and output.get("success") is False
        return bool(output) and output != {"output": ""} and not structured_failure

    def _handle_empty_output(self, task: ExecutionTaskNode, attempt: int, max_attempts: int) -> str:
        """Record one empty-output failed attempt and return its error string."""
        task.status = StatusEnum.FAILED
        task.error = "Task produced empty output"
        task.completed_at = datetime.now(timezone.utc).isoformat()
        logger.warning("Task %s produced empty output - marking as FAILED", task.id)
        self._emit_event("task_failed", task.id, {"status": StatusEnum.FAILED.value, "error": task.error})
        if self._circuit_breaker is not None:
            self._circuit_breaker.record_failure()
        if attempt < max_attempts - 1:
            self._sleep_retry_backoff(attempt)
        return task.error

    @staticmethod
    def _extract_tokens(output: Any) -> int:
        """Extract token usage from handler output."""
        if not isinstance(output, dict):
            return 0
        tokens = output.get("tokens_used", 0)
        if not tokens and "metadata" in output:
            tokens = output["metadata"].get("tokens_used", 0)
        return tokens

    def _handle_successful_output(
        self,
        graph: ExecutionGraph,
        task: ExecutionTaskNode,
        output: Any,
        attempt: int,
    ) -> dict[str, Any]:
        """Persist successful task output and return the task result dict."""
        task.status = StatusEnum.COMPLETED
        task.completed_at = datetime.now(timezone.utc).isoformat()
        task.output_data = output if isinstance(output, dict) else {"output": output}
        self._emit_event("task_completed", task.id, {"status": StatusEnum.COMPLETED.value, "attempts": attempt + 1})
        self._record_learning(task, task.id, output)
        if self._on_task_complete:
            try:
                self._on_task_complete(task)
            except Exception as exc:
                logger.warning("Task complete callback failed: %s", exc)
        self._update_plan_manager_success(graph, task)
        self._save_checkpoint(graph.plan_id, graph)
        result = {
            "status": StatusEnum.COMPLETED.value,
            "output": task.output_data,
            "tokens_used": self._extract_tokens(output),
            "metadata": task.output_data.get("metadata", {}) if isinstance(task.output_data, dict) else {},
        }
        if self._circuit_breaker_degraded:
            result["_circuit_breaker_degraded"] = True
        return result

    @staticmethod
    def _update_plan_manager_success(graph: ExecutionGraph, task: ExecutionTaskNode) -> None:
        """Synchronize a completed task into the legacy PlanManager."""
        wave_id = task.input_data.get("wave_id", "") if task.input_data else ""
        try:
            _get_plan_manager_factory()().update_task_status(
                plan_id=graph.plan_id,
                wave_id=wave_id,
                task_id=task.id,
                status=StatusEnum.COMPLETED.value,
                result=task.output_data,
            )
        except Exception as exc:
            logger.warning("PlanManager task status update skipped for %s - plan not tracked: %s", task.id, exc)

    def _handle_clarification(
        self, graph: ExecutionGraph, task: ExecutionTaskNode, exc: ClarificationNeeded
    ) -> dict[str, Any]:
        """Pause a task for clarification and publish the durable pause event."""
        paused_at = datetime.now(timezone.utc).isoformat()
        task.status = StatusEnum.PAUSED
        task.error = ""
        task.completed_at = None
        question_id = clarification.pause_task_for_clarification(self, graph.plan_id, task.id, exc)
        payload = {
            "status": StatusEnum.PAUSED.value,
            "question_id": question_id,
            "questions": exc.questions,
            "paused_at": paused_at,
        }
        self._emit_event("task_paused", task.id, payload, graph.plan_id)
        try:
            get_event_bus().publish(
                clarification_requested(
                    execution_id=graph.plan_id,
                    task_id=task.id,
                    question_id=question_id,
                    questions=exc.questions,
                    paused_at=paused_at,
                )
            )
        except Exception:
            logger.warning(
                "clarification event publish failed for execution=%s task=%s", graph.plan_id, task.id, exc_info=True
            )
        self._save_checkpoint(graph.plan_id, graph)
        return {
            "status": StatusEnum.PAUSED.value,
            "task_id": task.id,
            "question_id": question_id,
            "questions": exc.questions,
            "paused_at": paused_at,
        }

    def _handle_attempt_exception(
        self, task: ExecutionTaskNode, attempt: int, max_attempts: int, exc: Exception
    ) -> str:
        """Record one failed task attempt and return the latest error."""
        last_error = str(exc)
        task.retry_count = attempt + 1
        logger.warning("Task %s attempt %s failed: %s", task.id, attempt + 1, exc)
        if self._circuit_breaker is not None:
            self._circuit_breaker.record_failure()
        if attempt < max_attempts - 1:
            self._sleep_retry_backoff(attempt)
        return last_error

    def _handle_exhausted_task(
        self,
        graph: ExecutionGraph,
        task: ExecutionTaskNode,
        last_error: str | None,
        max_attempts: int,
    ) -> dict[str, Any]:
        """Fail a task after all retry attempts are exhausted."""
        task.status = StatusEnum.FAILED
        task.error = last_error
        task.completed_at = datetime.now(timezone.utc).isoformat()
        self._emit_event(
            "task_failed",
            task.id,
            {"status": StatusEnum.FAILED.value, "error": last_error, "attempts": max_attempts},
        )
        if self._on_task_fail:
            try:
                self._on_task_fail(task)
            except Exception as exc:
                logger.warning("Task fail callback failed: %s", exc)
        self._update_plan_manager_failure(graph, task, last_error)
        self._save_checkpoint(graph.plan_id, graph)
        result = {"status": StatusEnum.FAILED.value, "error": last_error}
        if self._circuit_breaker_degraded:
            result["_circuit_breaker_degraded"] = True
        return result

    @staticmethod
    def _update_plan_manager_failure(
        graph: ExecutionGraph,
        task: ExecutionTaskNode,
        last_error: str | None,
    ) -> None:
        """Synchronize a failed task into the legacy PlanManager."""
        wave_id = task.input_data.get("wave_id", "") if task.input_data else ""
        try:
            _get_plan_manager_factory()().update_task_status(
                plan_id=graph.plan_id,
                wave_id=wave_id,
                task_id=task.id,
                status=StatusEnum.FAILED.value,
                error=last_error or "unknown error",
            )
        except Exception as exc:
            logger.warning(
                "PlanManager task status update (failed) skipped for %s - plan not tracked: %s", task.id, exc
            )

    def _execute_task(self, graph: ExecutionGraph, task: ExecutionTaskNode) -> dict[str, Any]:
        """Execute a single task with retry logic and cycle detection."""
        preflight_result = self._preflight_task_execution(task)
        if preflight_result is not None:
            return preflight_result
        self._start_task_execution(task)
        adapter = self._workflow_adapter
        if adapter is not None:
            return self._dispatch_step_via_adapter(graph, task, prior_attempts=task.retry_count, adapter=adapter)

        handler = self._task_handlers.get(task.task_type) or self._task_handlers.get("default")
        if not handler:
            return self._fail_missing_handler(task)

        max_attempts = task.max_retries + 1
        last_error = None
        for attempt in range(max_attempts):
            self.record_heartbeat(task.id)
            if self._circuit_breaker is not None and not self._circuit_breaker.allow_request():
                logger.warning("Circuit breaker OPEN for task %s - skipping attempt %d", task.id, attempt + 1)
                last_error = "Circuit breaker open - too many recent failures"
                break
            try:
                output = handler(task)
                if self._circuit_breaker is not None:
                    self._circuit_breaker.record_success()
                if not self._has_useful_output(output):
                    last_error = self._handle_empty_output(task, attempt, max_attempts)
                    continue
                return self._handle_successful_output(graph, task, output, attempt)
            except ClarificationNeeded as exc:
                logger.warning("Exception handled by  execute task fallback", exc_info=True)
                return self._handle_clarification(graph, task, exc)
            except Exception as exc:
                last_error = self._handle_attempt_exception(task, attempt, max_attempts, exc)

        return self._handle_exhausted_task(graph, task, last_error, max_attempts)
