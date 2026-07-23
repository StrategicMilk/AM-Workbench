"""Private algorithms for deterministic Workbench graph rebuilds."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from typing import Any

from vetinari.utils.bounded_collections import BoundedList
from vetinari.workbench.work_graph_models import (
    WorkGraphComponent,
    WorkGraphEdge,
    WorkGraphEdgeInput,
    WorkGraphEdgeKind,
    WorkGraphNode,
    WorkGraphNodeKind,
    WorkGraphPriorityScore,
    WorkGraphReason,
    WorkGraphResult,
    WorkGraphSource,
)


def _normalize_edges(
    edges: tuple[WorkGraphEdgeInput, ...], node_ids: set[str], source_by_id: Mapping[str, WorkGraphSource]
) -> tuple[WorkGraphEdge, ...] | WorkGraphResult:
    from vetinari.workbench.work_graph import _blocked

    normalized: dict[tuple[str, str, WorkGraphEdgeKind], WorkGraphEdge] = {}
    raw_seen: set[tuple[str, str, WorkGraphEdgeKind]] = set()
    for edge in edges:
        raw_key = (edge.source_node_id, edge.target_node_id, edge.kind)
        if raw_key in raw_seen:
            return _blocked(WorkGraphReason.DUPLICATE_EDGE, edge.edge_id or repr(raw_key))
        raw_seen.add(raw_key)
        if edge.source_node_id == edge.target_node_id:
            return _blocked(WorkGraphReason.SELF_EDGE, edge.source_node_id)
        if edge.source_node_id not in node_ids or edge.target_node_id not in node_ids:
            return _blocked(WorkGraphReason.UNKNOWN_NODE, edge.edge_id or edge.source_node_id)
        if edge.source_id not in source_by_id:
            return _blocked(WorkGraphReason.UNREADABLE_SOURCE, edge.source_id)
        if not edge.provenance_refs:
            return _blocked(WorkGraphReason.MISSING_PROVENANCE, edge.edge_id or edge.source_node_id)
        if edge.kind is WorkGraphEdgeKind.DEPENDS_ON:
            source_id, target_id, kind = edge.target_node_id, edge.source_node_id, WorkGraphEdgeKind.BLOCKS
            metadata = {**edge.metadata, "normalized_from": WorkGraphEdgeKind.DEPENDS_ON.value}
        else:
            source_id, target_id, kind, metadata = edge.source_node_id, edge.target_node_id, edge.kind, edge.metadata
        normalized.setdefault(
            (source_id, target_id, kind),
            WorkGraphEdge(
                edge.edge_id or f"{kind.value}:{source_id}->{target_id}",
                source_id,
                target_id,
                kind,
                edge.source_id,
                edge.provenance_refs,
                metadata,
            ),
        )
    return tuple(normalized.values())


def _connected_components(
    nodes: tuple[WorkGraphNode, ...], edges: tuple[WorkGraphEdge, ...]
) -> tuple[WorkGraphComponent, ...]:
    adjacency: dict[str, set[str]] = {node.node_id: set() for node in nodes}
    for edge in edges:
        adjacency[edge.source_node_id].add(edge.target_node_id)
        adjacency[edge.target_node_id].add(edge.source_node_id)
    visited: set[str] = set()
    components = BoundedList[WorkGraphComponent](max(1, len(nodes)))
    for node in nodes:
        if node.node_id in visited:
            continue
        queue = deque([node.node_id], maxlen=max(1, len(nodes)))
        visited.add(node.node_id)
        member_ids = BoundedList[str](max(1, len(nodes)))
        while queue:
            current = queue.popleft()
            member_ids.append(current)
            for adjacent in sorted(adjacency[current]):
                if adjacent not in visited:
                    visited.add(adjacent)
                    queue.append(adjacent)
        sorted_members = tuple(sorted(member_ids))
        components.append(WorkGraphComponent(f"component:{sorted_members[0]}", sorted_members))
    return tuple(sorted(components, key=lambda component: component.component_id))


def _priority_scores(
    nodes: tuple[WorkGraphNode, ...], edges: tuple[WorkGraphEdge, ...], components: tuple[WorkGraphComponent, ...]
) -> tuple[WorkGraphPriorityScore, ...]:
    component_by_node = {node_id: component.component_id for component in components for node_id in component.node_ids}
    all_adjacency: dict[str, set[str]] = defaultdict(set)
    blocks_adjacency: dict[str, set[str]] = defaultdict(set)
    evidence_counts: dict[str, int] = defaultdict(int)
    for edge in edges:
        all_adjacency[edge.source_node_id].add(edge.target_node_id)
        if edge.kind is WorkGraphEdgeKind.BLOCKS:
            blocks_adjacency[edge.source_node_id].add(edge.target_node_id)
        if edge.kind in {WorkGraphEdgeKind.REPLAYED_BY_AUTOMATION, WorkGraphEdgeKind.EVIDENCED_BY_EVAL}:
            evidence_counts[edge.source_node_id] += 1
            evidence_counts[edge.target_node_id] += 1
    scores = BoundedList[WorkGraphPriorityScore](max(1, len(nodes)))
    for node in nodes:
        node_evidence = evidence_counts[node.node_id]
        if node.kind in {
            WorkGraphNodeKind.AUTOMATION_ASSET,
            WorkGraphNodeKind.EVAL_FAILURE,
            WorkGraphNodeKind.RUN_RECORD,
        }:
            node_evidence += 1
        scores.append(
            WorkGraphPriorityScore(
                node.node_id,
                component_by_node[node.node_id],
                len(_reachable_from(node.node_id, all_adjacency)),
                len(_reachable_from(node.node_id, blocks_adjacency)),
                len(node.stale_evidence_refs),
                node_evidence,
            )
        )
    return tuple(
        sorted(
            scores,
            key=lambda score: (
                -score.transitive_fanout,
                -score.blocked_downstream_count,
                -score.replay_eval_evidence_count,
                score.node_id,
            ),
        )
    )


def _reachable_from(start: str, adjacency: Mapping[str, set[str]]) -> set[str]:
    seen: set[str] = set()
    queue = deque(
        sorted(adjacency.get(start, ())),
        maxlen=max(1, len(adjacency) + sum(len(children) for children in adjacency.values())),
    )
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(sorted(adjacency.get(current, set()) - seen))
    return seen


def _has_dependency_cycle(edges: tuple[WorkGraphEdge, ...]) -> bool:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.kind is WorkGraphEdgeKind.BLOCKS:
            adjacency[edge.source_node_id].add(edge.target_node_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for child in adjacency.get(node_id, ()):
            if visit(child):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in sorted(adjacency))


def _coerce_source(value: WorkGraphSource | Mapping[str, Any]) -> WorkGraphSource:
    return value if isinstance(value, WorkGraphSource) else WorkGraphSource.from_mapping(value)


def _coerce_node(value: WorkGraphNode | Mapping[str, Any]) -> WorkGraphNode:
    return value if isinstance(value, WorkGraphNode) else WorkGraphNode.from_mapping(value)


def _coerce_edge(value: WorkGraphEdgeInput | Mapping[str, Any]) -> WorkGraphEdgeInput:
    try:
        return value if isinstance(value, WorkGraphEdgeInput) else WorkGraphEdgeInput.from_mapping(value)
    except ValueError as exc:
        raise ValueError(WorkGraphReason.INVALID_EDGE_KIND.value) from exc


def _reason_from_exception(exc: Exception) -> WorkGraphReason:
    text = str(exc)
    if WorkGraphReason.INVALID_EDGE_KIND.value in text:
        return WorkGraphReason.INVALID_EDGE_KIND
    if "provenance" in text:
        return WorkGraphReason.MISSING_PROVENANCE
    return WorkGraphReason.UNREADABLE_SOURCE


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _string_tuple(values: Iterable[Any], field_name: str) -> tuple[str, ...]:
    normalized = tuple(str(value) for value in values)
    if not normalized or any(not value.strip() for value in normalized):
        raise ValueError(f"{field_name} must contain non-empty strings")
    return normalized
