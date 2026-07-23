"""Typed graph-query contracts for cross-object Workbench inspection."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _require_provenance(provenance: dict[str, str], field_name: str = "provenance") -> None:
    if not isinstance(provenance, dict):
        raise ValueError(f"{field_name} must be a dict[str, str]")
    if not provenance.get("source", "").strip():
        raise ValueError(f"{field_name}.source must be non-empty")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in provenance.items()):
        raise ValueError(f"{field_name} must contain string keys and values")


class QueryObjectKind(str, Enum):
    """Object families visible to the Workbench graph-query layer."""

    ASSET = "asset"
    RUN = "run"
    TRACE = "trace"
    EVAL = "eval"
    DATASET = "dataset"
    ANNOTATION = "annotation"
    DIAGNOSIS = "diagnosis"
    PROPOSAL = "proposal"
    LEASE = "lease"
    RECEIPT = "receipt"
    AUTOMATION = "automation"
    PROMOTION = "promotion"
    MONITOR_SIGNAL = "monitor_signal"


class GraphViewId(str, Enum):
    """Built-in saved views operators can request without ad hoc queries."""

    FAILURE_SHARED_SOURCE_REVISION = "failure_shared_source_revision"
    STALE_EVIDENCE_BLOCKED_PROMOTIONS = "stale_evidence_blocked_promotions"
    ROUTE_COST_WITHOUT_QUALITY_GAIN = "route_cost_without_quality_gain"
    AUTOMATION_CHURN_WITHOUT_ADOPTION = "automation_churn_without_adoption"
    FULL_CROSS_OBJECT_GRAPH = "full_cross_object_graph"


@dataclass(frozen=True, slots=True)
class QueryRuntimeObject:
    """Caller-supplied object from dependency packs not persisted in the spine."""

    object_id: str
    kind: QueryObjectKind
    label: str
    revision: str = ""
    status: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    relations: tuple[tuple[str, str], ...] = ()
    provenance: dict[str, str] = field(default_factory=dict)
    confidence: float = 1.0

    def __post_init__(self) -> None:
        _require_non_empty(self.object_id, "object_id")
        _require_non_empty(self.label, "label")
        _require_provenance(self.provenance)
        if not isinstance(self.kind, QueryObjectKind):
            raise ValueError("kind must be QueryObjectKind")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")
        for relation, target_id in self.relations:
            _require_non_empty(relation, "relation")
            _require_non_empty(target_id, "relation target")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"QueryRuntimeObject(object_id={self.object_id!r}, kind={self.kind!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class GraphNode:
    """Normalized object node in the cross-object graph."""

    node_id: str
    kind: QueryObjectKind
    label: str
    revision: str
    status: str
    metadata: dict[str, Any]
    provenance: dict[str, str]
    confidence: float

    def __post_init__(self) -> None:
        _require_non_empty(self.node_id, "node_id")
        _require_non_empty(self.label, "label")
        _require_provenance(self.provenance)
        if not isinstance(self.kind, QueryObjectKind):
            raise ValueError("kind must be QueryObjectKind")
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"GraphNode(node_id={self.node_id!r}, kind={self.kind!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """Directed relationship between two graph nodes."""

    edge_id: str
    source_id: str
    target_id: str
    relation: str
    provenance: dict[str, str]
    confidence: float

    def __post_init__(self) -> None:
        _require_non_empty(self.edge_id, "edge_id")
        _require_non_empty(self.source_id, "source_id")
        _require_non_empty(self.target_id, "target_id")
        _require_non_empty(self.relation, "relation")
        _require_provenance(self.provenance)
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"GraphEdge(edge_id={self.edge_id!r}, source_id={self.source_id!r}, target_id={self.target_id!r})"


@dataclass(frozen=True, slots=True)
class SavedGraphView:
    """Immutable saved-view definition with explicit evidence requirements."""

    view_id: GraphViewId
    name: str
    description: str
    required_kinds: tuple[QueryObjectKind, ...]
    requires_authority: bool
    minimum_confidence: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.view_id, GraphViewId):
            raise ValueError("view_id must be GraphViewId")
        _require_non_empty(self.name, "name")
        _require_non_empty(self.description, "description")
        if not self.required_kinds:
            raise ValueError("required_kinds must be non-empty")
        if not 0.0 <= float(self.minimum_confidence) <= 1.0:
            raise ValueError("minimum_confidence must be between 0.0 and 1.0")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SavedGraphView(view_id={self.view_id!r}, name={self.name!r}, description={self.description!r})"


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    """Graph snapshot returned by runtime query collection."""

    project_id: str
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    saved_views: tuple[SavedGraphView, ...]
    authority_ref: str
    generated_at_utc: str
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.project_id, "project_id")
        _require_non_empty(self.authority_ref, "authority_ref")
        _require_non_empty(self.generated_at_utc, "generated_at_utc")
        node_ids = {node.node_id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("node_id values must be unique")
        for edge in self.edges:
            if edge.source_id not in node_ids or edge.target_id not in node_ids:
                raise ValueError(f"edge {edge.edge_id!r} references missing node")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"GraphSnapshot(project_id={self.project_id!r}, nodes={self.nodes!r}, edges={self.edges!r})"


@dataclass(frozen=True, slots=True)
class GraphQueryResult:
    """Result of applying one saved view to a graph snapshot."""

    view_id: GraphViewId
    matched_node_ids: tuple[str, ...]
    matched_edge_ids: tuple[str, ...]
    summary: str
    requires_operator_review: bool
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.view_id, GraphViewId):
            raise ValueError("view_id must be GraphViewId")
        _require_non_empty(self.summary, "summary")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"GraphQueryResult(view_id={self.view_id!r}, matched_node_ids={self.matched_node_ids!r}, matched_edge_ids={self.matched_edge_ids!r})"


@dataclass(frozen=True, slots=True)
class CrossObjectDiffRequest:
    """Request for comparing two graph snapshots along named dimensions."""

    before: GraphSnapshot
    after: GraphSnapshot
    dimensions: tuple[str, ...]
    authority_ref: str
    provenance: dict[str, str]

    def __post_init__(self) -> None:
        if not self.dimensions:
            raise ValueError("dimensions must be non-empty")
        _require_non_empty(self.authority_ref, "authority_ref")
        _require_provenance(self.provenance)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CrossObjectDiffRequest(before={self.before!r}, after={self.after!r}, dimensions={self.dimensions!r})"


@dataclass(frozen=True, slots=True)
class DiffChange:
    """One field-level before/after change in a cross-object diff."""

    node_id: str
    kind: QueryObjectKind
    field_path: str
    before: Any
    after: Any
    dimension: str
    impact: str

    def __post_init__(self) -> None:
        _require_non_empty(self.node_id, "node_id")
        _require_non_empty(self.field_path, "field_path")
        _require_non_empty(self.dimension, "dimension")
        _require_non_empty(self.impact, "impact")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DiffChange(node_id={self.node_id!r}, kind={self.kind!r}, field_path={self.field_path!r})"


@dataclass(frozen=True, slots=True)
class CrossObjectDiff:
    """Authority-checked diff across prompt/model/dataset/route/policy/recipe objects."""

    changes: tuple[DiffChange, ...]
    summary: str
    authority_ref: str
    provenance: dict[str, str]
    requires_operator_review: bool

    def __post_init__(self) -> None:
        _require_non_empty(self.summary, "summary")
        _require_non_empty(self.authority_ref, "authority_ref")
        _require_provenance(self.provenance)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"CrossObjectDiff(changes={self.changes!r}, summary={self.summary!r}, authority_ref={self.authority_ref!r})"
        )
