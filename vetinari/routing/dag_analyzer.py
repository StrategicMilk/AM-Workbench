"""DAG Analyzer — structural analysis of task dependency graphs.

Examines a task DAG to understand its shape: depth, parallelism potential,
bottleneck tasks, and suggested execution topology.  Used by the topology
router to select the most efficient execution strategy.

BFS is used for depth/level analysis.  Connected components are found via
union-find to detect isolated sub-graphs that can run fully in parallel.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Thresholds for topology suggestions
_MIN_PARALLEL_TASKS = 3  # minimum independent tasks to suggest parallel topology
_MIN_DEPTH_FOR_SEQUENTIAL = 3  # minimum depth before sequential is preferred over express
_HIGH_FAN_OUT = 4  # fan-out >= this suggests scatter-gather
_HIGH_FAN_IN = 4  # fan-in >= this suggests gather step present


@dataclass(frozen=True, slots=True)
class DAGShape:
    """Structural properties of a task dependency graph.

    Args:
        task_count: Total number of tasks in the graph.
        max_depth: Longest dependency chain length (critical path depth).
        avg_depth: Average depth across all tasks.
        independent_tasks: Tasks with no dependencies.
        max_fan_out: Maximum number of successors from any single task.
        max_fan_in: Maximum number of predecessors into any single task.
        connected_components: Number of disconnected sub-graphs.
        has_bottleneck: True if any task blocks the majority of the graph.
        bottleneck_task_id: The task_id of the identified bottleneck, if any.
        parallelism_potential: Estimated fraction of tasks that can run in parallel.
    """

    task_count: int = 0
    max_depth: int = 0
    avg_depth: float = 0.0
    independent_tasks: int = 0
    max_fan_out: int = 0
    max_fan_in: int = 0
    connected_components: int = 1
    has_bottleneck: bool = False
    bottleneck_task_id: str = ""
    parallelism_potential: float = 0.0

    def __repr__(self) -> str:
        return (
            f"DAGShape(tasks={self.task_count!r}, depth={self.max_depth!r}, "
            f"independent={self.independent_tasks!r}, "
            f"components={self.connected_components!r}, "
            f"parallelism={self.parallelism_potential:.2f})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary.

        Returns:
            Dictionary with all shape analysis fields.
        """
        return {
            "task_count": self.task_count,
            "max_depth": self.max_depth,
            "avg_depth": self.avg_depth,
            "independent_tasks": self.independent_tasks,
            "max_fan_out": self.max_fan_out,
            "max_fan_in": self.max_fan_in,
            "connected_components": self.connected_components,
            "has_bottleneck": self.has_bottleneck,
            "bottleneck_task_id": self.bottleneck_task_id,
            "parallelism_potential": self.parallelism_potential,
        }


def analyze_dag(
    tasks: list[dict[str, Any]],
) -> DAGShape:
    """Analyze the structural shape of a task DAG.

    Returns:
        Value produced for the caller.
    """
    if not tasks:
        return DAGShape()
    task_ids = {t["id"] for t in tasks}
    deps_map: dict[str, list[str]] = {}
    for t in tasks:
        raw_deps = t.get("dependencies", []) or []
        deps_map[t["id"]] = [d for d in raw_deps if d in task_ids]
    successors = _successor_map(task_ids, deps_map)
    max_fan_out = max((len(succs) for succs in successors.values()), default=0)
    max_fan_in = max((len(deps) for deps in deps_map.values()), default=0)
    independent = [tid for tid, deps in deps_map.items() if not deps]
    depths: dict[str, int] = dict.fromkeys(task_ids, 0)
    queue: deque[str] = deque(independent)
    while queue:
        current = queue.popleft()
        for succ in successors[current]:
            new_depth = depths[current] + 1
            if new_depth > depths[succ]:
                depths[succ] = new_depth
                queue.append(succ)
    max_depth = max(depths.values(), default=0)
    avg_depth = sum(depths.values()) / len(depths) if depths else 0.0
    components = _connected_component_count(task_ids, deps_map)
    has_bottleneck, bottleneck_task_id = _bottleneck(successors, max_fan_out)
    parallelism_potential = len(independent) / len(task_ids) if task_ids else 0.0
    logger.debug(
        "[DAGAnalyzer] tasks=%d depth=%d independent=%d components=%d fan_out=%d",
        len(tasks),
        max_depth,
        len(independent),
        components,
        max_fan_out,
    )
    return DAGShape(
        task_count=len(tasks),
        max_depth=max_depth,
        avg_depth=avg_depth,
        independent_tasks=len(independent),
        max_fan_out=max_fan_out,
        max_fan_in=max_fan_in,
        connected_components=components,
        has_bottleneck=has_bottleneck,
        bottleneck_task_id=bottleneck_task_id,
        parallelism_potential=parallelism_potential,
    )


def _successor_map(task_ids: set[str], deps_map: dict[str, list[str]]) -> dict[str, list[str]]:
    successors: dict[str, list[str]] = {tid: [] for tid in task_ids}
    for tid, deps in deps_map.items():
        for dep in deps:
            successors[dep].append(tid)
    return successors


def _connected_component_count(task_ids: set[str], deps_map: dict[str, list[str]]) -> int:
    parent = {tid: tid for tid in task_ids}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for tid, deps in deps_map.items():
        for dep in deps:
            parent[find(tid)] = find(dep)
    return len({find(tid) for tid in task_ids})


def _bottleneck(successors: dict[str, list[str]], max_fan_out: int) -> tuple[bool, str]:
    if max_fan_out < _HIGH_FAN_OUT:
        return False, ""
    for tid, succs in successors.items():
        if len(succs) >= _HIGH_FAN_OUT:
            return True, tid
    return False, ""


def suggest_topology(shape: DAGShape) -> str:
    """Recommend an execution topology string based on DAG shape analysis.

    Decision tree:
    1. Single task or trivial graph → ``"express"``
    2. Many independent tasks (parallelism_potential high) → ``"parallel"``
    3. Multiple disconnected components → ``"scatter_gather"``
    4. High fan-out bottleneck present → ``"scatter_gather"``
    5. Deep sequential chain → ``"sequential"``
    6. Mixed → ``"hierarchical"``

    Args:
        shape: DAGShape from analyze_dag().

    Returns:
        Topology string (one of: express, sequential, parallel,
        hierarchical, scatter_gather).
    """
    if shape.task_count <= 1:
        return "express"

    if shape.connected_components > 1 or shape.max_fan_out >= _HIGH_FAN_OUT:
        return "scatter_gather"

    if shape.parallelism_potential >= 0.5 and shape.independent_tasks >= _MIN_PARALLEL_TASKS:
        return "parallel"

    if shape.max_depth >= _MIN_DEPTH_FOR_SEQUENTIAL and shape.parallelism_potential < 0.3:
        return "sequential"

    if shape.max_depth >= 2 and shape.max_fan_out >= 2:
        return "hierarchical"

    return "sequential"
