"""Async wrapper for non-blocking execution of agent tasks in wave-based plans.

When to use this module:
    ``AsyncExecutor`` provides a coroutine-friendly execution layer for the
    wave-based plan structure produced by legacy ``PlanManager``.  Each wave
    is executed sequentially (preserving inter-wave dependency ordering) while
    tasks *within* a wave run concurrently via ``asyncio.gather``.  Use this
    path when integrating with async I/O frameworks or when you need
    cooperative concurrency without thread-pool overhead.

Pipeline role: Plan (wave structure) → **AsyncExecutor** (concurrent task dispatch)
→ collect SandboxResult per task.
Compare with ``pipeline_agent_graph.py`` (DAG-based, richer dependency model) and
``durable_execution.py`` (checkpoint persistence for crash recovery).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from vetinari.types import AgentType, StatusEnum

logger = logging.getLogger(__name__)


_DEFAULT_TASK_TIMEOUT = 300  # seconds


class AsyncTaskContractError(ValueError):
    """Raised when an async task descriptor is missing required routing data."""


def _require_task_agent_type(task: dict[str, Any]) -> str:
    """Return the explicit agent type for a task or fail closed."""
    agent_type = task.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type.strip():
        task_id = task.get("id", "unknown")
        raise AsyncTaskContractError(f"Task {task_id!r} is missing required agent_type")
    return agent_type.strip()


class AsyncExecutor:
    """Execute agent tasks asynchronously with wave-level concurrency.

    Within a plan the executor processes *waves* sequentially (dependency
    ordering is preserved between waves) while running all tasks inside a
    single wave concurrently via :func:`asyncio.gather`.

    Args:
        task_timeout: Per-task timeout in seconds.  Defaults to 300 s.
        fail_fast: When ``True`` the first task failure aborts the entire
            wave execution.  When ``False`` (default) remaining tasks
            continue running and failed results are collected.

    Example::

        executor = AsyncExecutor(task_timeout=60)
        results = asyncio.run(executor.execute_plan(plan))
    """

    def __init__(
        self,
        task_timeout: int = _DEFAULT_TASK_TIMEOUT,
        fail_fast: bool = False,
    ) -> None:
        self.task_timeout = task_timeout
        self.fail_fast = fail_fast

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute_task(self, task: dict[str, Any], agent_type: str) -> dict[str, Any]:
        """Execute a single agent task.

        Dispatches to the named agent type. Concrete deployments must override
        or wrap this method to call the real agent.

        Args:
            task: Task descriptor dict.  Expected keys: ``id``, ``prompt``,
                and any additional metadata required by the agent.
            agent_type: Name of the agent type responsible for this task
                (e.g. ``"WORKER"``).

        Returns:
            Result dict with keys ``task_id``, ``agent_type``, ``status``
            (``"completed"`` or ``"failed"``), ``output``, and optionally
            ``error``.
        """
        task_id = task.get("id", "unknown")
        logger.debug("Executing task %s via agent %s", task_id, agent_type)

        try:
            result = await asyncio.wait_for(
                self._dispatch(task, agent_type),
                timeout=self.task_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.error(
                "Task %s timed out after %s s (agent=%s)",
                task_id,
                self.task_timeout,
                agent_type,
            )
            return {
                "task_id": task_id,
                "agent_type": agent_type,
                "status": StatusEnum.FAILED.value,
                "output": None,
                "error": f"Timeout after {self.task_timeout}s",
            }
        except Exception as exc:
            logger.error("Task %s raised an exception: %s", task_id, exc, exc_info=True)
            return {
                "task_id": task_id,
                "agent_type": agent_type,
                "status": StatusEnum.FAILED.value,
                "output": None,
                "error": str(exc),
            }

        return result

    async def execute_wave(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Execute all tasks in a wave concurrently.

        All tasks are dispatched simultaneously via :func:`asyncio.gather`.
        If ``fail_fast`` is ``True`` the gather call propagates the first
        exception; otherwise results (including failures) are returned for
        every task.

        Args:
            tasks: List of task descriptor dicts.  Each must contain at least
                ``id`` and ``agent_type`` keys.

        Returns:
            List of result dicts in the same order as *tasks*.

        Raises:
            AsyncTaskContractError: If ``fail_fast`` is enabled and a task is
                missing its required agent type.
        """
        if not tasks:
            return []

        try:
            coroutines = [self.execute_task(task, _require_task_agent_type(task)) for task in tasks]
        except AsyncTaskContractError as exc:
            logger.warning("Wave task contract rejected: %s", exc)
            if self.fail_fast:
                raise
            return [
                {
                    "task_id": task.get("id", "unknown"),
                    "agent_type": task.get("agent_type"),
                    "status": StatusEnum.FAILED.value,
                    "output": None,
                    "error": "missing required agent_type",
                }
                for task in tasks
            ]

        if self.fail_fast:
            results: list[dict[str, Any]] = await asyncio.gather(*coroutines)
        else:
            raw = await asyncio.gather(*coroutines, return_exceptions=True)
            results = []
            for task, outcome in zip(tasks, raw):
                if isinstance(outcome, BaseException):
                    logger.error(
                        "Unhandled exception in task %s: %s",
                        task.get("id", "unknown"),
                        outcome,
                    )
                    results.append(
                        {
                            "task_id": task.get("id", "unknown"),
                            "agent_type": task.get("agent_type", AgentType.FOREMAN.value),
                            "status": StatusEnum.FAILED.value,
                            "output": None,
                            "error": str(outcome),
                        },
                    )
                else:
                    if isinstance(outcome, dict):
                        results.append(outcome)
                    else:
                        results.append(
                            {
                                "task_id": task.get("id", "unknown"),
                                "agent_type": task.get("agent_type", AgentType.FOREMAN.value),
                                "status": StatusEnum.FAILED.value,
                                "output": None,
                                "error": f"Unexpected async task result type: {type(outcome).__name__}",
                            },
                        )

        return results

    async def execute_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Execute a plan wave-by-wave.

        Waves are executed **sequentially** (preserving dependency order).
        Tasks inside each wave are executed **concurrently**.

        The plan dict must have a ``waves`` key whose value is a list of
        wave dicts, each containing a ``tasks`` key with a list of task
        descriptors.

        Args:
            plan: Plan descriptor dict.  Expected structure::

                {
                    "id": "plan-1",
                    "waves": [
                        {"wave_index": 0, "tasks": [...]},
                        {"wave_index": 1, "tasks": [...]},
                    ],
                }

        Returns:
            Result dict with keys ``plan_id``, ``status`` (``"completed"``
            or ``"failed"``), ``wave_results`` (list of wave result lists),
            and ``total_tasks``.
        """
        plan_id = plan.get("id", "unknown")
        waves: list[dict[str, Any]] = plan.get("waves", [])

        if not waves:
            logger.warning("Plan %s has no waves to execute", plan_id)
            return {
                "plan_id": plan_id,
                "status": StatusEnum.SKIPPED.value,
                "wave_results": [],
                "total_tasks": 0,
            }

        wave_results: list[list[dict[str, Any]]] = []
        overall_status = StatusEnum.COMPLETED.value

        for wave_idx, wave in enumerate(waves):
            tasks: list[dict[str, Any]] = wave.get("tasks", [])
            logger.info(
                "Plan %s: executing wave %d with %d task(s)",
                plan_id,
                wave_idx,
                len(tasks),
            )
            wave_result = await self.execute_wave(tasks)
            wave_results.append(wave_result)

            failed = [r for r in wave_result if r.get("status") == StatusEnum.FAILED.value]
            if failed:
                logger.warning(
                    "Plan %s wave %d: %d/%d tasks failed",
                    plan_id,
                    wave_idx,
                    len(failed),
                    len(tasks),
                )
                if self.fail_fast:
                    overall_status = StatusEnum.FAILED.value
                    break

        any_failed = any(r.get("status") == StatusEnum.FAILED.value for wave in wave_results for r in wave)
        if any_failed and overall_status != StatusEnum.FAILED.value:
            overall_status = StatusEnum.FAILED.value

        total_tasks = sum(len(w) for w in wave_results)
        return {
            "plan_id": plan_id,
            "status": overall_status,
            "wave_results": wave_results,
            "total_tasks": total_tasks,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _dispatch(self, task: dict[str, Any], agent_type: str) -> dict[str, Any]:
        """Dispatch a task to the named agent type.

        This is the extension point for concrete integrations. The base
        executor is instantiable so orchestration can wire it and tests can
        monkeypatch a dispatcher, but executing without a concrete dispatcher
        fails closed instead of returning synthetic success.

        Args:
            task: Task descriptor dict.
            agent_type: Agent type name.

        Concrete executors must return the task result payload.
        """
        raise RuntimeError("AsyncExecutor requires a concrete agent integration before dispatch")
