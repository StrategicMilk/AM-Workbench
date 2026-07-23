"""Grounded Workbench knowledge graph and semantic layer.

The semantic layer turns Workbench provenance records into an inspectable graph
for lineage, compliance, metric, retrieval, and impact questions. It performs
no import-time I/O and stores no module-level mutable state.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class KnowledgeGraphError(ValueError):
    """Raised when semantic graph state cannot be trusted."""


class EntityKind(str, Enum):
    """Entity families tracked by the Workbench semantic layer."""

    SOURCE = "source"
    DOCUMENT = "document"
    CODE = "code"
    TRACE = "trace"
    DATASET = "dataset"
    TOOL_OUTPUT = "tool_output"
    PROMPT = "prompt"
    MODEL = "model"
    POLICY = "policy"
    DEPLOYMENT = "deployment"
    METRIC = "metric"
    DOMAIN_TERM = "domain_term"


class RelationKind(str, Enum):
    """Typed relation vocabulary for semantic and lineage edges."""

    DERIVED_FROM = "derived_from"
    EXTRACTED_FROM = "extracted_from"
    PRODUCES = "produces"
    CONSUMES = "consumes"
    DEFINES = "defines"
    DEPENDS_ON = "depends_on"
    IMPLEMENTS = "implements"
    GOVERNED_BY = "governed_by"
    VIOLATES = "violates"
    DEPLOYED_AS = "deployed_as"
    EVIDENCES = "evidences"
    SEMANTICALLY_RELATED = "semantically_related"


@dataclass(frozen=True, slots=True)
class KnowledgeGraphProvenanceRef:
    """Reference to a SourceCard, AssetCard, trace, document, or tool record."""

    ref_id: str
    ref_type: str
    evidence: str

    def __post_init__(self) -> None:
        _require_non_empty(self.ref_id, "ProvenanceRef.ref_id")
        _require_non_empty(self.ref_type, "ProvenanceRef.ref_type")
        _require_non_empty(self.evidence, "ProvenanceRef.evidence")


ProvenanceRef = KnowledgeGraphProvenanceRef


@dataclass(frozen=True, slots=True)
class SemanticEntity:
    """One grounded node in the Workbench semantic graph."""

    entity_id: str
    kind: EntityKind
    label: str
    provenance_refs: tuple[ProvenanceRef, ...]
    aliases: tuple[str, ...] = ()
    properties: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.entity_id, "SemanticEntity.entity_id")
        _require_non_empty(self.label, "SemanticEntity.label")
        object.__setattr__(self, "kind", EntityKind(self.kind))
        object.__setattr__(self, "provenance_refs", _tuple_of(self.provenance_refs, ProvenanceRef, "provenance_refs"))
        object.__setattr__(self, "aliases", tuple(self.aliases))
        object.__setattr__(self, "properties", dict(self.properties))
        if not self.provenance_refs:
            raise KnowledgeGraphError("SemanticEntity.provenance_refs must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SemanticEntity(entity_id={self.entity_id!r}, kind={self.kind!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class SemanticRelation:
    """One grounded edge between two semantic entities."""

    relation_id: str
    kind: RelationKind
    source_entity_id: str
    target_entity_id: str
    provenance_refs: tuple[ProvenanceRef, ...]
    confidence: float = 1.0
    properties: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.relation_id, "SemanticRelation.relation_id")
        _require_non_empty(self.source_entity_id, "SemanticRelation.source_entity_id")
        _require_non_empty(self.target_entity_id, "SemanticRelation.target_entity_id")
        object.__setattr__(self, "kind", RelationKind(self.kind))
        object.__setattr__(self, "provenance_refs", _tuple_of(self.provenance_refs, ProvenanceRef, "provenance_refs"))
        object.__setattr__(self, "properties", dict(self.properties))
        if not self.provenance_refs:
            raise KnowledgeGraphError("SemanticRelation.provenance_refs must be non-empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise KnowledgeGraphError("SemanticRelation.confidence must be between 0.0 and 1.0")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SemanticRelation(relation_id={self.relation_id!r}, kind={self.kind!r}, source_entity_id={self.source_entity_id!r})"


@dataclass(frozen=True, slots=True)
class SemanticMetricDefinition:
    """A metric definition bound to graph entities and provenance."""

    metric_id: str
    name: str
    expression: str
    entity_refs: tuple[str, ...]
    provenance_refs: tuple[ProvenanceRef, ...]
    owner: str = ""
    unit: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.metric_id, "SemanticMetricDefinition.metric_id")
        _require_non_empty(self.name, "SemanticMetricDefinition.name")
        _require_non_empty(self.expression, "SemanticMetricDefinition.expression")
        object.__setattr__(self, "entity_refs", tuple(self.entity_refs))
        object.__setattr__(self, "provenance_refs", _tuple_of(self.provenance_refs, ProvenanceRef, "provenance_refs"))
        if not self.entity_refs:
            raise KnowledgeGraphError("SemanticMetricDefinition.entity_refs must be non-empty")
        if not self.provenance_refs:
            raise KnowledgeGraphError("SemanticMetricDefinition.provenance_refs must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SemanticMetricDefinition(metric_id={self.metric_id!r}, name={self.name!r}, expression={self.expression!r})"


@dataclass(frozen=True, slots=True)
class DomainOntology:
    """Domain vocabulary used to normalize graph-backed retrieval."""

    ontology_id: str
    name: str
    terms: Mapping[str, tuple[str, ...]]
    provenance_refs: tuple[ProvenanceRef, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.ontology_id, "DomainOntology.ontology_id")
        _require_non_empty(self.name, "DomainOntology.name")
        normalized_terms = {str(key): tuple(values) for key, values in self.terms.items()}
        object.__setattr__(self, "terms", normalized_terms)
        object.__setattr__(self, "provenance_refs", _tuple_of(self.provenance_refs, ProvenanceRef, "provenance_refs"))
        if not normalized_terms:
            raise KnowledgeGraphError("DomainOntology.terms must be non-empty")
        if not self.provenance_refs:
            raise KnowledgeGraphError("DomainOntology.provenance_refs must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DomainOntology(ontology_id={self.ontology_id!r}, name={self.name!r}, terms={self.terms!r})"


@dataclass(frozen=True, slots=True)
class RetrievalGrounding:
    """Graph-backed retrieval result with provenance intact."""

    query: str
    entity_ids: tuple[str, ...]
    relation_ids: tuple[str, ...]
    provenance_refs: tuple[ProvenanceRef, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetrievalGrounding(query={self.query!r}, entity_ids={self.entity_ids!r}, relation_ids={self.relation_ids!r})"


@dataclass(frozen=True, slots=True)
class ImpactAnalysis:
    """Impact result for a changed graph entity or provenance reference."""

    changed_refs: tuple[str, ...]
    impacted_entity_ids: tuple[str, ...]
    impacted_relation_ids: tuple[str, ...]
    compliance_blockers: tuple[str, ...]
    provenance_refs: tuple[ProvenanceRef, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ImpactAnalysis(changed_refs={self.changed_refs!r}, impacted_entity_ids={self.impacted_entity_ids!r}, impacted_relation_ids={self.impacted_relation_ids!r})"


@dataclass(frozen=True, slots=True)
class SemanticLayerSnapshot:
    """Validated graph snapshot plus semantic metric and ontology context."""

    graph_id: str
    entities: tuple[SemanticEntity, ...]
    relations: tuple[SemanticRelation, ...]
    metrics: tuple[SemanticMetricDefinition, ...] = ()
    ontologies: tuple[DomainOntology, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.graph_id, "SemanticLayerSnapshot.graph_id")
        object.__setattr__(self, "entities", _tuple_of(self.entities, SemanticEntity, "entities"))
        object.__setattr__(self, "relations", _tuple_of(self.relations, SemanticRelation, "relations"))
        object.__setattr__(self, "metrics", _tuple_of(self.metrics, SemanticMetricDefinition, "metrics"))
        object.__setattr__(self, "ontologies", _tuple_of(self.ontologies, DomainOntology, "ontologies"))
        if not self.entities:
            raise KnowledgeGraphError("SemanticLayerSnapshot.entities must be non-empty")
        entity_ids = [entity.entity_id for entity in self.entities]
        _reject_duplicates(entity_ids, "entity_id")
        _reject_duplicates((relation.relation_id for relation in self.relations), "relation_id")
        _reject_duplicates((metric.metric_id for metric in self.metrics), "metric_id")
        entity_id_set = set(entity_ids)
        for relation in self.relations:
            if relation.source_entity_id not in entity_id_set or relation.target_entity_id not in entity_id_set:
                raise KnowledgeGraphError(f"relation {relation.relation_id!r} references an unknown entity")
        for metric in self.metrics:
            unknown_refs = sorted(set(metric.entity_refs) - entity_id_set)
            if unknown_refs:
                raise KnowledgeGraphError(f"metric {metric.metric_id!r} references unknown entities: {unknown_refs}")

    def retrieve(self, query: str, *, limit: int = 5) -> RetrievalGrounding:
        """Return graph nodes and edges whose labels, aliases, or properties match query terms.

        Returns:
            RetrievalGrounding value produced by retrieve().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        _require_non_empty(query, "query")
        if limit < 1:
            raise KnowledgeGraphError("limit must be >= 1")
        terms = {part.casefold() for part in query.replace("_", " ").split() if part.strip()}
        matched_entities: list[SemanticEntity] = []
        for entity in self.entities:
            haystack = " ".join((
                entity.entity_id,
                entity.kind.value,
                entity.label,
                " ".join(entity.aliases),
                " ".join(entity.properties.values()),
                _ontology_aliases_for(entity.label, self.ontologies),
            )).casefold()
            if any(term in haystack for term in terms):
                matched_entities.append(entity)
            if len(matched_entities) >= limit:
                break
        matched_ids = {entity.entity_id for entity in matched_entities}
        matched_relations = tuple(
            relation
            for relation in self.relations
            if relation.source_entity_id in matched_ids or relation.target_entity_id in matched_ids
        )
        provenance = _dedupe_provenance(ref for entity in matched_entities for ref in entity.provenance_refs)
        provenance += _dedupe_provenance(ref for relation in matched_relations for ref in relation.provenance_refs)
        return RetrievalGrounding(
            query=query,
            entity_ids=tuple(entity.entity_id for entity in matched_entities),
            relation_ids=tuple(relation.relation_id for relation in matched_relations),
            provenance_refs=provenance,
        )

    def analyze_impact(self, changed_refs: Iterable[str]) -> ImpactAnalysis:
        """Traverse graph relations from changed entity or provenance refs.

        Returns:
            ImpactAnalysis value produced by analyze_impact().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        requested = tuple(str(ref) for ref in changed_refs if str(ref).strip())
        if not requested:
            raise KnowledgeGraphError("changed_refs must be non-empty")
        entity_by_id = {entity.entity_id: entity for entity in self.entities}
        relation_by_id = {relation.relation_id: relation for relation in self.relations}
        start_ids = {
            entity.entity_id
            for entity in self.entities
            if entity.entity_id in requested
            or any(ref.ref_id in requested or ref.evidence in requested for ref in entity.provenance_refs)
        }
        for relation in self.relations:
            if relation.relation_id in requested or any(
                ref.ref_id in requested or ref.evidence in requested for ref in relation.provenance_refs
            ):
                start_ids.add(relation.source_entity_id)
                start_ids.add(relation.target_entity_id)
        if not start_ids:
            raise KnowledgeGraphError("changed_refs did not match any graph entity, relation, or provenance ref")
        adjacency: dict[str, list[SemanticRelation]] = {}
        for relation in self.relations:
            adjacency.setdefault(relation.source_entity_id, []).append(relation)
            adjacency.setdefault(relation.target_entity_id, []).append(relation)
        impacted_ids: set[str] = set()
        impacted_relation_ids: set[str] = set()
        queue: deque[str] = deque(start_ids)
        while queue:
            current = queue.popleft()
            if current in impacted_ids:
                continue
            impacted_ids.add(current)
            for relation in adjacency.get(current, ()):
                impacted_relation_ids.add(relation.relation_id)
                other = relation.target_entity_id if relation.source_entity_id == current else relation.source_entity_id
                if other not in impacted_ids:
                    queue.append(other)
        blockers = tuple(
            relation.relation_id
            for relation in self.relations
            if relation.relation_id in impacted_relation_ids
            and relation.kind in {RelationKind.GOVERNED_BY, RelationKind.VIOLATES}
        )
        provenance = _dedupe_provenance(
            ref for entity_id in sorted(impacted_ids) for ref in entity_by_id[entity_id].provenance_refs
        )
        provenance += _dedupe_provenance(
            ref for relation_id in sorted(impacted_relation_ids) for ref in relation_by_id[relation_id].provenance_refs
        )
        return ImpactAnalysis(
            changed_refs=requested,
            impacted_entity_ids=tuple(sorted(impacted_ids)),
            impacted_relation_ids=tuple(sorted(impacted_relation_ids)),
            compliance_blockers=blockers,
            provenance_refs=provenance,
        )

    def to_schema_document(self) -> dict[str, Any]:
        """Return a JSON-schema-valid representation of the semantic layer."""
        return {
            "graph_id": self.graph_id,
            "entities": [
                {
                    "entity_id": entity.entity_id,
                    "kind": entity.kind.value,
                    "label": entity.label,
                    "aliases": list(entity.aliases),
                    "properties": dict(entity.properties),
                    "provenance_refs": [_provenance_to_dict(ref) for ref in entity.provenance_refs],
                }
                for entity in self.entities
            ],
            "relations": [
                {
                    "relation_id": relation.relation_id,
                    "kind": relation.kind.value,
                    "source_entity_id": relation.source_entity_id,
                    "target_entity_id": relation.target_entity_id,
                    "confidence": relation.confidence,
                    "properties": dict(relation.properties),
                    "provenance_refs": [_provenance_to_dict(ref) for ref in relation.provenance_refs],
                }
                for relation in self.relations
            ],
            "metrics": [
                {
                    "metric_id": metric.metric_id,
                    "name": metric.name,
                    "expression": metric.expression,
                    "entity_refs": list(metric.entity_refs),
                    "owner": metric.owner,
                    "unit": metric.unit,
                    "provenance_refs": [_provenance_to_dict(ref) for ref in metric.provenance_refs],
                }
                for metric in self.metrics
            ],
            "ontologies": [
                {
                    "ontology_id": ontology.ontology_id,
                    "name": ontology.name,
                    "terms": {key: list(values) for key, values in ontology.terms.items()},
                    "provenance_refs": [_provenance_to_dict(ref) for ref in ontology.provenance_refs],
                }
                for ontology in self.ontologies
            ],
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SemanticLayerSnapshot(graph_id={self.graph_id!r}, entities={self.entities!r}, relations={self.relations!r})"


def build_semantic_layer(
    *,
    graph_id: str,
    entities: Iterable[SemanticEntity],
    relations: Iterable[SemanticRelation],
    metrics: Iterable[SemanticMetricDefinition] = (),
    ontologies: Iterable[DomainOntology] = (),
) -> SemanticLayerSnapshot:
    """Build and validate a grounded semantic layer snapshot."""
    return SemanticLayerSnapshot(
        graph_id=graph_id,
        entities=tuple(entities),
        relations=tuple(relations),
        metrics=tuple(metrics),
        ontologies=tuple(ontologies),
    )


def extract_entities_from_cards(
    *,
    source_cards: Iterable[Any] = (),
    asset_cards: Iterable[Any] = (),
) -> tuple[SemanticEntity, ...]:
    """Create graph entities from existing SourceCard and AssetCard-like records.

    Returns:
        tuple[SemanticEntity, ...] value produced by extract_entities_from_cards().
    """
    entities: list[SemanticEntity] = []
    for card in source_cards:
        source_card_id = _attr(card, "source_card_id")
        entities.append(
            SemanticEntity(
                entity_id=f"source:{source_card_id}",
                kind=EntityKind.SOURCE,
                label=_attr(card, "name"),
                aliases=tuple(_attr(card, "can_answer", ()) or ()),
                properties={"source_card_id": source_card_id, "kind": str(_attr(card, "kind"))},
                provenance_refs=(ProvenanceRef(source_card_id, "SourceCard", f"source_card:{source_card_id}"),),
            )
        )
    for card in asset_cards:
        asset_id = _attr(card, "asset_id")
        entities.append(
            SemanticEntity(
                entity_id=f"asset:{asset_id}",
                kind=_entity_kind_for_asset(card),
                label=_attr(card, "summary", _attr(card, "name", asset_id)),
                aliases=(_attr(card, "card_id", asset_id),),
                properties={"asset_id": asset_id, "revision": _attr(card, "revision", "")},
                provenance_refs=(ProvenanceRef(asset_id, "AssetCard", f"asset_card:{asset_id}"),),
            )
        )
    return tuple(entities)


def analyze_impact(snapshot: SemanticLayerSnapshot, changed_refs: Iterable[str]) -> ImpactAnalysis:
    """Convenience wrapper for callers that do not need the snapshot method."""
    return snapshot.analyze_impact(changed_refs)


def _entity_kind_for_asset(card: Any) -> EntityKind:
    card_kind = str(_attr(card, "card_kind", "")).casefold()
    asset_id = str(_attr(card, "asset_id", "")).casefold()
    if "dataset" in card_kind or "dataset" in asset_id:
        return EntityKind.DATASET
    if "prompt" in card_kind or "prompt" in asset_id:
        return EntityKind.PROMPT
    if "model" in card_kind or "model" in asset_id:
        return EntityKind.MODEL
    return EntityKind.DOCUMENT


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    value = getattr(obj, name, default)
    if value is None:
        return default
    return value


def _ontology_aliases_for(label: str, ontologies: tuple[DomainOntology, ...]) -> str:
    label_key = label.casefold()
    aliases: list[str] = []
    for ontology in ontologies:
        for term, values in ontology.terms.items():
            if term.casefold() == label_key or label_key in {value.casefold() for value in values}:
                aliases.extend(values)
    return " ".join(aliases)


def _dedupe_provenance(refs: Iterable[ProvenanceRef]) -> tuple[ProvenanceRef, ...]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[ProvenanceRef] = []
    for ref in refs:
        key = (ref.ref_id, ref.ref_type, ref.evidence)
        if key in seen:
            continue
        seen.add(key)
        unique.append(ref)
    return tuple(unique)


def _provenance_to_dict(ref: ProvenanceRef) -> dict[str, str]:
    return {"ref_id": ref.ref_id, "ref_type": ref.ref_type, "evidence": ref.evidence}


def _reject_duplicates(values: Iterable[str], field_name: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise KnowledgeGraphError(f"duplicate {field_name}: {value}")
        seen.add(value)


def _tuple_of(values: Iterable[Any], cls: type[Any], field_name: str) -> tuple[Any, ...]:
    normalized = tuple(values)
    if not all(isinstance(value, cls) for value in normalized):
        raise KnowledgeGraphError(f"{field_name} must contain {cls.__name__} instances")
    return normalized


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise KnowledgeGraphError(f"{field_name} must be non-empty")


__all__ = [
    "DomainOntology",
    "EntityKind",
    "ImpactAnalysis",
    "KnowledgeGraphError",
    "ProvenanceRef",
    "RelationKind",
    "RetrievalGrounding",
    "SemanticEntity",
    "SemanticLayerSnapshot",
    "SemanticMetricDefinition",
    "SemanticRelation",
    "analyze_impact",
    "build_semantic_layer",
    "extract_entities_from_cards",
]
