"""Top-level AKS bundle records split from aks_bundle_records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Self

from vetinari.workbench.knowledge.aks_bundle_records import (
    AKSBundleAuthority,
    AKSBundleClaim,
    AKSBundleDecision,
    AKSBundleEntity,
    AKSBundleEvalResult,
    AKSBundleMemory,
    AKSBundleProvenance,
    AKSBundleRelationship,
    AKSBundleRunRecord,
    AKSBundleSource,
    _provenance_from_payload,
    _provenance_tuple,
)
from vetinari.workbench.knowledge.aks_bundle_support import (
    BundleExportError,
    _record_payload,
    _require_mapping,
    _require_non_empty,
    _require_sequence,
    _require_str,
    _string_mapping,
    _tuple_of,
)


@dataclass(frozen=True, slots=True)
class AKSBundleWorkflowLesson:
    """AKS-shaped workflow lesson."""

    lesson_id: str
    summary: str
    provenance_refs: tuple[AKSBundleProvenance, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.lesson_id, "AKSBundleWorkflowLesson.lesson_id")
        _require_non_empty(self.summary, "AKSBundleWorkflowLesson.summary")
        object.__setattr__(self, "provenance_refs", _provenance_tuple(self.provenance_refs, "workflow_lesson"))
        object.__setattr__(self, "metadata", _string_mapping(self.metadata, "metadata"))

    def to_payload(self) -> dict[str, Any]:
        return _record_payload(
            record_id_key="lesson_id",
            record_id=self.lesson_id,
            provenance_refs=self.provenance_refs,
            summary=self.summary,
            metadata=self.metadata,
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            lesson_id=_require_str(payload, "lesson_id"),
            summary=_require_str(payload, "summary"),
            metadata=_string_mapping(payload.get("metadata", {}), "metadata"),
            provenance_refs=_provenance_from_payload(payload, "provenance_refs"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AKSBundleWorkflowLesson(lesson_id={self.lesson_id!r}, summary={self.summary!r}, provenance_refs={self.provenance_refs!r})"


@dataclass(frozen=True, slots=True)
class AKSBundle:
    """Top-level AKS-compatible portable knowledge bundle."""

    project_id: str
    bundle_id: str
    schema_version: str
    exported_at_utc: str
    authority: AKSBundleAuthority
    entities: tuple[AKSBundleEntity, ...]
    relationships: tuple[AKSBundleRelationship, ...]
    sources: tuple[AKSBundleSource, ...]
    run_records: tuple[AKSBundleRunRecord, ...]
    claims: tuple[AKSBundleClaim, ...]
    decisions: tuple[AKSBundleDecision, ...]
    eval_results: tuple[AKSBundleEvalResult, ...]
    memories: tuple[AKSBundleMemory, ...]
    workflow_lessons: tuple[AKSBundleWorkflowLesson, ...]
    source_world_view: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.project_id, "AKSBundle.project_id")
        _require_non_empty(self.bundle_id, "AKSBundle.bundle_id")
        _require_non_empty(self.schema_version, "AKSBundle.schema_version")
        _require_non_empty(self.exported_at_utc, "AKSBundle.exported_at_utc")
        if not isinstance(self.authority, AKSBundleAuthority):
            raise BundleExportError("AKSBundle.authority must be AKSBundleAuthority")
        object.__setattr__(self, "entities", _tuple_of(self.entities, AKSBundleEntity, "entities"))
        object.__setattr__(self, "relationships", _tuple_of(self.relationships, AKSBundleRelationship, "relationships"))
        object.__setattr__(self, "sources", _tuple_of(self.sources, AKSBundleSource, "sources"))
        object.__setattr__(self, "run_records", _tuple_of(self.run_records, AKSBundleRunRecord, "run_records"))
        object.__setattr__(self, "claims", _tuple_of(self.claims, AKSBundleClaim, "claims"))
        object.__setattr__(self, "decisions", _tuple_of(self.decisions, AKSBundleDecision, "decisions"))
        object.__setattr__(self, "eval_results", _tuple_of(self.eval_results, AKSBundleEvalResult, "eval_results"))
        object.__setattr__(self, "memories", _tuple_of(self.memories, AKSBundleMemory, "memories"))
        object.__setattr__(
            self,
            "workflow_lessons",
            _tuple_of(self.workflow_lessons, AKSBundleWorkflowLesson, "workflow_lessons"),
        )
        object.__setattr__(self, "source_world_view", _string_mapping(self.source_world_view, "source_world_view"))

    def to_payload(self) -> dict[str, Any]:
        """Return the schema-pinned AKS-compatible wire payload."""
        return {
            "bundle_id": self.bundle_id,
            "project_id": self.project_id,
            "schema_version": self.schema_version,
            "exported_at_utc": self.exported_at_utc,
            "authority": self.authority.to_payload(),
            "source_world_view": dict(self.source_world_view),
            "entities": [entity.to_payload() for entity in self.entities],
            "relationships": [relationship.to_payload() for relationship in self.relationships],
            "sources": [source.to_payload() for source in self.sources],
            "run_records": [record.to_payload() for record in self.run_records],
            "claims": [claim.to_payload() for claim in self.claims],
            "decisions": [decision.to_payload() for decision in self.decisions],
            "eval_results": [result.to_payload() for result in self.eval_results],
            "memories": [memory.to_payload() for memory in self.memories],
            "workflow_lessons": [lesson.to_payload() for lesson in self.workflow_lessons],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        """Reconstruct a typed bundle from schema-shaped data.

        Returns:
            Self value produced by from_payload().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        required = {
            "bundle_id",
            "project_id",
            "schema_version",
            "exported_at_utc",
            "authority",
            "entities",
            "relationships",
            "sources",
            "run_records",
            "claims",
            "decisions",
            "eval_results",
            "memories",
            "workflow_lessons",
        }
        missing = required - set(payload)
        if missing:
            raise BundleExportError(f"bundle payload missing required fields: {sorted(missing)}")
        return cls(
            bundle_id=_require_str(payload, "bundle_id"),
            project_id=_require_str(payload, "project_id"),
            schema_version=_require_str(payload, "schema_version"),
            exported_at_utc=_require_str(payload, "exported_at_utc"),
            authority=AKSBundleAuthority.from_payload(_require_mapping(payload, "authority")),
            source_world_view=_string_mapping(payload.get("source_world_view", {}), "source_world_view"),
            entities=tuple(AKSBundleEntity.from_payload(item) for item in _require_sequence(payload, "entities")),
            relationships=tuple(
                AKSBundleRelationship.from_payload(item) for item in _require_sequence(payload, "relationships")
            ),
            sources=tuple(AKSBundleSource.from_payload(item) for item in _require_sequence(payload, "sources")),
            run_records=tuple(
                AKSBundleRunRecord.from_payload(item) for item in _require_sequence(payload, "run_records")
            ),
            claims=tuple(AKSBundleClaim.from_payload(item) for item in _require_sequence(payload, "claims")),
            decisions=tuple(AKSBundleDecision.from_payload(item) for item in _require_sequence(payload, "decisions")),
            eval_results=tuple(
                AKSBundleEvalResult.from_payload(item) for item in _require_sequence(payload, "eval_results")
            ),
            memories=tuple(AKSBundleMemory.from_payload(item) for item in _require_sequence(payload, "memories")),
            workflow_lessons=tuple(
                AKSBundleWorkflowLesson.from_payload(item) for item in _require_sequence(payload, "workflow_lessons")
            ),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AKSBundle(project_id={self.project_id!r}, bundle_id={self.bundle_id!r}, schema_version={self.schema_version!r})"
