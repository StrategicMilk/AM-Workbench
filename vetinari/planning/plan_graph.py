"""Step 2 of Foreman's planning phase: order SpecFrame work into a cycle-free dependency graph."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

MAX_PLAN_WIDTH: int = 5
MAX_PLAN_DEPTH: int = 6
MAX_REDRIVES: int = 3


class CyclicDependencyError(ValueError):
    """Raised when a dependency cycle is detected in a PlanGraph."""


class PlanDepthError(ValueError):
    """Raised when the longest dependency chain exceeds MAX_PLAN_DEPTH."""


class PlanWidthError(ValueError):
    """Raised when a dependency layer exceeds MAX_PLAN_WIDTH."""


@dataclass
class PlanGraph:
    """Dependency graph for planning nodes."""

    nodes: set[str] = field(default_factory=set)
    edges: dict[str, list[str]] = field(default_factory=dict)

    def add_node(self, node_id: str) -> None:
        """Add a node to the graph if it is not already present."""
        self.nodes.add(node_id)
        self.edges.setdefault(node_id, [])

    def add_edge(self, from_node: str, to_node: str) -> None:
        """Add a directed edge where from_node depends on to_node.

        Args:
            from_node: Dependent node id.
            to_node: Dependency node id.
        """
        self.add_node(from_node)
        self.add_node(to_node)
        if to_node not in self.edges[from_node]:
            self.edges[from_node].append(to_node)

    def validate(self) -> None:
        """Validate that the graph is cycle-free and within depth/width limits.

        Raises:
            CyclicDependencyError: If a dependency cycle exists.
            PlanDepthError: If the longest chain exceeds MAX_PLAN_DEPTH.
            PlanWidthError: If any dependency layer exceeds MAX_PLAN_WIDTH.
        """
        order = self.topological_sort()
        dependents = self._dependents()
        depth_by_node = dict.fromkeys(order, 0)
        max_depth = 0
        for node_id in order:
            node_depth = depth_by_node[node_id]
            for dependent in dependents[node_id]:
                depth_by_node[dependent] = max(depth_by_node[dependent], node_depth + 1)
                max_depth = max(max_depth, depth_by_node[dependent])
        if max_depth > MAX_PLAN_DEPTH:
            raise PlanDepthError(f"Plan depth {max_depth} exceeds maximum of {MAX_PLAN_DEPTH}")
        width_by_depth: dict[int, int] = {}
        for depth in depth_by_node.values():
            width_by_depth[depth] = width_by_depth.get(depth, 0) + 1
        max_width = max(width_by_depth.values(), default=0)
        if max_width > MAX_PLAN_WIDTH:
            raise PlanWidthError(f"Plan width {max_width} exceeds maximum of {MAX_PLAN_WIDTH}")

    def topological_sort(self) -> list[str]:
        """Return nodes in dependency-before-dependent topological order.

        Returns:
            Ordered node ids with dependencies before dependents.

        Raises:
            CyclicDependencyError: If a dependency cycle exists.
        """
        in_degree = {node_id: len(self.edges.get(node_id, [])) for node_id in self.nodes}
        dependents = self._dependents()
        ready = deque(sorted(node_id for node_id, degree in in_degree.items() if degree == 0))
        ordered: list[str] = []

        while ready:
            node_id = ready.popleft()
            ordered.append(node_id)
            for dependent in sorted(dependents[node_id]):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    ready.append(dependent)

        if len(ordered) != len(self.nodes):
            cycle_node = min(node_id for node_id, degree in in_degree.items() if degree > 0)
            raise CyclicDependencyError(f"Cycle detected involving node: {cycle_node}")
        return ordered

    def _dependents(self) -> dict[str, list[str]]:
        dependents = {node_id: [] for node_id in self.nodes}
        for dependent, dependencies in self.edges.items():
            for dependency in dependencies:
                dependents.setdefault(dependency, []).append(dependent)
            dependents.setdefault(dependent, [])
        return dependents


__all__ = [
    "MAX_PLAN_DEPTH",
    "MAX_PLAN_WIDTH",
    "MAX_REDRIVES",
    "CyclicDependencyError",
    "PlanDepthError",
    "PlanGraph",
    "PlanWidthError",
]
