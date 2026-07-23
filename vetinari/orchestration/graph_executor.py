"""Plan and layer execution orchestration for the AgentGraph."""

from __future__ import annotations

import inspect
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from vetinari.orchestration.graph_executor_layers import (
    _get_goal_tracker_class,
    _get_milestone_action_class,
    _get_milestone_manager_class,
    _GraphExecutionLayerRunner,
)
from vetinari.orchestration.graph_executor_parallel import _GraphExecutionParallelMixin
from vetinari.orchestration.graph_types import ExecutionStrategy
from vetinari.types import AgentType, StatusEnum

if TYPE_CHECKING:
    from vetinari.agents.contracts import AgentResult, AgentTask, Plan

logger = logging.getLogger(__name__)

_AGENT_TASK_CLASS = None
_PLAN_CLASS = None


def _get_agent_task_class():
    """Return the AgentTask class through a cached lazy import."""
    global _AGENT_TASK_CLASS
    if _AGENT_TASK_CLASS is None:
        from vetinari.agents.contracts import AgentTask

        _AGENT_TASK_CLASS = AgentTask
    return _AGENT_TASK_CLASS


def _get_plan_class():
    """Return the Plan class through a cached lazy import."""
    global _PLAN_CLASS
    if _PLAN_CLASS is None:
        from vetinari.agents.contracts import Plan

        _PLAN_CLASS = Plan
    return _PLAN_CLASS


class GraphExecutionEngine(_GraphExecutionParallelMixin, _GraphExecutionLayerRunner):
    """Plan, layer, and subgraph execution for AgentGraph."""

    @staticmethod
    def _utc_now_iso() -> str:
        """Return the current UTC timestamp in ISO-8601 format."""
        return datetime.now(timezone.utc).isoformat()

    def _initialize_execution(self, plan: Plan):
        """Create runtime execution state and optional goal/milestone helpers."""
        exec_plan = self.create_execution_plan(plan)
        exec_plan.status = StatusEnum.RUNNING
        exec_plan.started_at = self._utc_now_iso()
        self._initialize_goal_tracker(plan)
        self._initialize_milestone_manager()
        return exec_plan

    def _initialize_goal_tracker(self, plan: Plan) -> None:
        """Initialize and run non-blocking goal drift checks."""
        goal_text = getattr(plan, "goal", "")
        if not goal_text:
            return
        try:
            self._goal_tracker = _get_goal_tracker_class()(goal_text)
        except Exception:
            self._goal_tracker = None
        if self._goal_tracker is None or not plan.tasks:
            return
        try:
            creep_items = self._goal_tracker.detect_scope_creep(plan.tasks)
            if creep_items:
                logger.warning(
                    "[AgentGraph] Scope creep detected in plan %s: %d task(s) may be outside the goal",
                    plan.plan_id,
                    len(creep_items),
                )
        except Exception:
            logger.warning("[AgentGraph] Scope-creep scan failed for plan %s", plan.plan_id)

    def _initialize_milestone_manager(self) -> None:
        """Initialize optional milestone approval manager."""
        try:
            self._milestone_manager = _get_milestone_manager_class()()
            approval_cb = getattr(self, "_milestone_approval_callback", None)
            if approval_cb is not None:
                self._milestone_manager.set_approval_callback(approval_cb)
        except Exception:
            self._milestone_manager = None

    def _execute_sequential_plan(self, exec_plan, results: dict[str, AgentResult]) -> None:
        """Execute plan nodes sequentially, including mid-execution replanning."""
        remaining_order = list(exec_plan.execution_order)
        idx = 0
        while idx < len(remaining_order):
            task_id = remaining_order[idx]
            node = exec_plan.nodes.get(task_id)
            if node is None or node.status == StatusEnum.COMPLETED:
                idx += 1
                continue
            result = self._execute_task_node(node, results)
            results[task_id] = result
            node.status = StatusEnum.COMPLETED if result.success else StatusEnum.FAILED
            new_order = self._maybe_replan_after_node(exec_plan, node, result, remaining_order, idx)
            if new_order is not None:
                remaining_order = new_order
                idx = 0
                continue
            idx += 1

    def _maybe_replan_after_node(
        self, exec_plan, node, result, remaining_order: list[str], idx: int
    ) -> list[str] | None:
        """Trigger and apply a replan when a completed node requests it."""
        if not (result.success and node.task and self._should_replan(node.task, result)):
            return None
        remaining_tasks = [
            exec_plan.nodes[task_id].task
            for task_id in remaining_order[idx + 1 :]
            if task_id in exec_plan.nodes
            and exec_plan.nodes[task_id].task is not None
            and exec_plan.nodes[task_id].status == StatusEnum.PENDING
        ]
        if not remaining_tasks:
            return None
        replan_result = self._trigger_replan(node.task, result, remaining_tasks)
        if replan_result.new_tasks == remaining_tasks:
            return None
        self._replace_remaining_tasks(exec_plan, replan_result.new_tasks)
        return [
            task_id
            for task_id in exec_plan.execution_order
            if task_id in exec_plan.nodes and exec_plan.nodes[task_id].status == StatusEnum.PENDING
        ]

    def _execute_parallel_plan(self, exec_plan, results: dict[str, AgentResult]) -> None:
        """Execute dispatchable DAG layers in parallel."""
        parallel_pool = ThreadPoolExecutor(max_workers=max(1, self._max_workers), thread_name_prefix="graph-executor")
        try:
            while True:
                dispatchable_layer = self._next_dispatchable_layer(exec_plan)
                if not dispatchable_layer:
                    break
                self._emit_scaffold_layer_receipt_if_needed(exec_plan, dispatchable_layer)
                layer_results = self._execute_layer_with_optional_executor(
                    dispatchable_layer, exec_plan, results, parallel_pool
                )
                results.update(layer_results)
                self._process_runtime_diffs(exec_plan)
                self._handle_failed_scaffolds(dispatchable_layer, exec_plan, layer_results)
                self._check_layer_milestones(exec_plan, layer_results, results)
        finally:
            parallel_pool.shutdown(wait=True, cancel_futures=False)

    def _execute_layer_with_optional_executor(self, dispatchable_layer, exec_plan, results, parallel_pool):
        """Call _execute_layer_parallel with executor only when supported."""
        execute_layer = self._execute_layer_parallel
        params = inspect.signature(execute_layer).parameters
        accepts_executor = "executor" in params or any(
            param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()
        )
        if accepts_executor:
            return execute_layer(dispatchable_layer, exec_plan, results, executor=parallel_pool)
        return execute_layer(dispatchable_layer, exec_plan, results)

    def _process_runtime_diffs(self, exec_plan) -> None:
        """Emit receipts for queued runtime plan edits."""
        drained = self._drain_runtime_diff_queue(exec_plan)
        for diff, before_state, after_state in drained:
            self._emit_plan_runtime_edit_receipt(exec_plan, diff, before_state, after_state)
        if drained:
            self._clear_cached_execution_layers(exec_plan)

    def _check_layer_milestones(
        self, exec_plan, layer_results: dict[str, AgentResult], results: dict[str, AgentResult]
    ) -> None:
        """Run optional milestone checkpoints after a completed layer."""
        if not self._milestone_manager:
            return
        completed = [task_id for task_id, result in results.items() if result.success]
        for task_id, result in layer_results.items():
            node = exec_plan.nodes.get(task_id)
            if not node or not node.task:
                continue
            approval = self._milestone_manager.check_and_wait(node.task, result, completed)
            if hasattr(approval, "action") and approval.action == _get_milestone_action_class().ABORT:
                raise RuntimeError("Execution aborted at milestone checkpoint")

    def _run_post_execution_worker_task(self, task: AgentTask, log_label: str) -> AgentResult | None:
        """Run a non-blocking post-execution Worker task."""
        if AgentType.WORKER not in self._agents:
            return None
        try:
            result = self._agents[AgentType.WORKER].execute(task)
            return result if result.success else None
        except Exception as exc:
            logger.warning("[AgentGraph] %s failed: %s", log_label, exc)
            return None

    def _add_post_execution_results(self, plan: Plan, results: dict[str, AgentResult]) -> None:
        """Add suggestions and synthesis result entries when Worker succeeds."""
        AgentTask = _get_agent_task_class()

        suggestion_task = AgentTask(
            task_id="suggestion",
            agent_type=AgentType.WORKER,
            description="suggest improvements for project",
            prompt=f"Suggest improvements for: {plan.goal}",
            context={
                "insertion_point": "post_execution",
                "completed_outputs": [str(r.output)[:200] for r in results.values() if r.success][:5],
            },
        )
        suggestion = self._run_post_execution_worker_task(suggestion_task, "Suggestion generation")
        if suggestion is not None:
            results["_suggestions"] = suggestion
        synthesis_task = AgentTask(
            task_id="auto_synthesis",
            agent_type=AgentType.WORKER,
            description="Synthesise execution results into a summary report",
            prompt=f"Synthesise results for plan: {plan.goal}",
            context={
                "mode": "synthesis",
                "completed_tasks": [tid for tid, r in results.items() if r.success],
                "failed_tasks": [tid for tid, r in results.items() if not r.success],
            },
        )
        synthesis = self._run_post_execution_worker_task(synthesis_task, "Operations auto-synthesis")
        if synthesis is not None:
            results["_synthesis"] = synthesis

    def execute_plan(self, plan: Plan) -> dict[str, AgentResult]:
        """Execute a complete plan, parallelising independent tasks where possible.

        Returns:
            Value produced for the caller.

        Raises:
            Exception: Propagated when validation, persistence, or execution fails.
        """
        exec_plan = self._initialize_execution(plan)
        results: dict[str, AgentResult] = {}
        try:
            if self._strategy == ExecutionStrategy.SEQUENTIAL:
                self._execute_sequential_plan(exec_plan, results)
            else:
                self._execute_parallel_plan(exec_plan, results)
            self._add_post_execution_results(plan, results)
            any_failed = any(node.status == StatusEnum.FAILED for node in exec_plan.nodes.values())
            exec_plan.status = (
                StatusEnum.FAILED if any_failed or self._has_unfinished_nodes(exec_plan) else StatusEnum.COMPLETED
            )
        except Exception as exc:
            logger.error("[AgentGraph] Plan execution failed: %s", exc)
            exec_plan.status = StatusEnum.FAILED
            raise
        finally:
            exec_plan.completed_at = self._utc_now_iso()
        return results

    def execute_subgraph(self, plan: Plan, task_ids: list[str]) -> dict[str, AgentResult]:
        """Execute a subset of tasks from an existing plan.

        Args:
            plan: Plan value consumed by execute_subgraph().
            task_ids: Task ids value consumed by execute_subgraph().

        Returns:
            Value produced for the caller.
        """
        logger.info("execute_subgraph: running %d tasks from plan %s", len(task_ids), plan.plan_id)
        task_id_set = set(task_ids)
        filtered_tasks = [task for task in plan.tasks if task.id in task_id_set]
        if not filtered_tasks:
            logger.warning("execute_subgraph: no matching tasks found in plan")
            return {}
        Plan = _get_plan_class()
        subgraph_plan = Plan(plan_id=f"{plan.plan_id}-sub", goal=plan.goal, phase=plan.phase, tasks=filtered_tasks)
        return self.execute_plan(subgraph_plan)
