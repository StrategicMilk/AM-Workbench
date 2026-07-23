"""Causal graph helpers for root cause analysis."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CausalEdge:
    """A directed cause-effect relationship in the causal graph.

    Attributes:
        cause: The upstream cause node identifier.
        effect: The downstream effect node identifier.
        strength: Confidence in this causal link (0.0-1.0).
        evidence: What supports this causal link.
    """

    cause: str
    effect: str
    strength: float = 0.8
    evidence: str = ""

    def __repr__(self) -> str:
        return "CausalEdge(...)"


class CausalGraph:
    """Directed acyclic graph of cause-effect relationships for failure analysis.

    Builds a graph where nodes are failure events or conditions and edges
    represent causal links. Walking the graph from a symptom backwards
    through causes finds the deepest root cause.
    """

    def __init__(self) -> None:
        # All edges in the graph.
        # Written by: add_edge(), build_from_failures().
        # Read by: get_root_causes(), get_all_paths().
        self._edges: list[CausalEdge] = []

        # Inverted adjacency: effect -> list of CausalEdge where that node is the effect.
        # This lets us walk backwards from symptom to cause efficiently.
        self._adjacency: dict[str, list[CausalEdge]] = {}

        # Isolated node IDs — failures with no caused_by and no edges.
        # These are genuine standalone root causes that have no predecessor.
        # Without tracking them, get_root_causes() would return an empty list
        # for a single failure that has no causal chain.
        # Written by: build_from_failures(). Read by: get_root_causes().
        self._isolated_nodes: set[str] = set()

    def add_edge(self, cause: str, effect: str, strength: float = 0.8, evidence: str = "") -> None:
        """Add a directed causal link from *cause* to *effect*.

        Args:
            cause: Upstream node identifier.
            effect: Downstream node identifier.
            strength: Confidence in the causal link (0.0-1.0).
            evidence: Human-readable description supporting this link.

        Raises:
            ValueError: If cause and effect are the same node (self-loop).
        """
        if cause == effect:
            raise ValueError(f"Self-loop not allowed: {cause!r}")
        edge = CausalEdge(cause=cause, effect=effect, strength=strength, evidence=evidence)
        self._edges.append(edge)
        self._adjacency.setdefault(effect, []).append(edge)
        logger.debug("Causal edge added: %s -> %s (strength=%.2f)", cause, effect, strength)

    def build_from_failures(self, failures: list[dict[str, Any]]) -> None:
        """Construct the causal graph from a list of failure records.

        Each failure dict should have:
        - ``"id"`` (str): unique failure identifier
        - ``"caused_by"`` (str, optional): id of the failure that caused this one
        - ``"evidence"`` (str, optional): description of the causal link
        - ``"category"`` (str, optional): defect category for context

        Args:
            failures: List of failure dictionaries.
        """
        for failure in failures:
            failure_id = failure.get("id", "")
            if not failure_id:
                continue
            caused_by = failure.get("caused_by")
            if caused_by and caused_by != failure_id:
                self.add_edge(
                    cause=caused_by,
                    effect=failure_id,
                    evidence=failure.get("evidence", ""),
                )
            else:
                # No caused_by — this failure has no known predecessor and is
                # itself a root cause. Track it so get_root_causes() includes it.
                self._isolated_nodes.add(failure_id)

    def walk_to_root_cause(self, symptom: str) -> list[str]:
        """Walk backwards from *symptom* through causes to the deepest root cause.

        At each step, follows the highest-strength incoming edge. Uses
        cycle detection to prevent infinite loops in malformed graphs.

        Args:
            symptom: The node to start walking from.

        Returns:
            Path from symptom to root cause (symptom first, root cause last).
            Returns ``[symptom]`` if the node has no known causes.
        """
        path = [symptom]
        visited: set[str] = {symptom}
        current = symptom

        while True:
            incoming = self._adjacency.get(current, [])
            if not incoming:
                break
            # Follow highest-strength cause
            best = max(incoming, key=lambda e: e.strength)
            if best.cause in visited:
                logger.warning("Cycle detected in causal graph at %s — stopping walk", best.cause)
                break
            visited.add(best.cause)
            path.append(best.cause)
            current = best.cause

        return path

    def get_root_causes(self) -> list[str]:
        """Return all nodes that are causes but never effects (source nodes).

        Includes isolated failures — those with no caused_by and no edges —
        because a standalone failure is its own root cause.

        Returns:
            Sorted list of root cause node identifiers.
        """
        all_causes = {e.cause for e in self._edges}
        all_effects = {e.effect for e in self._edges}
        # Chain root causes (causes with no incoming edge) plus isolated nodes
        # (standalone failures that never appear in any edge).
        return sorted((all_causes - all_effects) | self._isolated_nodes)

    def get_all_paths(self, symptom: str) -> list[list[str]]:
        """Return all paths from *symptom* to root causes.

        Useful when a symptom has multiple independent cause chains.

        Args:
            symptom: The node to start from.

        Returns:
            List of paths, each a list of node IDs from symptom to root cause.
        """
        results: list[list[str]] = []
        self._dfs_all_paths(symptom, [symptom], set(), results)
        return results or [[symptom]]

    def _dfs_all_paths(
        self,
        node: str,
        current_path: list[str],
        visited: set[str],
        results: list[list[str]],
    ) -> None:
        """Recursive DFS to enumerate all paths to root causes."""
        incoming = self._adjacency.get(node, [])
        if not incoming:
            results.append(list(current_path))
            return

        for edge in incoming:
            if edge.cause not in visited:
                visited.add(edge.cause)
                current_path.append(edge.cause)
                self._dfs_all_paths(edge.cause, current_path, visited, results)
                current_path.pop()
                visited.discard(edge.cause)


def build_causal_graph(failures: list[dict[str, Any]]) -> CausalGraph:
    """Build a CausalGraph from a list of failure records.

    Args:
        failures: List of failure dicts with ``"id"``, ``"caused_by"``,
            and optional ``"evidence"`` keys.

    Returns:
        A populated CausalGraph ready for root cause analysis.
    """
    graph = CausalGraph()
    graph.build_from_failures(failures)
    return graph


def walk_graph_for_root_cause(graph: CausalGraph, symptom: str) -> str | None:
    """Walk the causal graph to find the deepest root cause of a symptom.

    Args:
        graph: A populated CausalGraph.
        symptom: The failure node to trace backwards from.

    Returns:
        The root cause node ID, or None if symptom is not in the graph.
    """
    # Check if symptom appears anywhere in the graph (as cause or effect)
    all_nodes = {e.cause for e in graph._edges} | {e.effect for e in graph._edges}
    if symptom not in all_nodes:
        return None
    path = graph.walk_to_root_cause(symptom)
    if not path:
        return None
    return path[-1]
