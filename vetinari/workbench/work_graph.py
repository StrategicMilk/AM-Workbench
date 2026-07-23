"""Deterministic Workbench work graph and score-component map.

The work graph is an inspectable rebuild surface, not a project-management
authority. Callers provide source-shaped records and receive immutable graph
snapshots with transparent score components. This module performs no I/O and
registers no module-level state.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from vetinari.workbench.work_graph_algorithms import (
    _coerce_edge,
    _coerce_node,
    _coerce_source,
    _connected_components,
    _has_dependency_cycle,
    _normalize_edges,
    _priority_scores,
    _reason_from_exception,
    _require_text,
)
from vetinari.workbench.work_graph_models import (
    SUPPORTED_PRIORITY_POLICY,
    WorkbenchGraphQueryService,
    WorkGraphComponent,
    WorkGraphEdge,
    WorkGraphEdgeInput,
    WorkGraphEdgeKind,
    WorkGraphNode,
    WorkGraphNodeKind,
    WorkGraphPriorityScore,
    WorkGraphReason,
    WorkGraphResult,
    WorkGraphSnapshot,
    WorkGraphSource,
    WorkGraphStatus,
)

logger = logging.getLogger(__name__)


def rebuild_work_graph(
    *,
    graph_id: str,
    sources: Sequence[WorkGraphSource | Mapping[str, Any]],
    nodes: Sequence[WorkGraphNode | Mapping[str, Any]],
    edges: Sequence[WorkGraphEdgeInput | Mapping[str, Any]],
    priority_policy: str = SUPPORTED_PRIORITY_POLICY,
) -> WorkGraphResult:
    """Validate and rebuild a deterministic work graph snapshot.

    Returns:
        WorkGraphResult value produced by rebuild_work_graph().
    """
    if priority_policy != SUPPORTED_PRIORITY_POLICY:
        return _blocked(WorkGraphReason.UNSUPPORTED_PRIORITY_POLICY, priority_policy)
    try:
        normalized_sources = tuple(_coerce_source(source) for source in sources)
        normalized_nodes = tuple(_coerce_node(node) for node in nodes)
        normalized_edge_inputs = tuple(_coerce_edge(edge) for edge in edges)
        _require_text(graph_id, "graph_id")
    except (ValueError, KeyError) as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return _blocked(_reason_from_exception(exc), str(exc))

    source_by_id = {source.source_id: source for source in normalized_sources}
    if len(source_by_id) != len(normalized_sources):
        return _blocked(WorkGraphReason.DUPLICATE_NODE, "duplicate-source")
    for source in normalized_sources:
        if source.stale:
            return _blocked(WorkGraphReason.STALE_SOURCE, source.source_id)
        if not source.readable:
            return _blocked(WorkGraphReason.UNREADABLE_SOURCE, source.source_id)

    node_ids = [node.node_id for node in normalized_nodes]
    if len(set(node_ids)) != len(node_ids):
        return _blocked(WorkGraphReason.DUPLICATE_NODE, "duplicate-node")
    for node in normalized_nodes:
        if node.source_id not in source_by_id:
            return _blocked(WorkGraphReason.UNREADABLE_SOURCE, node.source_id)
        if not node.provenance_refs:
            return _blocked(WorkGraphReason.MISSING_PROVENANCE, node.node_id)

    normalized_edges = _normalize_edges(normalized_edge_inputs, set(node_ids), source_by_id)
    if isinstance(normalized_edges, WorkGraphResult):
        return normalized_edges
    if _has_dependency_cycle(normalized_edges):
        return _blocked(WorkGraphReason.CYCLE_DETECTED, "blocks-cycle")

    sorted_nodes = tuple(sorted(normalized_nodes, key=lambda node: node.node_id))
    sorted_edges = tuple(
        sorted(
            normalized_edges,
            key=lambda edge: (edge.kind.value, edge.source_node_id, edge.target_node_id, edge.edge_id),
        )
    )
    components = _connected_components(sorted_nodes, sorted_edges)
    scores = _priority_scores(sorted_nodes, sorted_edges, components)
    return WorkGraphResult(
        status=WorkGraphStatus.SUCCEEDED,
        reasons=("graph-rebuilt",),
        snapshot=WorkGraphSnapshot(
            graph_id=graph_id,
            nodes=sorted_nodes,
            edges=sorted_edges,
            components=components,
            priority_scores=scores,
        ),
    )


def _blocked(reason: WorkGraphReason, record: str) -> WorkGraphResult:
    return WorkGraphResult(
        status=WorkGraphStatus.BLOCKED,
        reasons=(reason.value,),
        rejected_records=(record,),
    )


__all__ = [
    "SUPPORTED_PRIORITY_POLICY",
    "WorkGraphComponent",
    "WorkGraphEdge",
    "WorkGraphEdgeInput",
    "WorkGraphEdgeKind",
    "WorkGraphNode",
    "WorkGraphNodeKind",
    "WorkGraphPriorityScore",
    "WorkGraphReason",
    "WorkGraphResult",
    "WorkGraphSnapshot",
    "WorkGraphSource",
    "WorkGraphStatus",
    "WorkbenchGraphQueryService",
    "rebuild_work_graph",
]
