"""DAG construction and pipeline building for the AgentGraph.

Handles plan-to-execution-plan conversion, topological ordering, layer
grouping for parallel execution, and linear pipeline construction.

This is step 1 of the execution flow: receive a Plan, validate delegation
constraints, build the TaskNode graph, and compute the execution order.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import (
    Plan,
    Task,
)
from vetinari.boundary_guards import account_evidence_drop
from vetinari.exceptions import CircularDependencyError, PlanningError
from vetinari.orchestration.graph_types import (
    ExecutionDAG as ExecutionPlan,
)
from vetinari.orchestration.graph_types import (
    TaskNode,
)
from vetinari.types import AgentType, StatusEnum

logger = logging.getLogger(__name__)


class GraphPlanningEngine:
    """DAG construction methods for AgentGraph.

    Provides plan creation, topological sorting, layer grouping, and
    linear pipeline construction. Mixed into AgentGraph alongside the
    executor, validator, and recovery mixins.

    Attributes expected on ``self``:
        _execution_plans (dict[str, ExecutionPlan]): Plan cache shared with executor.
        _agents (dict[AgentType, Any]): Registered agent instances.
    """

    # ------------------------------------------------------------------
    # Plan creation
    # ------------------------------------------------------------------

    if TYPE_CHECKING:
        _agents: dict[AgentType, Any]
        _execution_plans: dict[str, ExecutionPlan]

    def create_execution_plan(self, plan: Plan) -> ExecutionPlan:
        """Build an ExecutionPlan with task nodes and topological order.

        Validates delegation constraints between tasks — if task B depends
        on task A, the delegation from A's agent to B's agent must be allowed
        by architecture constraints.

        Args:
            plan: The Plan to convert into an ExecutionPlan.

        Returns:
            An ExecutionPlan ready for execution.

        Raises:
            PlanningError: If constraint validation is unavailable.
            CircularDependencyError: If task dependencies contain a cycle.

        Constraint validation fails closed: planning stops when the registry is
        unavailable, after recording the dropped validation evidence.
        """
        exec_plan = ExecutionPlan(plan_id=plan.plan_id, original_plan=plan)

        for task in plan.tasks:
            node = TaskNode(
                task=task,
                dependencies=set(task.dependencies),
                status=StatusEnum.PENDING,
            )
            exec_plan.nodes[task.id] = node

        # Validate delegation constraints across the DAG
        try:
            from vetinari.constraints.registry import get_constraint_registry

            _reg = get_constraint_registry()
            for task in plan.tasks:
                for dep_id in task.dependencies:
                    dep_task = exec_plan.nodes.get(dep_id)
                    if dep_task is None:
                        continue
                    from_type = dep_task.task.assigned_agent
                    to_type = task.assigned_agent
                    if from_type == to_type:
                        continue  # Same agent, no delegation
                    from_val = from_type.value if hasattr(from_type, "value") else str(from_type)
                    to_val = to_type.value if hasattr(to_type, "value") else str(to_type)
                    allowed, reason = _reg.validate_delegation(from_val, to_val)
                    if not allowed:
                        logger.warning(
                            "[AgentGraph] Delegation constraint violation: %s -> %s for task %s: %s",
                            from_val,
                            to_val,
                            task.id,
                            reason,
                        )
        except Exception as exc:
            item = {"plan_id": plan.plan_id, "task_count": len(plan.tasks)}
            account_evidence_drop(item, "constraint_validation", logger=logger)
            logger.warning("Constraint system unavailable during graph planning")
            raise PlanningError("constraint system unavailable during graph planning") from exc

        # Build reverse edges (dependents)
        for task_id, node in exec_plan.nodes.items():
            for dep_id in node.dependencies:
                if dep_id in exec_plan.nodes:
                    exec_plan.nodes[dep_id].dependents.add(task_id)

        exec_plan.execution_order = self._topological_sort(exec_plan.nodes)
        self._execution_plans[plan.plan_id] = exec_plan
        return exec_plan

    @staticmethod
    def _topological_sort(nodes: dict[str, TaskNode]) -> list[str]:
        """Kahn's algorithm topological sort with cycle detection.

        Args:
            nodes: The task node dictionary keyed by task ID.

        Returns:
            A topologically ordered list of task IDs.

        Raises:
            CircularDependencyError: If a cycle is detected in the graph.
        """
        in_degree = {tid: len(n.dependencies) for tid, n in nodes.items()}
        queue = deque(tid for tid, d in in_degree.items() if d == 0)
        result: list[str] = []

        while queue:
            current = queue.popleft()
            result.append(current)
            for dependent_id in nodes[current].dependents:
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    queue.append(dependent_id)

        if len(result) != len(nodes):
            raise CircularDependencyError("Circular dependency detected in task graph")
        return result

    # ------------------------------------------------------------------
    # Layer grouping for parallel execution
    # ------------------------------------------------------------------

    def _build_execution_layers(self, exec_plan: ExecutionPlan) -> list[list[str]]:
        """Group tasks into parallel layers by dependency level.

        Each layer contains all tasks whose dependencies are fully satisfied
        by the previous layers, enabling parallel execution within a layer.
        Oversized layers are subdivided to respect per-agent-type
        ``max_parallel_tasks`` constraints.  Decision: ADR-0087.

        Args:
            exec_plan: The execution plan whose nodes to group.

        Returns:
            List of layers, each layer a list of task IDs that can run in parallel.
        """
        raw_layers: list[list[str]] = []
        in_degree = {tid: len(node.dependencies) for tid, node in exec_plan.nodes.items()}
        ready = deque(tid for tid, degree in in_degree.items() if degree == 0)
        placed: set[str] = set()

        while ready:
            layer = list(ready)
            ready.clear()
            raw_layers.append(layer)
            for task_id in layer:
                placed.add(task_id)
                for dependent_id in exec_plan.nodes[task_id].dependents:
                    if dependent_id not in in_degree:
                        continue
                    in_degree[dependent_id] -= 1
                    if in_degree[dependent_id] == 0:
                        ready.append(dependent_id)

        if len(placed) != len(exec_plan.nodes):
            # Preserve the legacy fail-soft behavior for malformed DAGs that
            # bypassed topological validation: expose blocked tasks in one
            # final layer instead of hiding them.
            raw_layers.append([tid for tid in exec_plan.nodes if tid not in placed])

        # Subdivide layers that exceed max_parallel_tasks for any agent type
        layers: list[list[str]] = []
        for raw_layer in raw_layers:
            layers.extend(self._subdivide_layer_by_parallelism(raw_layer, exec_plan))

        return layers

    @staticmethod
    def _subdivide_layer_by_parallelism(
        layer: list[str],
        exec_plan: ExecutionPlan,
    ) -> list[list[str]]:
        """Split a layer into sub-layers respecting max_parallel_tasks per agent type.

        If a layer has 5 WORKER tasks but ``max_parallel_tasks`` for WORKER is 3,
        the layer is split into [3 tasks, 2 tasks].  This prevents VRAM
        exhaustion from too many concurrent agents of the same type.

        Args:
            layer: Task IDs that are dependency-ready.
            exec_plan: The execution plan (used to look up agent types).

        Returns:
            One or more sub-layers, each respecting parallelism limits.
        """
        from vetinari.constraints.resources import get_resource_constraint

        if len(layer) <= 1:
            return [layer]

        # Find the tightest parallelism constraint across agent types in this layer
        max_allowed = len(layer)  # default: no constraint
        for tid in layer:
            node = exec_plan.nodes.get(tid)
            if node and node.task:
                agent_val = node.task.assigned_agent
                agent_str = agent_val.value if hasattr(agent_val, "value") else str(agent_val)
                constraint = get_resource_constraint(agent_str)
                if constraint.max_parallel_tasks < max_allowed:
                    max_allowed = constraint.max_parallel_tasks

        if max_allowed >= len(layer):
            return [layer]

        # Split into chunks of max_allowed
        return [layer[i : i + max_allowed] for i in range(0, len(layer), max_allowed)]

    # ------------------------------------------------------------------
    # Pipeline builder — variable-length DAGs per tier
    # ------------------------------------------------------------------

    def build_pipeline(self, agents: list[AgentType], *, clock: Callable[[], float] | None = None) -> ExecutionPlan:
        """Build an ExecutionPlan with a linear pipeline of specified agents.

        Creates a sequential DAG where each agent depends on the previous one.
        Used for tier-based routing: Express gets [WORKER, INSPECTOR],
        Standard gets [FOREMAN, WORKER, INSPECTOR], etc.

        Args:
            agents: Ordered list of agent types forming the pipeline.
            clock: Optional timestamp source used for deterministic plan IDs.

        Returns:
            An ExecutionPlan with len(agents) nodes in a linear chain.

        Raises:
            PlanningError: If agents list is empty.
        """
        if not agents:
            raise PlanningError("Pipeline requires at least one agent")

        active_clock = clock or time.time
        plan_id = f"pipeline-{int(active_clock())}"
        tasks: list[Task] = []
        prev_task_id: str | None = None

        for idx, agent_type in enumerate(agents):
            task_id = f"{plan_id}-{agent_type.value}-{idx}"
            deps = [prev_task_id] if prev_task_id is not None else []
            task = Task(
                id=task_id,
                description=f"Pipeline step {idx}: {agent_type.value}",
                assigned_agent=agent_type,
                dependencies=deps,
            )
            tasks.append(task)
            prev_task_id = task_id

        plan = Plan(
            plan_id=plan_id,
            goal=f"Pipeline with {len(agents)} agents",
            tasks=tasks,
        )
        return self.create_execution_plan(plan)
