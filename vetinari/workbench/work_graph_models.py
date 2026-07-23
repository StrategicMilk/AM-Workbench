"""Work graph records, enums, and query service."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _string_tuple(values: Iterable[Any], field_name: str) -> tuple[str, ...]:
    result = tuple(str(value).strip() for value in values if str(value).strip())
    if not result:
        raise ValueError(f"{field_name} must not be empty")
    return result


class WorkGraphStatus(str, Enum):
    """Result status for graph rebuilds."""

    SUCCEEDED = "succeeded"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class WorkGraphNodeKind(str, Enum):
    """Node families in the native Workbench work graph."""

    ROADMAP_ITEM = "roadmap_item"
    AUTOMATION_ASSET = "automation_asset"
    IMPROVEMENT_PROPOSAL = "improvement_proposal"
    EVAL_FAILURE = "eval_failure"
    KNOWLEDGE_BACKFEED_ITEM = "knowledge_backfeed_item"
    RUN_RECORD = "run_record"


class WorkGraphEdgeKind(str, Enum):
    """Relationship vocabulary accepted by the work graph."""

    DEPENDS_ON = "depends-on"
    BLOCKS = "blocks"
    SHARED_REFERENCE = "shared-reference"
    CONCEPT = "concept"
    PRODUCED_BY_RUN = "produced-by-run"
    REPLAYED_BY_AUTOMATION = "replayed-by-automation"
    EVIDENCED_BY_EVAL = "evidenced-by-eval"


class WorkGraphReason(str, Enum):
    """Fail-closed validation and degradation reason vocabulary."""

    MISSING_PROVENANCE = "missing-provenance"
    STALE_SOURCE = "stale-source"
    UNKNOWN_NODE = "unknown-node"
    DUPLICATE_NODE = "duplicate-node"
    DUPLICATE_EDGE = "duplicate-edge"
    SELF_EDGE = "self-edge"
    INVALID_EDGE_KIND = "invalid-edge-kind"
    CYCLE_DETECTED = "cycle-detected"
    UNREADABLE_SOURCE = "unreadable-source"
    UNSUPPORTED_PRIORITY_POLICY = "unsupported-priority-policy"


SUPPORTED_PRIORITY_POLICY = "score-components"


class WorkbenchGraphQueryService:
    """Batched metadata-spine lookup service for graph traversal."""

    def __init__(self, spine: Any) -> None:
        self._spine = spine

    def snapshot(self, kind: str, project_ids: Sequence[str]) -> tuple[dict[str, Any], ...]:
        """Return metadata spine payloads for many project ids in one query.

        Args:
            kind: Spine record kind to select.
            project_ids: Project ids to fetch in one batch.

        Returns:
            Matching spine payload rows ordered newest-first.

        Raises:
            ValueError: If the spine does not expose ``list_record_payloads``.
        """
        project_batch = tuple(dict.fromkeys(project_ids))
        if not project_batch:
            return ()
        if not hasattr(self._spine, "list_record_payloads"):
            raise ValueError("spine must expose list_record_payloads(kind)")
        project_set = set(project_batch)
        rows = [
            dict(payload)
            for payload in self._spine.list_record_payloads(kind)
            if str(payload.get("project_id", "")) in project_set
        ]
        return tuple(sorted(rows, key=_payload_sort_key, reverse=True))


def _payload_sort_key(payload: Mapping[str, Any]) -> tuple[str, str]:
    timestamp = (
        payload.get("created_at_utc")
        or payload.get("captured_at_utc")
        or payload.get("started_at_utc")
        or payload.get("opened_at_utc")
        or ""
    )
    return (str(timestamp), str(payload.get("record_id", "")))


@dataclass(frozen=True, slots=True)
class WorkGraphSource:
    """Source record freshness and provenance gate for graph inputs."""

    source_id: str
    provenance_ref: str
    stale: bool = False
    readable: bool = True

    def __post_init__(self) -> None:
        _require_text(self.source_id, "source_id")
        _require_text(self.provenance_ref, "provenance_ref")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> WorkGraphSource:
        return cls(
            source_id=str(payload.get("source_id", "")),
            provenance_ref=str(payload.get("provenance_ref", "")),
            stale=bool(payload.get("stale", False)),
            readable=bool(payload.get("readable", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkGraphSource(source_id={self.source_id!r}, provenance_ref={self.provenance_ref!r}, stale={self.stale!r})"


@dataclass(frozen=True, slots=True)
class WorkGraphNode:
    """One validated work item or evidence object."""

    node_id: str
    kind: WorkGraphNodeKind
    label: str
    source_id: str
    provenance_refs: tuple[str, ...]
    stale_evidence_refs: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.node_id, "node_id")
        _require_text(self.label, "label")
        _require_text(self.source_id, "source_id")
        object.__setattr__(self, "kind", WorkGraphNodeKind(self.kind))
        object.__setattr__(self, "provenance_refs", _string_tuple(self.provenance_refs, "provenance_refs"))
        object.__setattr__(self, "stale_evidence_refs", tuple(str(ref) for ref in self.stale_evidence_refs))
        object.__setattr__(self, "metadata", {str(key): str(value) for key, value in self.metadata.items()})

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> WorkGraphNode:
        return cls(
            node_id=str(payload.get("node_id", "")),
            kind=WorkGraphNodeKind(str(payload.get("kind", ""))),
            label=str(payload.get("label", "")),
            source_id=str(payload.get("source_id", "")),
            provenance_refs=tuple(str(ref) for ref in payload.get("provenance_refs", ())),
            stale_evidence_refs=tuple(str(ref) for ref in payload.get("stale_evidence_refs", ())),
            metadata={str(key): str(value) for key, value in dict(payload.get("metadata", {})).items()},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "label": self.label,
            "source_id": self.source_id,
            "provenance_refs": list(self.provenance_refs),
            "stale_evidence_refs": list(self.stale_evidence_refs),
            "metadata": dict(self.metadata),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkGraphNode(node_id={self.node_id!r}, kind={self.kind!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class WorkGraphEdgeInput:
    """Caller-provided edge before depends-on/blocks normalization."""

    source_node_id: str
    target_node_id: str
    kind: WorkGraphEdgeKind | str
    source_id: str
    provenance_refs: tuple[str, ...]
    edge_id: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.source_node_id, "source_node_id")
        _require_text(self.target_node_id, "target_node_id")
        _require_text(self.source_id, "source_id")
        object.__setattr__(self, "kind", WorkGraphEdgeKind(self.kind))
        object.__setattr__(self, "provenance_refs", _string_tuple(self.provenance_refs, "provenance_refs"))
        object.__setattr__(self, "metadata", {str(key): str(value) for key, value in self.metadata.items()})

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> WorkGraphEdgeInput:
        return cls(
            edge_id=str(payload.get("edge_id", "")),
            source_node_id=str(payload.get("source_node_id", "")),
            target_node_id=str(payload.get("target_node_id", "")),
            kind=str(payload.get("kind", "")),
            source_id=str(payload.get("source_id", "")),
            provenance_refs=tuple(str(ref) for ref in payload.get("provenance_refs", ())),
            metadata={str(key): str(value) for key, value in dict(payload.get("metadata", {})).items()},
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkGraphEdgeInput(source_node_id={self.source_node_id!r}, target_node_id={self.target_node_id!r}, kind={self.kind!r})"


@dataclass(frozen=True, slots=True)
class WorkGraphEdge:
    """Normalized graph edge."""

    edge_id: str
    source_node_id: str
    target_node_id: str
    kind: WorkGraphEdgeKind
    source_id: str
    provenance_refs: tuple[str, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "kind": self.kind.value,
            "source_id": self.source_id,
            "provenance_refs": list(self.provenance_refs),
            "metadata": dict(self.metadata),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkGraphEdge(edge_id={self.edge_id!r}, source_node_id={self.source_node_id!r}, target_node_id={self.target_node_id!r})"


@dataclass(frozen=True, slots=True)
class WorkGraphComponent:
    """Connected component of work/evidence nodes."""

    component_id: str
    node_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"component_id": self.component_id, "node_ids": list(self.node_ids)}


@dataclass(frozen=True, slots=True)
class WorkGraphPriorityScore:
    """Transparent score components for one node."""

    node_id: str
    component_id: str
    transitive_fanout: int
    blocked_downstream_count: int
    stale_evidence_count: int
    replay_eval_evidence_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkGraphPriorityScore(node_id={self.node_id!r}, component_id={self.component_id!r}, transitive_fanout={self.transitive_fanout!r})"


@dataclass(frozen=True, slots=True)
class WorkGraphSnapshot:
    """Deterministic graph rebuild output."""

    graph_id: str
    nodes: tuple[WorkGraphNode, ...]
    edges: tuple[WorkGraphEdge, ...]
    components: tuple[WorkGraphComponent, ...]
    priority_scores: tuple[WorkGraphPriorityScore, ...]
    priority_policy: str = SUPPORTED_PRIORITY_POLICY

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "priority_policy": self.priority_policy,
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "components": [component.to_dict() for component in self.components],
            "priority_scores": [score.to_dict() for score in self.priority_scores],
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkGraphSnapshot(graph_id={self.graph_id!r}, nodes={self.nodes!r}, edges={self.edges!r})"


@dataclass(frozen=True, slots=True)
class WorkGraphResult:
    """Typed response for successful, degraded, or blocked rebuilds."""

    status: WorkGraphStatus
    reasons: tuple[str, ...]
    snapshot: WorkGraphSnapshot | None = None
    rejected_records: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reasons": list(self.reasons),
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
            "rejected_records": list(self.rejected_records),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkGraphResult(status={self.status!r}, reasons={self.reasons!r}, snapshot={self.snapshot!r})"
