"""Typed PlanDiff union for live-plan-edit runtime patches.

Each diff is a frozen value object; ``apply_diff`` is the deterministic
mutation site.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, TypeAlias

from vetinari.orchestration.graph_types import ExecutionDAG, TaskNode
from vetinari.types import StatusEnum

if TYPE_CHECKING:
    from vetinari.agents.contracts import Task
    from vetinari.planning.plan_graph import PlanGraph

__all__ = [
    "AddDependency",
    "AddTask",
    "PlanDiff",
    "PlanRuntimeEditConflict",
    "RemoveDependency",
    "RemoveTask",
    "UpdateTask",
    "apply_diff",
]

_UPDATE_FIELDS = frozenset({"description", "metadata", "assigned_agent"})


@dataclass(frozen=True, slots=True)
class AddTask:
    """Runtime contract for AddTask."""

    task: Task
    after: str | None = None


@dataclass(frozen=True, slots=True)
class RemoveTask:
    """Runtime contract for RemoveTask."""

    task_id: str


@dataclass(frozen=True, slots=True)
class UpdateTask:
    """Runtime contract for UpdateTask."""

    task_id: str
    fields: Mapping[str, Any]
    from_task_id: str | None = None


@dataclass(frozen=True, slots=True)
class AddDependency:
    """Runtime contract for AddDependency."""

    from_task_id: str
    to_task_id: str


@dataclass(frozen=True, slots=True)
class RemoveDependency:
    """Runtime contract for RemoveDependency."""

    from_task_id: str
    to_task_id: str


PlanDiff: TypeAlias = AddTask | RemoveTask | UpdateTask | AddDependency | RemoveDependency


class PlanRuntimeEditConflict(Exception):
    """Raised when a runtime plan edit would mutate an in-flight or immutable task."""

    def __init__(self, task_id: str, status: Any, op: str) -> None:
        self.task_id = task_id
        self.status = status
        self.op = op
        status_name = getattr(status, "name", str(_status_value(status)).upper())
        super().__init__(
            f"runtime-edit conflict: cannot apply {op} to task {task_id} in status "
            f"{status_name} (RUNNING tasks may not be removed/updated; COMPLETED tasks may not be removed)"
        )


def apply_diff(diff: PlanDiff, exec_plan: ExecutionDAG) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply one deterministic live-plan diff to an execution DAG.

    Args:
        diff: Diff value consumed by apply_diff().
        exec_plan: Exec plan value consumed by apply_diff().

    Returns:
        tuple[dict[str, Any], dict[str, Any]] value produced by apply_diff().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if isinstance(diff, AddTask):
        return _apply_add_task(diff, exec_plan)
    if isinstance(diff, RemoveTask):
        return _apply_remove_task(diff, exec_plan)
    if isinstance(diff, UpdateTask):
        return _apply_update_task(diff, exec_plan)
    if isinstance(diff, AddDependency):
        return _apply_add_dependency(diff, exec_plan)
    if isinstance(diff, RemoveDependency):
        return _apply_remove_dependency(diff, exec_plan)
    raise TypeError(f"unsupported PlanDiff type: {type(diff).__name__}")


def _apply_add_task(diff: AddTask, exec_plan: ExecutionDAG) -> tuple[dict[str, Any], dict[str, Any]]:
    task_id = diff.task.id
    if task_id in exec_plan.nodes:
        raise ValueError(f"task {task_id} already present in plan")
    for dep_id in diff.task.dependencies:
        if dep_id not in exec_plan.nodes:
            raise ValueError(f"dependency task {dep_id} not present in plan")

    _validate_width_with_added_task(diff.task, exec_plan)
    diff.task.status = StatusEnum.PENDING
    exec_plan.nodes[task_id] = TaskNode(
        task=diff.task,
        status=StatusEnum.PENDING,
        dependencies=set(diff.task.dependencies),
        dependents=set(),
    )
    for dep_id in diff.task.dependencies:
        exec_plan.nodes[dep_id].dependents.add(task_id)
    _refresh_execution_order(exec_plan)
    return {}, {"task_id": task_id, "task": _task_to_dict(diff.task)}


def _apply_remove_task(diff: RemoveTask, exec_plan: ExecutionDAG) -> tuple[dict[str, Any], dict[str, Any]]:
    target = _require_node(exec_plan, diff.task_id)
    if _status_in(target.status, {StatusEnum.RUNNING, StatusEnum.COMPLETED}):
        raise PlanRuntimeEditConflict(diff.task_id, target.status, "RemoveTask")

    before_state = {
        "task": _task_to_dict(target.task),
        "status": target.status.value,
        "dependencies": sorted(target.dependencies),
        "dependents": sorted(target.dependents),
    }
    for dep_id in list(target.dependencies):
        dep = exec_plan.nodes.get(dep_id)
        if dep is not None:
            dep.dependents.discard(diff.task_id)
    for dependent_id in list(target.dependents):
        dependent = exec_plan.nodes.get(dependent_id)
        if dependent is not None:
            dependent.dependencies.discard(diff.task_id)
            if _status_is(dependent.status, StatusEnum.PENDING) and not dependent.dependencies:
                dependent.status = StatusEnum.READY
                dependent.task.status = StatusEnum.READY
    del exec_plan.nodes[diff.task_id]
    _refresh_execution_order(exec_plan)
    return before_state, {}


def _apply_update_task(diff: UpdateTask, exec_plan: ExecutionDAG) -> tuple[dict[str, Any], dict[str, Any]]:
    target = _require_node(exec_plan, diff.task_id)
    if _status_is(target.status, StatusEnum.RUNNING):
        raise PlanRuntimeEditConflict(diff.task_id, StatusEnum.RUNNING, "UpdateTask")
    for key in diff.fields:
        if key not in _UPDATE_FIELDS:
            raise ValueError(f"UpdateTask field {key} is not in the allow-list")

    before_fields = {key: _json_safe(getattr(target.task, key)) for key in diff.fields}
    for key, value in diff.fields.items():
        setattr(target.task, key, value)
    after_fields = {key: _json_safe(value) for key, value in diff.fields.items()}
    return (
        {"task_id": diff.task_id, "fields": before_fields},
        {"task_id": diff.task_id, "fields": after_fields},
    )


def _apply_add_dependency(diff: AddDependency, exec_plan: ExecutionDAG) -> tuple[dict[str, Any], dict[str, Any]]:
    if diff.from_task_id == diff.to_task_id:
        raise ValueError("dependency self-loop is not allowed")
    from_node = _require_node(exec_plan, diff.from_task_id)
    _require_node(exec_plan, diff.to_task_id)
    if not _status_in(from_node.status, {StatusEnum.PENDING, StatusEnum.READY}):
        raise PlanRuntimeEditConflict(diff.from_task_id, from_node.status, "AddDependency")
    if diff.to_task_id in from_node.dependencies:
        raise ValueError(f"dependency edge {diff.from_task_id} -> {diff.to_task_id} already present")
    if _has_transitive_dependency(exec_plan, start_id=diff.to_task_id, target_id=diff.from_task_id):
        raise ValueError(f"dependency {diff.from_task_id} -> {diff.to_task_id} would introduce a cycle")

    before = {"from_task_id": diff.from_task_id, "to_task_id": diff.to_task_id, "edge_present": False}
    from_node.dependencies.add(diff.to_task_id)
    exec_plan.nodes[diff.to_task_id].dependents.add(diff.from_task_id)
    _refresh_execution_order(exec_plan)
    after = {"from_task_id": diff.from_task_id, "to_task_id": diff.to_task_id, "edge_present": True}
    return before, after


def _apply_remove_dependency(diff: RemoveDependency, exec_plan: ExecutionDAG) -> tuple[dict[str, Any], dict[str, Any]]:
    from_node = _require_node(exec_plan, diff.from_task_id)
    _require_node(exec_plan, diff.to_task_id)
    if diff.to_task_id not in from_node.dependencies:
        raise ValueError(f"dependency edge {diff.from_task_id} -> {diff.to_task_id} not present")
    before = {"from_task_id": diff.from_task_id, "to_task_id": diff.to_task_id, "edge_present": True}
    from_node.dependencies.remove(diff.to_task_id)
    exec_plan.nodes[diff.to_task_id].dependents.discard(diff.from_task_id)
    _refresh_execution_order(exec_plan)
    after = {"from_task_id": diff.from_task_id, "to_task_id": diff.to_task_id, "edge_present": False}
    return before, after


def _require_node(exec_plan: ExecutionDAG, task_id: str) -> TaskNode:
    node = exec_plan.nodes.get(task_id)
    if node is None:
        raise ValueError(f"task {task_id} not present in plan")
    return node


def _status_value(status: Any) -> Any:
    return getattr(status, "value", status)


def _status_is(status: Any, expected: StatusEnum) -> bool:
    return _status_value(status) == expected.value


def _status_in(status: Any, expected: set[StatusEnum]) -> bool:
    value = _status_value(status)
    return any(value == item.value for item in expected)


def _task_to_dict(task: Task) -> dict[str, Any]:
    return _json_safe(asdict(task))


def _json_safe(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _plan_graph(exec_plan: ExecutionDAG) -> PlanGraph:
    from vetinari.planning.plan_graph import PlanGraph

    graph = PlanGraph()
    for task_id, node in exec_plan.nodes.items():
        graph.add_node(task_id)
        for dep_id in sorted(node.dependencies):
            graph.add_edge(task_id, dep_id)
    return graph


def _validate_width_with_added_task(task: Task, exec_plan: ExecutionDAG) -> None:
    graph = _plan_graph(exec_plan)
    graph.add_node(task.id)
    for dep_id in task.dependencies:
        graph.add_edge(task.id, dep_id)
    graph.validate()


def _refresh_execution_order(exec_plan: ExecutionDAG) -> None:
    graph = _plan_graph(exec_plan)
    graph.validate()
    exec_plan.execution_order = graph.topological_sort()


def _has_transitive_dependency(exec_plan: ExecutionDAG, start_id: str, target_id: str) -> bool:
    queue = deque(exec_plan.nodes[start_id].dependencies)
    seen: set[str] = set(queue)
    while queue:
        task_id = queue.popleft()
        if task_id == target_id:
            return True
        for dep_id in exec_plan.nodes[task_id].dependencies:
            if dep_id not in seen:
                seen.add(dep_id)
                queue.append(dep_id)
    return False
