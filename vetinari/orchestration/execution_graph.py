"""Execution Graph — Layer 1 data structures for the Two-Layer Orchestration System.

Contains the DAG (Directed Acyclic Graph) of tasks used for plan execution.
This is distinct from :class:`~vetinari.orchestration.graph_types.TaskNode`
which represents agent-level graph nodes in the AgentGraph.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.exceptions import CircularDependencyError
from vetinari.security.fail_closed import SchemaOpenError, assert_closed_schema, sanitize_untrusted_text
from vetinari.types import PlanStatus, StatusEnum
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


@dataclass
class ExecutionTaskNode:
    """A single task node in the execution graph."""

    id: str
    description: str
    task_type: str = "general"

    # Dependencies
    depends_on: list[str] = field(default_factory=list)
    depended_by: list[str] = field(default_factory=list)

    # Execution
    status: StatusEnum = StatusEnum.PENDING
    assigned_model: str = ""

    # Results
    input_data: dict[str, Any] = field(default_factory=dict)
    output_data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    # Metadata
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_at: str | None = None
    completed_at: str | None = None
    retry_count: int = 0
    max_retries: int = 3

    # Checkpoint
    checkpoint_id: str = ""

    def __repr__(self) -> str:
        return (
            f"ExecutionTaskNode(id={self.id!r}, task_type={self.task_type!r}, "
            f"status={self.status.value!r}, retry_count={self.retry_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Converts this task node to a plain dictionary suitable for JSON output."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ExecutionTaskNode:
        """Reconstruct a ExecutionTaskNode from a plain dictionary.

        Args:
            data: Dictionary previously produced by ``to_dict`` or an
                equivalent JSON payload containing task node fields.

        Returns:
            A new ExecutionTaskNode instance populated from the dictionary values.

        Raises:
            ValueError: If the payload cannot be parsed into a task node.
        """
        try:
            assert_closed_schema(
                data,
                allowed_keys=(
                    "id",
                    "description",
                    "task_type",
                    "depends_on",
                    "depended_by",
                    "status",
                    "assigned_model",
                    "input_data",
                    "output_data",
                    "error",
                    "created_at",
                    "started_at",
                    "completed_at",
                    "retry_count",
                    "max_retries",
                    "checkpoint_id",
                ),
                required_keys=("id", "description"),
            )
            _status = StatusEnum(data.get("status", StatusEnum.PENDING.value))
        except (SchemaOpenError, ValueError) as exc:
            raise ValueError(f"ExecutionTaskNode payload is unsafe: {exc}") from exc
        return cls(
            id=sanitize_untrusted_text(data["id"], max_length=160),
            description=sanitize_untrusted_text(data["description"], max_length=2000),
            task_type=sanitize_untrusted_text(data.get("task_type", "general"), max_length=120),
            depends_on=data.get("depends_on", []),
            depended_by=data.get("depended_by", []),
            status=_status,
            assigned_model=data.get("assigned_model", ""),
            input_data=data.get("input_data", {}),
            output_data=data.get("output_data", {}),
            error=data.get("error", ""),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            checkpoint_id=data.get("checkpoint_id", ""),
        )


@dataclass
class ExecutionGraph:
    """Directed Acyclic Graph (DAG) of tasks for execution.

    Supports:
    - Parallel execution of independent tasks
    - Dependency resolution
    - Execution ordering
    - Checkpoint management
    """

    plan_id: str
    goal: str

    # Nodes in the graph
    nodes: dict[str, ExecutionTaskNode] = field(default_factory=dict)

    # Graph metadata
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    topology_metadata: dict[str, Any] = field(default_factory=dict)

    # Execution state
    status: PlanStatus = PlanStatus.DRAFT
    current_layer: int = 0
    completed_count: int = 0
    failed_count: int = 0

    def add_task(
        self,
        task_id: str,
        description: str,
        task_type: str = "general",
        depends_on: list[str] | None = None,
        input_data: dict[str, Any] | None = None,
    ) -> ExecutionTaskNode:
        """Add a task to the graph.

        Args:
            task_id: The task id.
            description: The description.
            task_type: The task type.
            depends_on: The depends on.
            input_data: The input data.

        Returns:
            The ExecutionTaskNode result.
        """
        node = ExecutionTaskNode(
            id=task_id,
            description=description,
            task_type=task_type,
            depends_on=depends_on or [],
            input_data=input_data or {},
        )

        # Update dependency links
        for dep_id in node.depends_on:
            if dep_id in self.nodes:
                self.nodes[dep_id].depended_by.append(task_id)

        self.nodes[task_id] = node
        self.updated_at = datetime.now(timezone.utc).isoformat()
        return node

    def get_ready_tasks(self) -> list[ExecutionTaskNode]:
        """Get tasks whose dependencies are all completed.

        Also marks PENDING tasks as BLOCKED when any predecessor has failed,
        and CANCELLED when any predecessor has been cancelled — preventing
        dependents from being stuck in PENDING forever.

        Returns:
            List of ExecutionTaskNodes ready for execution.
        """
        ready = []
        for node in self.nodes.values():
            if node.status != StatusEnum.PENDING:
                continue

            has_failed_dep = False
            has_cancelled_dep = False
            all_completed = True
            for dep_id in node.depends_on:
                dep_node = self.nodes.get(dep_id)
                if dep_node is None:
                    all_completed = False
                    continue
                if dep_node.status == StatusEnum.FAILED:
                    has_failed_dep = True
                    break
                if dep_node.status == StatusEnum.CANCELLED:
                    has_cancelled_dep = True
                    break
                if dep_node.status != StatusEnum.COMPLETED:
                    all_completed = False

            if has_failed_dep:
                node.status = StatusEnum.BLOCKED
                node.error = f"Blocked: predecessor {dep_id!r} failed"
            elif has_cancelled_dep:
                # Propagate cancellation transitively — a cancelled predecessor
                # means this task can never run, so cancel it too rather than
                # leaving it stranded in PENDING.
                node.status = StatusEnum.CANCELLED
                node.error = f"Cancelled: predecessor {dep_id!r} was cancelled"
            elif all_completed:
                ready.append(node)
        return ready

    def get_next_layer(self) -> list[list[ExecutionTaskNode]]:
        """Get the full execution schedule as layers of parallel tasks.

        Uses a simulation pass: tasks are "virtually completed" after being
        placed in a layer so that subsequent layers correctly detect their
        dependencies as satisfied.

        Returns:
            List of results.

        Raises:
            CircularDependencyError: If pending tasks cannot be scheduled due
                to blocked tasks or unsatisfied dependencies.
        """
        layers: list[list[ExecutionTaskNode]] = []
        simulated_completed: set[str] = {nid for nid, n in self.nodes.items() if n.status == StatusEnum.COMPLETED}
        blocked = {nid: n for nid, n in self.nodes.items() if n.status == StatusEnum.BLOCKED}
        remaining = {nid: n for nid, n in self.nodes.items() if n.status == StatusEnum.PENDING}

        if blocked and not remaining:
            blocked_ids = ", ".join(sorted(blocked))
            raise CircularDependencyError(f"execution graph has no schedulable tasks; blocked tasks: {blocked_ids}")

        while remaining:
            current_layer: list[ExecutionTaskNode] = []
            to_remove: list[str] = []

            for task_id, node in remaining.items():
                deps_met = all(dep_id in simulated_completed for dep_id in node.depends_on)
                if deps_met:
                    current_layer.append(node)
                    to_remove.append(task_id)

            if not current_layer:
                remaining_ids = ", ".join(sorted(remaining))
                missing_deps = sorted({
                    dep_id for node in remaining.values() for dep_id in node.depends_on if dep_id not in self.nodes
                })
                detail = f"remaining tasks: {remaining_ids}"
                if missing_deps:
                    detail += f"; missing dependencies: {', '.join(missing_deps)}"
                logger.warning("get_next_layer: no tasks available - %s", detail)
                raise CircularDependencyError(f"execution graph cannot be scheduled ({detail})")

            layers.append(current_layer)
            for task_id in to_remove:
                del remaining[task_id]
                simulated_completed.add(task_id)

        return layers

    def get_execution_order(self) -> list[list[ExecutionTaskNode]]:
        """Get full execution order as layers."""
        return self.get_next_layer()

    def can_retry(self, task_id: str) -> bool:
        """Check if a failed task can be retried.

        Returns:
            True if successful, False otherwise.
        """
        if task_id not in self.nodes:
            return False
        node = self.nodes[task_id]
        return node.retry_count < node.max_retries

    def get_blocked_tasks(self) -> list[ExecutionTaskNode]:
        """Get tasks that are blocked waiting for dependencies."""
        return [n for n in self.nodes.values() if n.status == StatusEnum.BLOCKED]

    def get_failed_tasks(self) -> list[ExecutionTaskNode]:
        """Get all failed tasks."""
        return [n for n in self.nodes.values() if n.status == StatusEnum.FAILED]

    def get_completed_tasks(self) -> list[ExecutionTaskNode]:
        """Get all completed tasks."""
        return [n for n in self.nodes.values() if n.status == StatusEnum.COMPLETED]

    def __repr__(self) -> str:
        return (
            f"ExecutionGraph(plan_id={self.plan_id!r}, status={self.status.value!r}, "
            f"nodes={len(self.nodes)}, completed={self.completed_count!r}, "
            f"failed={self.failed_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Converts this execution graph to a plain dictionary suitable for JSON output."""
        return dataclass_to_dict(self)

    def to_dag_json(self) -> str:
        """Export graph in DAG format for visualization."""
        return json.dumps(self.to_dict(), indent=2)
