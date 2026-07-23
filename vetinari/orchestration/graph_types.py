"""Shared dataclasses, enums, and helper classes for the AgentGraph.

This module contains types that are co-owned by the orchestration engine and
by external consumers (tests, two_layer orchestrator, etc.) without pulling in
the full AgentGraph machinery.  Keeping them here breaks the import-chain that
would otherwise occur when external modules need a lightweight reference to
``TaskNode`` or ``ExecutionPlan``.

Exported:
    ExecutionStrategy — parallel/sequential/adaptive strategy enum
    ConditionalEdge   — conditional routing edge in the DAG
    CycleDetector     — tracks per-task iteration counts
    HumanCheckpoint   — human-in-the-loop approval management
    TaskNode          — single node in the execution DAG
    ExecutionPlan     — full execution plan with task graph
    ReplanResult      — result of a mid-execution replan request
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.agents.contracts import AgentResult, Task
    from vetinari.agents.contracts import ExecutionPlan as ContractsExecutionPlan


class ExecutionStrategy(Enum):
    """Strategy for task execution.

    SCAFFOLD_THEN_FILL partitions tasks by TaskKind before topological layering.
    """

    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"
    ADAPTIVE = "adaptive"
    SCAFFOLD_THEN_FILL = "scaffold_then_fill"


# ── Graph enhancement dataclasses (US-031) ────────────────────────────────


@dataclass(frozen=True, slots=True)
class ConditionalEdge:
    """A conditional edge in the execution DAG.

    Routes execution to ``true_target`` or ``false_target`` based on
    evaluating ``condition`` against the source task's result.

    Args:
        source_task_id: The task whose result is evaluated.
        condition: Callable that receives an AgentResult and returns bool.
        true_target: Task ID to execute when condition is True.
        false_target: Task ID to execute when condition is False.
        description: Human-readable description of the routing logic.
    """

    source_task_id: str
    condition: Any  # Callable[[AgentResult | None], bool]
    true_target: str
    false_target: str
    description: str = ""

    def __repr__(self) -> str:
        return (
            f"ConditionalEdge(source_task_id={self.source_task_id!r}, "
            f"true_target={self.true_target!r}, false_target={self.false_target!r})"
        )


class CycleDetector:
    """Detects cycles in the execution DAG to prevent infinite loops.

    Tracks iteration counts per task and raises when the limit is exceeded.

    Args:
        max_iterations: Maximum allowed executions of any single task.
    """

    def __init__(self, max_iterations: int = 10) -> None:
        self._max_iterations = max_iterations
        self._counts: dict[str, int] = {}

    def record_execution(self, task_id: str) -> None:
        """Record that a task was executed.

        Args:
            task_id: The task that was executed.

        Raises:
            RuntimeError: If the task has exceeded max_iterations.
        """
        self._counts[task_id] = self._counts.get(task_id, 0) + 1
        if self._counts[task_id] > self._max_iterations:
            msg = (
                f"Cycle detected: task {task_id} executed {self._counts[task_id]} times (limit={self._max_iterations})"
            )
            raise RuntimeError(msg)

    def get_count(self, task_id: str) -> int:
        """Return the execution count for a task.

        Args:
            task_id: The task ID to look up.

        Returns:
            Number of times the task has been executed.
        """
        return self._counts.get(task_id, 0)

    def reset(self) -> None:
        """Clear all execution counts."""
        self._counts.clear()


class HumanCheckpoint:
    """Manages human-in-the-loop checkpoints for critical tasks.

    Tasks marked as checkpoints require explicit approval before their
    results are propagated to dependents.
    """

    def __init__(self) -> None:
        self._checkpoints: dict[str, str] = {}  # task_id -> reason
        self._approved: set[str] = set()

    def add_checkpoint(self, task_id: str, reason: str = "") -> None:
        """Mark a task as requiring human review.

        Args:
            task_id: The task to checkpoint.
            reason: Why human review is needed.
        """
        self._checkpoints[task_id] = reason

    def is_checkpoint(self, task_id: str) -> bool:
        """Return True if the task requires human review.

        Args:
            task_id: The task ID to check.

        Returns:
            True if the task is a checkpoint.
        """
        return task_id in self._checkpoints

    def approve(self, task_id: str) -> None:
        """Approve a checkpointed task, allowing execution to continue.

        Args:
            task_id: The task to approve.
        """
        self._approved.add(task_id)

    def is_approved(self, task_id: str) -> bool:
        """Return True if the checkpoint has been approved.

        Args:
            task_id: The task ID to check.

        Returns:
            True if the checkpoint is approved.
        """
        return task_id in self._approved

    def get_pending(self) -> dict[str, str]:
        """Return all checkpoints that have not been approved.

        Returns:
            Dict mapping task_id to reason for unapproved checkpoints.
        """
        return {tid: reason for tid, reason in self._checkpoints.items() if tid not in self._approved}


@dataclass
class TaskNode:
    """A node in the execution DAG."""

    task: Task
    status: StatusEnum = StatusEnum.PENDING
    result: AgentResult | None = None
    dependencies: set[str] = field(default_factory=set)
    dependents: set[str] = field(default_factory=set)
    retries: int = 0
    max_retries: int = 3

    def __repr__(self) -> str:
        return f"TaskNode(task_id={self.task.id!r}, status={self.status.value!r}, retries={self.retries!r})"


@dataclass
class ExecutionDAG:
    """Task DAG with nodes, dependency order, and scheduling state."""

    plan_id: str
    original_plan: ContractsExecutionPlan
    nodes: dict[str, TaskNode] = field(default_factory=dict)
    execution_order: list[str] = field(default_factory=list)
    status: StatusEnum = StatusEnum.PENDING
    started_at: str | None = None
    completed_at: str | None = None

    def __repr__(self) -> str:
        return f"ExecutionDAG(plan_id={self.plan_id!r}, status={self.status.value!r}, nodes={len(self.nodes)})"

    @classmethod
    def create_new(cls, goal: str, phase: int = 0) -> ExecutionDAG:
        """Create a new ExecutionPlan with an auto-generated contracts plan.

        Args:
            goal: Human-readable description of the top-level goal to achieve.
            phase: Execution phase number (0 = initial; increments on replan).

        Returns:
            A new ``ExecutionDAG`` with an auto-generated UUID plan ID and the
            provided goal attached via a ``contracts.ExecutionPlan``, ready for
            task nodes to be populated.
        """
        from vetinari.agents.contracts import ExecutionPlan as _ContractsPlan

        contracts_plan = _ContractsPlan.create_new(goal, phase)
        return cls(plan_id=contracts_plan.plan_id, original_plan=contracts_plan)


@dataclass
class ReplanResult:
    """Result of a mid-execution DAG replanning request (Dept 7.9).

    Attributes:
        new_tasks: The (possibly modified) list of tasks to execute.
        replan_output: The Planner's natural language output describing changes.
    """

    new_tasks: list[Task] = field(default_factory=list)
    replan_output: str = ""

    def __repr__(self) -> str:
        return f"ReplanResult(new_tasks={len(self.new_tasks)})"
