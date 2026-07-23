"""Parallel and async layer execution helpers for graph execution."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import AgentResult, Plan
from vetinari.concurrency import run_in_executor_with_context, submit_with_context
from vetinari.orchestration.graph_executor_layers import _lazy_get_vram_manager
from vetinari.orchestration.graph_types import ExecutionDAG
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


class _GraphExecutionParallelMixin:
    """Parallel layer execution behavior for ``GraphExecutionEngine``."""

    if TYPE_CHECKING:
        _clear_cached_execution_layers: Any
        _drain_runtime_diff_queue: Any
        _emit_plan_runtime_edit_receipt: Any
        _emit_scaffold_layer_receipt_if_needed: Any
        _execute_task_node: Any
        _add_post_execution_results: Any
        _handle_failed_scaffolds: Any
        _has_unfinished_nodes: Any
        _max_workers: Any
        _next_dispatchable_layer: Any
        _utc_now_iso: Any
        create_execution_plan: Any

    def _execute_single_layer_task(
        self,
        task_id: str,
        exec_plan: ExecutionDAG,
        prior_results: dict[str, AgentResult],
    ) -> dict[str, AgentResult]:
        node = exec_plan.nodes[task_id]
        result = self._execute_task_node(node, prior_results)
        node.status = StatusEnum.COMPLETED if result.success else StatusEnum.FAILED
        return {task_id: result}

    @staticmethod
    def _finish_parallel_future(
        future: Future[AgentResult],
        task_id: str,
        exec_plan: ExecutionDAG,
    ) -> AgentResult:
        node = exec_plan.nodes[task_id]
        try:
            result = future.result()
        except Exception as exc:
            result = AgentResult(success=False, output=None, errors=[str(exc)])
        node.status = StatusEnum.COMPLETED if result.success else StatusEnum.FAILED
        return result

    async def _execute_layer_async(
        self,
        layer: list[str],
        exec_plan: ExecutionDAG,
        results_snapshot: dict[str, AgentResult],
        loop: asyncio.AbstractEventLoop,
        executor: ThreadPoolExecutor,
    ) -> dict[str, AgentResult]:
        """Execute one async layer while respecting the throttled worker budget.

        Args:
            layer: Task IDs ready to run in this layer.
            exec_plan: The execution plan containing the task nodes.
            results_snapshot: Stable prior-layer results for worker reads.
            loop: Event loop that schedules executor work.
            executor: Reusable executor owned by ``execute_plan_async``.

        Returns:
            Mapping of task ID to AgentResult for this layer's tasks.
        """
        workers = max(1, min(self._max_workers, len(layer)))
        workers = self._vram_throttle_workers(workers, layer, exec_plan)
        layer_results: dict[str, AgentResult] = {}
        future_map: dict[asyncio.Future[AgentResult], str] = {}
        next_index = 0

        def submit_next() -> None:
            nonlocal next_index
            if next_index >= len(layer):
                return
            task_id = layer[next_index]
            next_index += 1
            future = run_in_executor_with_context(
                loop,
                executor,
                self._execute_task_node,
                exec_plan.nodes[task_id],
                results_snapshot,
                require_correlation=False,
            )
            future_map[future] = task_id

        for _ in range(workers):
            submit_next()

        while future_map:
            done, _ = await asyncio.wait(future_map, return_when=asyncio.FIRST_COMPLETED)
            for future in done:
                task_id = future_map.pop(future)
                node = exec_plan.nodes[task_id]
                try:
                    result = future.result()
                except Exception as exc:
                    result = AgentResult(success=False, output=None, errors=[str(exc)])
                node.status = StatusEnum.COMPLETED if result.success else StatusEnum.FAILED
                layer_results[task_id] = result
                submit_next()

        return layer_results

    def _execute_layer_parallel(
        self,
        layer: list[str],
        exec_plan: ExecutionDAG,
        prior_results: dict[str, AgentResult],
        *,
        executor: ThreadPoolExecutor | None = None,
    ) -> dict[str, AgentResult]:
        """Execute a batch of independent tasks in parallel via thread pool.

        For single-task layers, skips the thread pool overhead entirely.
        Worker count is capped by VRAM availability - if there isn't enough
        free + evictable VRAM for all tasks, parallelism is reduced to avoid
        cascading model evictions. Decision: ADR-0087.

        Args:
            layer: List of task IDs to execute in this layer.
            exec_plan: The execution plan containing the nodes.
            prior_results: Results from all previously completed tasks.
            executor: Reusable pool supplied by plan execution. When omitted,
                this method owns a temporary pool for direct helper calls.

        Returns:
            Mapping of task ID to AgentResult for this layer's tasks.
        """
        if len(layer) == 1:
            return self._execute_single_layer_task(layer[0], exec_plan, prior_results)

        layer_results: dict[str, AgentResult] = {}
        workers = max(1, min(self._max_workers, len(layer)))
        workers = self._vram_throttle_workers(workers, layer, exec_plan)
        results_snapshot = dict(prior_results) if prior_results else {}

        owned_executor: ThreadPoolExecutor | None = None
        pool = executor
        if pool is None:
            owned_executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="graph-executor",
            )
            pool = owned_executor
        assert pool is not None

        try:
            future_map: dict[Future[AgentResult], str] = {}
            pending: set[Future[AgentResult]] = set()
            next_index = 0

            def submit_next() -> None:
                nonlocal next_index
                if next_index >= len(layer):
                    return
                task_id = layer[next_index]
                next_index += 1
                future = submit_with_context(
                    pool,
                    self._execute_task_node,
                    exec_plan.nodes[task_id],
                    results_snapshot,
                    require_correlation=False,
                )
                future_map[future] = task_id
                pending.add(future)

            for _ in range(workers):
                submit_next()

            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    task_id = future_map.pop(future)
                    result = self._finish_parallel_future(future, task_id, exec_plan)
                    layer_results[task_id] = result
                    submit_next()
        finally:
            if owned_executor is not None:
                owned_executor.shutdown(wait=True, cancel_futures=False)

        return layer_results

    @staticmethod
    def _vram_throttle_workers(
        max_workers: int,
        layer: list[str],
        exec_plan: ExecutionDAG,
    ) -> int:
        """Reduce worker count if VRAM cannot support full parallelism.

        Estimates per-task VRAM from the average loaded model size and checks
        how many concurrent tasks fit in available + evictable VRAM. Never
        reduces below 1.

        Args:
            max_workers: Upper bound on workers from config / layer size.
            layer: Task IDs in this layer.
            exec_plan: The execution plan used to look up agent types.

        Returns:
            Adjusted worker count.
        """
        try:
            mgr = _lazy_get_vram_manager()
            available_gb = mgr.get_max_available_vram_gb()

            loaded = list(mgr._estimates.values())
            if not loaded:
                return max_workers
            avg_model_gb = sum(e.total_gpu_gb for e in loaded) / len(loaded)
            if avg_model_gb <= 0:
                return max_workers

            sharing_discount = 0.7
            fits = max(1, int(available_gb / (avg_model_gb * sharing_discount)))
            throttled = min(max_workers, fits)

            if throttled < max_workers:
                logger.info(
                    "VRAM throttle: layer has %d tasks but only %.1f GB available "
                    "(avg model %.1f GB) - limiting to %d concurrent workers",
                    len(layer),
                    available_gb,
                    avg_model_gb,
                    throttled,
                )

            return throttled

        except Exception:
            logger.warning(
                "VRAMManager unavailable for layer throttling - using default worker count of %d",
                max_workers,
            )
            return max_workers

    async def execute_plan_async(self, plan: Plan) -> dict[str, AgentResult]:
        """Execute a plan asynchronously, running parallel layers via asyncio.

        Args:
            plan: The Plan to execute.

        Returns:
            Mapping of task ID to AgentResult for every task that completed
            before any exception, mirroring the structure of ``execute_plan``.

        Raises:
            Exception: Re-raises any exception that occurs during layer execution
                after marking the plan as failed.
        """
        exec_plan = self.create_execution_plan(plan)
        exec_plan.status = StatusEnum.RUNNING
        exec_plan.started_at = self._utc_now_iso()

        results: dict[str, AgentResult] = {}
        loop = asyncio.get_running_loop()
        parallel_pool = ThreadPoolExecutor(
            max_workers=max(1, self._max_workers),
            thread_name_prefix="graph-executor-async",
        )

        try:
            while True:
                dispatchable_layer = self._next_dispatchable_layer(exec_plan)
                if not dispatchable_layer:
                    break
                self._emit_scaffold_layer_receipt_if_needed(exec_plan, dispatchable_layer)
                results_snapshot = dict(results)
                layer_results = await self._execute_layer_async(
                    dispatchable_layer,
                    exec_plan,
                    results_snapshot,
                    loop,
                    parallel_pool,
                )
                results.update(layer_results)
                drained = self._drain_runtime_diff_queue(exec_plan)
                for diff, before_state, after_state in drained:
                    self._emit_plan_runtime_edit_receipt(exec_plan, diff, before_state, after_state)
                if drained:
                    self._clear_cached_execution_layers(exec_plan)
                self._handle_failed_scaffolds(dispatchable_layer, exec_plan, layer_results)

            self._add_post_execution_results(plan, results)
            any_failed = any(node.status == StatusEnum.FAILED for node in exec_plan.nodes.values())
            exec_plan.status = (
                StatusEnum.FAILED if any_failed or self._has_unfinished_nodes(exec_plan) else StatusEnum.COMPLETED
            )

        except Exception as exc:
            logger.error("[AgentGraph] Async plan execution failed: %s", exc)
            exec_plan.status = StatusEnum.FAILED
            raise
        finally:
            parallel_pool.shutdown(wait=True, cancel_futures=False)
            exec_plan.completed_at = self._utc_now_iso()

        return results
