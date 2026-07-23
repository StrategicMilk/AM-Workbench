"""Layering, receipts, and runtime-edit helpers for graph execution."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.exceptions import ExecutionNotFound
from vetinari.orchestration.graph_executor_layer_helpers import (
    _TERMINAL_NODE_STATUSES,
    _diff_task_id,
    _ensure_runtime_diff_state,
    _get_foreman_agent_with_judgment,
    _sha16,
)
from vetinari.orchestration.graph_executor_layer_helpers import (
    _get_goal_tracker_class as _get_goal_tracker_class,
)
from vetinari.orchestration.graph_executor_layer_helpers import (
    _get_milestone_action_class as _get_milestone_action_class,
)
from vetinari.orchestration.graph_executor_layer_helpers import (
    _get_milestone_manager_class as _get_milestone_manager_class,
)
from vetinari.orchestration.graph_executor_layer_helpers import (
    _lazy_get_vram_manager as _lazy_get_vram_manager,
)
from vetinari.orchestration.graph_task_runner import GraphTaskRunner
from vetinari.orchestration.graph_types import ExecutionDAG, ExecutionStrategy, TaskNode
from vetinari.orchestration.plan_diff import (
    AddDependency,
    PlanDiff,
    PlanRuntimeEditConflict,
    RemoveTask,
    UpdateTask,
    apply_diff,
)
from vetinari.planning.delegation_budget import DelegationBudget
from vetinari.planning.plan_graph import PlanGraph
from vetinari.planning.spec_frame import SpecFrame
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.types import AgentType, EvidenceBasis, StatusEnum, TaskKind

if TYPE_CHECKING:
    from vetinari.agents.contracts import AgentResult, DecomposeDecision, Task

logger = logging.getLogger(__name__)


class _GraphExecutionLayerRunner(GraphTaskRunner):
    """Shared child-plan, layer, and runtime-edit behavior for AgentGraph."""

    def _execute_task_node(
        self,
        node: TaskNode,
        prior_results: dict[str, AgentResult] | None = None,
    ) -> AgentResult:
        """Execute one node, preserving the base task-runner safety path."""
        assigned_plan_id = self._assigned_plan_id_for_task(node.task)
        if assigned_plan_id:
            return self._execute_assigned_plan_node(node, assigned_plan_id)

        result = super()._execute_task_node(node, prior_results)
        if getattr(result, "escalated", False):
            return self._handle_worker_escalation(node, result)
        return result

    def load_plan(self, plan_id: str) -> ExecutionDAG:
        """Load an already-cached child DAG by plan id.

        Returns:
            Resolved plan value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        child_dag = self._execution_plans.get(plan_id)
        if child_dag is None:
            raise KeyError(f"assigned child plan not found: {plan_id}")
        return child_dag

    def execute_dag(self, dag: ExecutionDAG, parent_subtask_id: str = "") -> list[AgentResult]:
        """Execute a cached DAG on this engine instance.

        Args:
            dag: Dag value consumed by execute_dag().
            parent_subtask_id: Parent subtask id value consumed by execute_dag().

        Returns:
            Outcome produced by execute_dag().
        """
        results: dict[str, AgentResult] = {}
        for task_id in dag.execution_order:
            node = dag.nodes[task_id]
            result = self._execute_task_node(node, results)
            results[task_id] = result
            node.status = StatusEnum.COMPLETED if result.success else StatusEnum.FAILED
        dag.status = (
            StatusEnum.FAILED if any(not result.success for result in results.values()) else StatusEnum.COMPLETED
        )
        dag.completed_at = datetime.now(timezone.utc).isoformat()
        return list(results.values())

    def _assigned_plan_id_for_task(self, task: Task) -> str:
        direct = getattr(task, "assigned_plan_id", "")
        if direct:
            return str(direct)
        metadata = self._task_metadata(task)
        return str(metadata.get("assigned_plan_id") or "")

    def _current_plan_id_for_task(self, task: Task) -> str:
        metadata = self._task_metadata(task)
        if metadata.get("plan_id"):
            return str(metadata["plan_id"])
        for plan_id, dag in self._execution_plans.items():
            if task.id in dag.nodes:
                return plan_id
        return str(getattr(task, "plan_id", "") or "default")

    @staticmethod
    def _task_metadata(task: Task) -> dict[str, Any]:
        """Return a mutable metadata mapping for Task-like objects."""
        metadata = getattr(task, "metadata", None)
        if isinstance(metadata, dict):
            return metadata
        if metadata is None:
            try:
                task.metadata = {}
                return task.metadata
            except Exception:
                logger.warning("Handled recoverable failure before fallback.", exc_info=True)
                return {}
        return {}

    def _execute_assigned_plan_node(self, node: TaskNode, child_plan_id: str) -> AgentResult:
        # ADR-0121: atomic cycle check + parent-map registration under one
        # lock — resolved through importlib each call so tests that
        # monkey-patch ``foreman._check_and_register_child_plan`` are
        # honored.
        import importlib

        from vetinari.agents.contracts import AgentResult

        foreman_mod = importlib.import_module("vetinari.agents.consolidated.foreman")
        task = node.task
        current_plan_id = self._current_plan_id_for_task(task)
        try:
            foreman_mod._check_and_register_child_plan(child_plan_id, current_plan_id)
        except RecursionError as exc:
            task.status = StatusEnum.FAILED
            task.error = str(exc)
            task.metadata.setdefault("context", {})["error"] = str(exc)
            task.metadata["error"] = str(exc)
            node.status = StatusEnum.FAILED
            raise

        child_dag = self.load_plan(child_plan_id)
        child_results = self.execute_dag(child_dag, parent_subtask_id=str(task.id))
        child_output_dicts = [
            result.to_dict() if hasattr(result, "to_dict") else dict(result) for result in child_results
        ]
        task.outputs = list(task.outputs) + child_output_dicts
        task.metadata["child_outputs"] = child_output_dicts
        task.metadata["child_plan_id"] = child_plan_id
        self._emit_child_plan_receipt(task, child_plan_id, child_output_dicts)
        return AgentResult(
            success=all(result.success for result in child_results),
            output={"child_plan_id": child_plan_id, "results": child_output_dicts},
            task_id=str(task.id),
        )

    def _emit_child_plan_receipt(self, task: Task, child_plan_id: str, outputs: list[dict]) -> None:
        from vetinari.agents.contracts import OutcomeSignal, ToolEvidence

        store = getattr(self, "_receipt_store", None)
        if store is None:
            store = WorkReceiptStore()
            self._receipt_store = store
        digest = hashlib.sha256(json.dumps(outputs, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        receipt = WorkReceipt(
            project_id=self._current_plan_id_for_task(task),
            agent_id="graph-executor:assigned-plan",
            agent_type=AgentType.FOREMAN,
            kind=WorkReceiptKind.PLAN_ROUND,
            outcome=OutcomeSignal(
                passed=True,
                basis=EvidenceBasis.TOOL_EVIDENCE,
                score=1.0,
                tool_evidence=(
                    ToolEvidence(
                        tool_name="graph_executor",
                        command=f"execute_child_plan child_plan_id={child_plan_id}",
                        exit_code=0,
                        stdout_snippet=f"outputs_digest={digest}",
                        stdout_hash=digest,
                        passed=True,
                    ),
                ),
                issues=(f"child_plan_id={child_plan_id}", f"outputs_digest={digest}"),
            ),
            inputs_summary=f"parent_task_id={task.id}; child_plan_id={child_plan_id}"[:200],
            outputs_summary=f"outputs_digest={digest}"[:200],
        )
        store.append(receipt)

    def _read_task_kind(self, node: TaskNode) -> TaskKind:
        """Return a node's scaffold-then-fill pass, defaulting conservatively."""
        metadata = self._task_metadata(node.task)
        raw_value = metadata.get("kind")
        if raw_value is None or raw_value == "":
            return TaskKind.IMPLEMENTATION
        if isinstance(raw_value, TaskKind):
            return raw_value
        try:
            return TaskKind(str(raw_value))
        except ValueError:
            logger.warning(
                "Invalid TaskKind metadata %r for task %s; defaulting to implementation",
                raw_value,
                node.task.id,
            )
            return TaskKind.IMPLEMENTATION

    def _build_kind_aware_layers(self, exec_plan: ExecutionDAG) -> list[list[str]]:
        """Build layers in SCAFFOLD, IMPLEMENTATION, VERIFICATION pass order.

        ``all_completed`` accumulates across passes so that cross-pass explicit
        dependency edges (e.g. an IMPL task that depends on a specific SCAFFOLD
        task) are honoured correctly.  Resetting per-pass would silently drop
        those edges, allowing IMPL tasks to run before their SCAFFOLD
        dependencies finished.
        """
        ordered_layers: list[list[str]] = []
        pass_order = (TaskKind.SCAFFOLD, TaskKind.IMPLEMENTATION, TaskKind.VERIFICATION)
        all_completed: set[str] = set()

        for task_kind in pass_order:
            pass_task_ids = {tid for tid, node in exec_plan.nodes.items() if self._read_task_kind(node) == task_kind}
            if not pass_task_ids:
                continue

            remaining = set(pass_task_ids)
            raw_layers: list[list[str]] = []
            while remaining:
                ready = sorted(tid for tid in remaining if exec_plan.nodes[tid].dependencies <= all_completed)
                if not ready:
                    logger.warning(
                        "Intra-pass dependency cycle in %s pass; forcing arbitrary ordering for %d task(s)",
                        task_kind.value,
                        len(remaining),
                    )
                    ready = sorted(remaining)
                raw_layers.append(ready)
                remaining.difference_update(ready)
                all_completed.update(ready)

            for raw_layer in raw_layers:
                ordered_layers.extend(self._subdivide_layer_by_parallelism(raw_layer, exec_plan))

        return ordered_layers

    def _cancel_dependents_of_failed_scaffold(
        self,
        failed_node: TaskNode,
        exec_plan: ExecutionDAG,
    ) -> list[str]:
        """Cancel dependents of a failed SCAFFOLD task before the next pass."""
        cancelled: list[str] = []
        queue = deque(failed_node.dependents)
        seen: set[str] = set()
        enqueued: set[str] = set(failed_node.dependents)
        while queue:
            task_id = queue.popleft()
            if task_id in seen:
                continue
            seen.add(task_id)
            node = exec_plan.nodes.get(task_id)
            if node is None:
                continue
            for dependent_id in node.dependents:
                if dependent_id not in seen and dependent_id not in enqueued:
                    enqueued.add(dependent_id)
                    queue.append(dependent_id)
            metadata = self._task_metadata(node.task)
            if node.status in {StatusEnum.PENDING, StatusEnum.READY}:
                node.status = StatusEnum.CANCELLED
                node.task.status = StatusEnum.CANCELLED
                metadata["upstream_scaffold_failed"] = failed_node.task.id
                metadata["error"] = f"cancelled: upstream SCAFFOLD task {failed_node.task.id} failed"
                node.task.error = f"cancelled: upstream SCAFFOLD task {failed_node.task.id} failed"
                cancelled.append(task_id)
            elif node.status == StatusEnum.RUNNING:
                metadata["upstream_scaffold_failed"] = failed_node.task.id
        return sorted(cancelled)

    def _emit_layer_transition_receipt(
        self,
        exec_plan: ExecutionDAG,
        pass_label: str,
        dispatched_task_ids: list[str],
    ) -> None:
        """Emit a PLAN_ROUND receipt naming the scaffold-then-fill pass."""
        from vetinari.agents.contracts import OutcomeSignal, ToolEvidence

        if not dispatched_task_ids:
            return
        layer_kind = self._read_task_kind(exec_plan.nodes[dispatched_task_ids[0]])
        if any(self._read_task_kind(exec_plan.nodes[tid]) != layer_kind for tid in dispatched_task_ids):
            raise RuntimeError("partition invariant violated: layer mixes kinds")
        if pass_label != layer_kind.name:
            raise RuntimeError("partition invariant violated: pass label does not match layer kind")

        store = getattr(self, "_receipt_store", None)
        if store is None:
            store = WorkReceiptStore()
            self._receipt_store = store
        digest = hashlib.sha256(",".join(sorted(dispatched_task_ids)).encode("utf-8")).hexdigest()[:16]
        receipt = WorkReceipt(
            project_id=exec_plan.plan_id,
            agent_id="graph-executor:scaffold-then-fill",
            agent_type=AgentType.FOREMAN,
            kind=WorkReceiptKind.PLAN_ROUND,
            outcome=OutcomeSignal(
                passed=True,
                basis=EvidenceBasis.TOOL_EVIDENCE,
                score=1.0,
                tool_evidence=(
                    ToolEvidence(
                        tool_name="graph_executor",
                        command=f"dispatch_scaffold_then_fill_pass pass={pass_label}",
                        exit_code=0,
                        stdout_snippet=f"task_digest={digest}",
                        stdout_hash=digest,
                        passed=True,
                    ),
                ),
                issues=(f"pass={pass_label}", f"task_digest={digest}"),
            ),
            inputs_summary=f"pass={pass_label}"[:200],
            outputs_summary=",".join(sorted(dispatched_task_ids))[:200],
        )
        store.append(receipt)

    def _enqueue_runtime_diff(self, plan_id: str, diff: PlanDiff) -> None:
        """Queue a live-plan edit for the next layer boundary.

        Mid-layer edits are deferred to the next layer transition. The drain
        runs only after layer futures have joined and before scaffold-failure
        cancellation walks, so no in-flight task is mutated.
        """
        with _ensure_runtime_diff_state(self):
            exec_plan = self._execution_plans.get(plan_id)
            if exec_plan is None:
                raise ExecutionNotFound(plan_id)
            self._raise_runtime_conflict_if_needed(exec_plan, diff)
            self._runtime_diff_queues.setdefault(plan_id, []).append(diff)

    def _drain_runtime_diff_queue(
        self,
        exec_plan: ExecutionDAG,
    ) -> list[tuple[PlanDiff, dict[str, Any], dict[str, Any]]]:
        """Apply queued runtime edits at a layer boundary.

        Returns the applied diff snapshots for receipt emission. Receipt I/O is
        deliberately outside this lock-held method.
        """
        with _ensure_runtime_diff_state(self):
            queued = self._runtime_diff_queues.pop(exec_plan.plan_id, [])
            drained: list[tuple[PlanDiff, dict[str, Any], dict[str, Any]]] = []
            for diff in queued:
                before_state, after_state = apply_diff(diff, exec_plan)
                drained.append((diff, before_state, after_state))
            if drained:
                self._select_execution_layers(exec_plan)
            return drained

    @staticmethod
    def _raise_runtime_conflict_if_needed(exec_plan: ExecutionDAG, diff: PlanDiff) -> None:
        diff_kind = type(diff).__name__
        if isinstance(diff, (RemoveTask, UpdateTask)) or diff_kind in {"RemoveTask", "UpdateTask"}:
            node = exec_plan.nodes.get(diff.task_id)
            if node is None:
                raise ValueError(f"task {diff.task_id} not present in plan")
            if _status_is(node.status, StatusEnum.RUNNING):
                raise PlanRuntimeEditConflict(diff.task_id, StatusEnum.RUNNING, diff_kind)
            if diff_kind == "RemoveTask" and _status_is(node.status, StatusEnum.COMPLETED):
                raise PlanRuntimeEditConflict(diff.task_id, StatusEnum.COMPLETED, diff_kind)
        if isinstance(diff, AddDependency) or diff_kind == "AddDependency":
            node = exec_plan.nodes.get(diff.from_task_id)
            if node is None:
                raise ValueError(f"task {diff.from_task_id} not present in plan")
            if _status_in(node.status, {StatusEnum.RUNNING, StatusEnum.COMPLETED, StatusEnum.FAILED}):
                raise PlanRuntimeEditConflict(diff.from_task_id, node.status, diff_kind)

    def _emit_plan_runtime_edit_receipt(
        self,
        exec_plan: ExecutionDAG,
        diff: PlanDiff,
        before_state: dict[str, Any],
        after_state: dict[str, Any],
    ) -> None:
        """Emit one replayable receipt after one runtime edit is applied."""
        from vetinari.agents.contracts import OutcomeSignal, ToolEvidence

        store = getattr(self, "_receipt_store", None)
        if store is None:
            store = WorkReceiptStore()
            self._receipt_store = store
        edit_hash = hashlib.sha256(
            json.dumps(
                {"before": before_state, "after": after_state, "op": type(diff).__name__},
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        receipt = WorkReceipt(
            project_id=exec_plan.plan_id,
            agent_id="graph-executor:plan-runtime-edit",
            agent_type=AgentType.FOREMAN,
            kind=WorkReceiptKind.PLAN_RUNTIME_EDIT,
            outcome=OutcomeSignal(
                passed=True,
                basis=EvidenceBasis.TOOL_EVIDENCE,
                score=1.0,
                tool_evidence=(
                    ToolEvidence(
                        tool_name="graph_executor",
                        command=f"apply_plan_runtime_edit op={type(diff).__name__} task_id={_diff_task_id(diff)}",
                        exit_code=0,
                        stdout_snippet=f"before_sha={_sha16(before_state)};after_sha={_sha16(after_state)}",
                        stdout_hash=edit_hash,
                        passed=True,
                    ),
                ),
                issues=(
                    json.dumps(before_state, sort_keys=True),
                    json.dumps(after_state, sort_keys=True),
                ),
            ),
            inputs_summary=f"op={type(diff).__name__};task_id={_diff_task_id(diff)}"[:200],
            outputs_summary=f"before_sha={_sha16(before_state)};after_sha={_sha16(after_state)}"[:200],
        )
        store.append(receipt)

    # Class-level flag: log the scaffold-then-fill strategy choice only once
    # per executor instance to avoid per-layer log spam.
    _logged_scaffold_strategy: bool = False

    def _select_execution_layers(self, exec_plan: ExecutionDAG) -> list[list[str]]:
        """Select the layer-building strategy, honouring the kill-switch env flag."""
        if self._strategy == ExecutionStrategy.SCAFFOLD_THEN_FILL:
            flag = os.environ.get("VETINARI_SCAFFOLD_THEN_FILL", "1")
            if flag in ("0", "false", "False"):
                if not self._logged_scaffold_strategy:
                    logger.info(
                        "VETINARI_SCAFFOLD_THEN_FILL kill-switch active: falling back to standard scheduler "
                        "for plan %s (ADR-0123)",
                        exec_plan.plan_id,
                    )
                    self._logged_scaffold_strategy = True
                return self._build_execution_layers(exec_plan)
            if not self._logged_scaffold_strategy:
                logger.info(
                    "SCAFFOLD_THEN_FILL active (default-on) for plan %s",
                    exec_plan.plan_id,
                )
                self._logged_scaffold_strategy = True
            return self._build_kind_aware_layers(exec_plan)
        return self._build_execution_layers(exec_plan)

    def _cached_execution_layers(self, exec_plan: ExecutionDAG) -> list[list[str]]:
        cached = getattr(exec_plan, "_selected_execution_layers", None)
        if cached is None:
            cached = self._select_execution_layers(exec_plan)
            exec_plan._selected_execution_layers = cached
        return cached

    @staticmethod
    def _clear_cached_execution_layers(exec_plan: ExecutionDAG) -> None:
        if hasattr(exec_plan, "_selected_execution_layers"):
            del exec_plan._selected_execution_layers

    @staticmethod
    def _dispatchable_layer(layer: list[str], exec_plan: ExecutionDAG) -> list[str]:
        return [
            tid
            for tid in layer
            if tid in exec_plan.nodes and exec_plan.nodes[tid].status in {StatusEnum.PENDING, StatusEnum.READY}
        ]

    def _next_dispatchable_layer(self, exec_plan: ExecutionDAG) -> list[str]:
        """Return the next runnable layer from the current DAG state."""
        for layer in self._cached_execution_layers(exec_plan):
            dispatchable_layer = self._dispatchable_layer(layer, exec_plan)
            if dispatchable_layer:
                return dispatchable_layer
        return []

    @staticmethod
    def _has_unfinished_nodes(exec_plan: ExecutionDAG) -> bool:
        return any(node.status not in _TERMINAL_NODE_STATUSES for node in exec_plan.nodes.values())

    def _handle_failed_scaffolds(
        self,
        layer: list[str],
        exec_plan: ExecutionDAG,
        layer_results: dict[str, AgentResult],
    ) -> None:
        if self._strategy != ExecutionStrategy.SCAFFOLD_THEN_FILL:
            return
        for task_id in layer:
            node = exec_plan.nodes.get(task_id)
            result = layer_results.get(task_id)
            if node is None or result is None:
                continue
            if self._read_task_kind(node) == TaskKind.SCAFFOLD and result.success is False:
                with _ensure_runtime_diff_state(self):
                    self._cancel_dependents_of_failed_scaffold(node, exec_plan)

    def _emit_scaffold_layer_receipt_if_needed(self, exec_plan: ExecutionDAG, layer: list[str]) -> None:
        if self._strategy != ExecutionStrategy.SCAFFOLD_THEN_FILL or not layer:
            return
        pass_label = self._read_task_kind(exec_plan.nodes[layer[0]]).name
        self._emit_layer_transition_receipt(exec_plan, pass_label, layer)

    def _handle_worker_escalation(self, node: TaskNode, result: AgentResult) -> AgentResult:
        from vetinari.agents.contracts import AgentResult

        task = node.task
        if task.metadata.get("_worker_escalation_attempted"):
            error = "worker_escalation_loop_detected"
            task.status = StatusEnum.FAILED
            task.error = error
            task.metadata["error"] = error
            node.status = StatusEnum.FAILED
            return AgentResult(success=False, output=result.output, errors=[error], task_id=str(task.id))

        task.metadata["_worker_escalation_attempted"] = True
        spec_frame = SpecFrame(goal=task.description, in_scope=tuple(task.outputs))
        object.__setattr__(spec_frame, "worker_escalation_reason", result.escalation_reason)
        decision = self._judge_escalated_task(task, spec_frame)
        task.metadata["decompose_decision_action"] = decision.action
        task.metadata["decompose_decision_reason"] = decision.reason
        task.metadata["decompose_decision_confidence"] = decision.confidence

        if decision.action == "decompose_further":
            child_plan_id = str(result.metadata.get("assigned_plan_id") or task.metadata.get("assigned_plan_id") or "")
            if not child_plan_id:
                error = "worker_escalation_missing_child_plan_id"
                task.status = StatusEnum.FAILED
                task.metadata["error"] = error
                node.status = StatusEnum.FAILED
                return AgentResult(success=False, output=result.output, errors=[error], task_id=str(task.id))
            return self._execute_assigned_plan_node(node, child_plan_id)

        error = decision.reason
        task.status = StatusEnum.FAILED
        task.error = error
        task.metadata["error"] = error
        node.status = StatusEnum.FAILED
        return AgentResult(success=False, output=result.output, errors=[error], task_id=str(task.id))

    def _judge_escalated_task(self, task: Task, spec_frame: SpecFrame) -> DecomposeDecision:
        foreman = getattr(self, "_foreman", None)
        if foreman is None:
            foreman = _get_foreman_agent_with_judgment()
            self._foreman = foreman
        return foreman.judge_decomposability(
            task=task,
            plan_graph=getattr(self, "_plan_graph", PlanGraph()),
            delegation_budget=getattr(self, "_delegation_budget", DelegationBudget(str(task.id))),
            spec_frame=spec_frame,
            recursive_depth=int(task.metadata.get("recursive_depth", task.depth)),
        )


def _status_value(status: Any) -> Any:
    return getattr(status, "value", status)


def _status_is(status: Any, expected: StatusEnum) -> bool:
    return _status_value(status) == expected.value


def _status_in(status: Any, expected: set[StatusEnum]) -> bool:
    value = _status_value(status)
    return any(value == item.value for item in expected)
