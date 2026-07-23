"""Lineage edges joining source cards, connectors, quality reports, and datasets."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from vetinari.workbench.data_quality import DataQualityReport, evaluate_data_quality_report


class LineageError(ValueError):
    """Raised when lineage state is missing, damaged, or untrusted."""


class LineageNodeKind(str, Enum):
    """Node kinds in the connector quality lineage graph."""

    SOURCE_CARD = "source_card"
    CONNECTOR = "connector"
    DATASET_REVISION = "dataset_revision"
    QUALITY_REPORT = "quality_report"


class LineageEdgeKind(str, Enum):
    """Directed relation between lineage nodes."""

    INGESTED_BY = "ingested_by"
    PRODUCED_REVISION = "produced_revision"
    QUALITY_EVALUATED = "quality_evaluated"


@dataclass(frozen=True, slots=True)
class LineageNode:
    """One node in a lineage graph."""

    node_id: str
    kind: LineageNodeKind

    def __post_init__(self) -> None:
        _require_non_empty(self.node_id, "node_id")
        if not isinstance(self.kind, LineageNodeKind):
            raise LineageError("kind must be a LineageNodeKind")


@dataclass(frozen=True, slots=True)
class LineageEdge:
    """One evidence-bearing lineage edge."""

    edge_id: str
    edge_kind: LineageEdgeKind
    from_node_id: str
    to_node_id: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.edge_id, "edge_id")
        if not isinstance(self.edge_kind, LineageEdgeKind):
            raise LineageError("edge_kind must be a LineageEdgeKind")
        _require_non_empty(self.from_node_id, "from_node_id")
        _require_non_empty(self.to_node_id, "to_node_id")
        _require_non_empty(self.evidence_ref, "evidence_ref")
        if self.from_node_id == self.to_node_id:
            raise LineageError("lineage edge cannot point to itself")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"LineageEdge(edge_id={self.edge_id!r}, edge_kind={self.edge_kind!r}, from_node_id={self.from_node_id!r})"
        )


@dataclass(frozen=True, slots=True)
class LineageGraph:
    """Immutable lineage graph for one connector-fed dataset revision."""

    graph_id: str
    nodes: tuple[LineageNode, ...]
    edges: tuple[LineageEdge, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.graph_id, "graph_id")
        if not isinstance(self.nodes, tuple) or not self.nodes:
            raise LineageError("nodes must be a non-empty tuple")
        if not isinstance(self.edges, tuple) or not self.edges:
            raise LineageError("edges must be a non-empty tuple")
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise LineageError("lineage graph contains duplicate node_id values")
        known = set(node_ids)
        for edge in self.edges:
            if edge.from_node_id not in known or edge.to_node_id not in known:
                raise LineageError(f"edge {edge.edge_id!r} references absent node")


def build_connector_lineage_graph(
    *,
    source_card_id: str,
    connector_id: str,
    dataset_revision_id: str,
    quality_report: DataQualityReport,
    evidence_ref: str,
) -> LineageGraph:
    """Build a complete lineage graph or reject damaged/untrusted state.

    Returns:
        Newly constructed connector lineage graph value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    _require_non_empty(source_card_id, "source_card_id")
    _require_non_empty(connector_id, "connector_id")
    _require_non_empty(dataset_revision_id, "dataset_revision_id")
    _require_non_empty(evidence_ref, "evidence_ref")
    if quality_report.dataset_revision_id != dataset_revision_id:
        raise LineageError("quality report dataset_revision_id does not match lineage dataset revision")
    if quality_report.connector_id != connector_id:
        raise LineageError("quality report connector_id does not match lineage connector")
    if quality_report.source_card_id != source_card_id:
        raise LineageError("quality report source_card_id does not match lineage source card")
    verdict = evaluate_data_quality_report(quality_report)
    if not verdict.passed:
        raise LineageError("; ".join(verdict.rejection_reasons))

    quality_report_id = quality_report.quality_report_id
    return LineageGraph(
        graph_id=f"lineage:{dataset_revision_id}",
        nodes=(
            LineageNode(source_card_id, LineageNodeKind.SOURCE_CARD),
            LineageNode(connector_id, LineageNodeKind.CONNECTOR),
            LineageNode(dataset_revision_id, LineageNodeKind.DATASET_REVISION),
            LineageNode(quality_report_id, LineageNodeKind.QUALITY_REPORT),
        ),
        edges=(
            LineageEdge(
                edge_id=f"{source_card_id}->{connector_id}",
                edge_kind=LineageEdgeKind.INGESTED_BY,
                from_node_id=source_card_id,
                to_node_id=connector_id,
                evidence_ref=evidence_ref,
            ),
            LineageEdge(
                edge_id=f"{connector_id}->{dataset_revision_id}",
                edge_kind=LineageEdgeKind.PRODUCED_REVISION,
                from_node_id=connector_id,
                to_node_id=dataset_revision_id,
                evidence_ref=evidence_ref,
            ),
            LineageEdge(
                edge_id=f"{dataset_revision_id}->{quality_report_id}",
                edge_kind=LineageEdgeKind.QUALITY_EVALUATED,
                from_node_id=dataset_revision_id,
                to_node_id=quality_report_id,
                evidence_ref=quality_report.policy_ref,
            ),
        ),
    )


def assert_lineage_allows_dataset_consumption(graph: LineageGraph, *, dataset_revision_id: str) -> None:
    """Raise unless lineage includes the required source->connector->dataset->quality chain.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    _require_non_empty(dataset_revision_id, "dataset_revision_id")
    nodes = {node.node_id: node.kind for node in graph.nodes}
    if nodes.get(dataset_revision_id) is not LineageNodeKind.DATASET_REVISION:
        raise LineageError(f"dataset revision {dataset_revision_id!r} is absent from lineage graph")
    edge_pairs = {(edge.edge_kind, edge.from_node_id, edge.to_node_id) for edge in graph.edges}
    source_to_connector = any(edge[0] is LineageEdgeKind.INGESTED_BY for edge in edge_pairs)
    connector_to_dataset = any(
        edge_kind is LineageEdgeKind.PRODUCED_REVISION and to_node_id == dataset_revision_id
        for edge_kind, _from_node_id, to_node_id in edge_pairs
    )
    dataset_to_quality = any(
        edge_kind is LineageEdgeKind.QUALITY_EVALUATED and from_node_id == dataset_revision_id
        for edge_kind, from_node_id, _to_node_id in edge_pairs
    )
    if not (source_to_connector and connector_to_dataset and dataset_to_quality):
        raise LineageError("lineage graph does not prove source->connector->dataset->quality chain")


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise LineageError(f"{field_name} must be non-empty")


__all__ = [
    "LineageEdge",
    "LineageEdgeKind",
    "LineageError",
    "LineageGraph",
    "LineageNode",
    "LineageNodeKind",
    "assert_lineage_allows_dataset_consumption",
    "build_connector_lineage_graph",
]
