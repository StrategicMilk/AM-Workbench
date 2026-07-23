"""Typed AKS-compatible portable knowledge bundle records."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Self

from vetinari.workbench.knowledge.aks_bundle_support import (
    BundleExportError,
    _enum_or_value,
    _provenance_payloads,
    _record_payload,
    _require_bool,
    _require_mapping,
    _require_non_empty,
    _require_sequence,
    _require_str,
    _string_mapping,
)

SCHEMA_VERSION = "1.0"

PROVENANCE_REF_TYPES = {
    "semantic_entity",
    "semantic_relation",
    "context_asset_pack",
    "source_card",
    "tool_card",
    "run_record",
    "evidence_asset",
    "memory_lineage",
    "eval_result",
    "decision",
    "workflow_lesson",
}

AKS_ENTITY_KINDS = {
    "source",
    "document",
    "code",
    "trace",
    "dataset",
    "tool_output",
    "prompt",
    "model",
    "policy",
    "deployment",
    "metric",
    "domain_term",
    "claim",
    "decision",
    "memory",
    "workflow_lesson",
    "eval_result",
    "run_record",
}


class VerifiedFlag(str, Enum):
    """Verification state preserved from Workbench evidence."""

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    UNKNOWN = "unknown"


class ScopeBoundary(str, Enum):
    """Scope boundary for AKS-compatible bundle records."""

    PROJECT = "project"
    WORKSPACE = "workspace"
    USER = "user"
    PUBLIC = "public"


class ClaimAttestation(str, Enum):
    """Export-time strength of a Workbench observation."""

    CLAIM = "claim"
    OBSERVATION = "observation"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class AKSBundleAuthority:
    """Strict authority preservation policy for AKS exports.

    AKS is the export target, not the authority. These flags decide whether a
    Workbench evidence field maps to an AKS bundle field as a real claim,
    demoted to an unverified observation, or refused entirely. The default is
    strict; production callers must not relax flags without an audit trail.
    """

    must_preserve_source_traceability: bool = True
    must_preserve_verified_flag: bool = True
    must_preserve_scope: bool = True
    must_preserve_run_records: bool = True
    must_preserve_flow_steps: bool = True
    must_preserve_document_audit: bool = True

    @classmethod
    def strict_default(cls) -> Self:
        """Return the production default: all authority checks enabled."""
        return cls(
            must_preserve_source_traceability=True,
            must_preserve_verified_flag=True,
            must_preserve_scope=True,
            must_preserve_run_records=True,
            must_preserve_flow_steps=True,
            must_preserve_document_audit=True,
        )

    def to_payload(self) -> dict[str, bool]:
        return {
            "must_preserve_source_traceability": self.must_preserve_source_traceability,
            "must_preserve_verified_flag": self.must_preserve_verified_flag,
            "must_preserve_scope": self.must_preserve_scope,
            "must_preserve_run_records": self.must_preserve_run_records,
            "must_preserve_flow_steps": self.must_preserve_flow_steps,
            "must_preserve_document_audit": self.must_preserve_document_audit,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        """Execute the from payload operation.

        Returns:
            Self value produced by from_payload().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        required = set(cls.strict_default().to_payload())
        missing = required - set(payload)
        if missing:
            raise BundleExportError(f"authority missing required fields: {sorted(missing)}")
        return cls(**{field_name: _require_bool(payload[field_name], field_name) for field_name in required})

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AKSBundleAuthority(must_preserve_source_traceability={self.must_preserve_source_traceability!r}, must_preserve_verified_flag={self.must_preserve_verified_flag!r}, must_preserve_scope={self.must_preserve_scope!r})"


@dataclass(frozen=True, slots=True)
class AKSBundleProvenance:
    """Reference to the Workbench evidence supporting one exported record."""

    ref_type: str
    ref_id: str
    evidence: str

    def __post_init__(self) -> None:
        _require_non_empty(self.ref_type, "AKSBundleProvenance.ref_type")
        _require_non_empty(self.ref_id, "AKSBundleProvenance.ref_id")
        _require_non_empty(self.evidence, "AKSBundleProvenance.evidence")
        if self.ref_type not in PROVENANCE_REF_TYPES:
            raise BundleExportError(f"unsupported provenance ref_type: {self.ref_type!r}")

    def to_payload(self) -> dict[str, str]:
        return {"ref_type": self.ref_type, "ref_id": self.ref_id, "evidence": self.evidence}

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            ref_type=_require_str(payload, "ref_type"),
            ref_id=_require_str(payload, "ref_id"),
            evidence=_require_str(payload, "evidence"),
        )


@dataclass(frozen=True, slots=True)
class AKSBundleEntity:
    """AKS-shaped entity with source-preserving provenance."""

    entity_id: str
    kind: str
    label: str
    provenance_refs: tuple[AKSBundleProvenance, ...]
    aliases: tuple[str, ...] = ()
    properties: Mapping[str, str] = field(default_factory=dict)
    verified_flag: VerifiedFlag = VerifiedFlag.UNKNOWN
    scope: ScopeBoundary | None = ScopeBoundary.PROJECT

    def __post_init__(self) -> None:
        _require_non_empty(self.entity_id, "AKSBundleEntity.entity_id")
        _require_non_empty(self.label, "AKSBundleEntity.label")
        kind = _enum_or_value(self.kind)
        if kind not in AKS_ENTITY_KINDS:
            raise BundleExportError(f"unsupported AKS entity kind: {self.kind!r}")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "provenance_refs", _provenance_tuple(self.provenance_refs, "entity"))
        object.__setattr__(self, "aliases", tuple(str(alias) for alias in self.aliases))
        object.__setattr__(self, "properties", {str(key): str(value) for key, value in self.properties.items()})
        object.__setattr__(self, "verified_flag", VerifiedFlag(_enum_or_value(self.verified_flag)))
        if self.scope is not None:
            object.__setattr__(self, "scope", ScopeBoundary(_enum_or_value(self.scope)))

    def to_payload(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "label": self.label,
            "aliases": list(self.aliases),
            "properties": dict(self.properties),
            "verified_flag": self.verified_flag.value,
            "scope": None if self.scope is None else self.scope.value,
            "provenance_refs": _provenance_payloads(self.provenance_refs),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            entity_id=_require_str(payload, "entity_id"),
            kind=_require_str(payload, "kind"),
            label=_require_str(payload, "label"),
            aliases=tuple(str(item) for item in payload.get("aliases", ())),
            properties=_string_mapping(payload.get("properties", {}), "properties"),
            verified_flag=VerifiedFlag(_require_str(payload, "verified_flag")),
            scope=_optional_scope(payload.get("scope")),
            provenance_refs=_provenance_from_payload(payload, "provenance_refs"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AKSBundleEntity(entity_id={self.entity_id!r}, kind={self.kind!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class AKSBundleRelationship:
    """AKS-shaped relationship between exported entities."""

    relationship_id: str
    kind: str
    source_entity_id: str
    target_entity_id: str
    provenance_refs: tuple[AKSBundleProvenance, ...]
    confidence: float = 1.0
    properties: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.relationship_id, "AKSBundleRelationship.relationship_id")
        _require_non_empty(self.kind, "AKSBundleRelationship.kind")
        _require_non_empty(self.source_entity_id, "AKSBundleRelationship.source_entity_id")
        _require_non_empty(self.target_entity_id, "AKSBundleRelationship.target_entity_id")
        object.__setattr__(self, "provenance_refs", _provenance_tuple(self.provenance_refs, "relationship"))
        object.__setattr__(self, "properties", _string_mapping(self.properties, "properties"))
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise BundleExportError("AKSBundleRelationship.confidence must be between 0 and 1")

    def to_payload(self) -> dict[str, Any]:
        return {
            "relationship_id": self.relationship_id,
            "kind": _enum_or_value(self.kind),
            "source_entity_id": self.source_entity_id,
            "target_entity_id": self.target_entity_id,
            "confidence": float(self.confidence),
            "properties": dict(self.properties),
            "provenance_refs": _provenance_payloads(self.provenance_refs),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            relationship_id=_require_str(payload, "relationship_id"),
            kind=_require_str(payload, "kind"),
            source_entity_id=_require_str(payload, "source_entity_id"),
            target_entity_id=_require_str(payload, "target_entity_id"),
            confidence=float(payload.get("confidence", 1.0)),
            properties=_string_mapping(payload.get("properties", {}), "properties"),
            provenance_refs=_provenance_from_payload(payload, "provenance_refs"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AKSBundleRelationship(relationship_id={self.relationship_id!r}, kind={self.kind!r}, source_entity_id={self.source_entity_id!r})"


@dataclass(frozen=True, slots=True)
class AKSBundleSource:
    """AKS-shaped source record with freshness and audit metadata."""

    source_id: str
    kind: str
    name: str
    provenance_refs: tuple[AKSBundleProvenance, ...]
    freshness: str = "unknown"
    prompt_safety_status: str = "unknown"
    document_audit: Mapping[str, str] = field(default_factory=dict)
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.source_id, "AKSBundleSource.source_id")
        _require_non_empty(self.kind, "AKSBundleSource.kind")
        _require_non_empty(self.name, "AKSBundleSource.name")
        object.__setattr__(self, "provenance_refs", _provenance_tuple(self.provenance_refs, "source"))
        object.__setattr__(self, "document_audit", _string_mapping(self.document_audit, "document_audit"))
        object.__setattr__(self, "metadata", _string_mapping(self.metadata, "metadata"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "kind": self.kind,
            "name": self.name,
            "freshness": self.freshness,
            "prompt_safety_status": self.prompt_safety_status,
            "document_audit": dict(self.document_audit),
            "metadata": dict(self.metadata),
            "provenance_refs": _provenance_payloads(self.provenance_refs),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            source_id=_require_str(payload, "source_id"),
            kind=_require_str(payload, "kind"),
            name=_require_str(payload, "name"),
            freshness=str(payload.get("freshness", "unknown")),
            prompt_safety_status=str(payload.get("prompt_safety_status", "unknown")),
            document_audit=_string_mapping(payload.get("document_audit", {}), "document_audit"),
            metadata=_string_mapping(payload.get("metadata", {}), "metadata"),
            provenance_refs=_provenance_from_payload(payload, "provenance_refs"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AKSBundleSource(source_id={self.source_id!r}, kind={self.kind!r}, name={self.name!r})"


@dataclass(frozen=True, slots=True)
class AKSBundleRunRecord:
    """AKS-shaped agent run record."""

    run_id: str
    summary: str
    provenance_refs: tuple[AKSBundleProvenance, ...]
    status: str = "unknown"
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.run_id, "AKSBundleRunRecord.run_id")
        _require_non_empty(self.summary, "AKSBundleRunRecord.summary")
        object.__setattr__(self, "provenance_refs", _provenance_tuple(self.provenance_refs, "run_record"))
        object.__setattr__(self, "metadata", _string_mapping(self.metadata, "metadata"))

    def to_payload(self) -> dict[str, Any]:
        return _record_payload(
            record_id_key="run_id",
            record_id=self.run_id,
            provenance_refs=self.provenance_refs,
            summary=self.summary,
            status=self.status,
            metadata=self.metadata,
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            run_id=_require_str(payload, "run_id"),
            summary=_require_str(payload, "summary"),
            status=str(payload.get("status", "unknown")),
            metadata=_string_mapping(payload.get("metadata", {}), "metadata"),
            provenance_refs=_provenance_from_payload(payload, "provenance_refs"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AKSBundleRunRecord(run_id={self.run_id!r}, summary={self.summary!r}, provenance_refs={self.provenance_refs!r})"


@dataclass(frozen=True, slots=True)
class AKSBundleClaim:
    """AKS-shaped claim or observation with attestation strength."""

    claim_id: str
    claim_kind: str
    statement: str
    provenance_refs: tuple[AKSBundleProvenance, ...]
    attestation: ClaimAttestation = ClaimAttestation.OBSERVATION
    verified_flag: VerifiedFlag = VerifiedFlag.UNKNOWN
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.claim_id, "AKSBundleClaim.claim_id")
        _require_non_empty(self.claim_kind, "AKSBundleClaim.claim_kind")
        _require_non_empty(self.statement, "AKSBundleClaim.statement")
        object.__setattr__(self, "provenance_refs", _provenance_tuple(self.provenance_refs, "claim"))
        object.__setattr__(self, "attestation", ClaimAttestation(_enum_or_value(self.attestation)))
        object.__setattr__(self, "verified_flag", VerifiedFlag(_enum_or_value(self.verified_flag)))
        object.__setattr__(self, "metadata", _string_mapping(self.metadata, "metadata"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "claim_kind": self.claim_kind,
            "statement": self.statement,
            "attestation": self.attestation.value,
            "verified_flag": self.verified_flag.value,
            "metadata": dict(self.metadata),
            "provenance_refs": _provenance_payloads(self.provenance_refs),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            claim_id=_require_str(payload, "claim_id"),
            claim_kind=_require_str(payload, "claim_kind"),
            statement=_require_str(payload, "statement"),
            attestation=ClaimAttestation(_require_str(payload, "attestation")),
            verified_flag=VerifiedFlag(_require_str(payload, "verified_flag")),
            metadata=_string_mapping(payload.get("metadata", {}), "metadata"),
            provenance_refs=_provenance_from_payload(payload, "provenance_refs"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"AKSBundleClaim(claim_id={self.claim_id!r}, claim_kind={self.claim_kind!r}, statement={self.statement!r})"
        )


@dataclass(frozen=True, slots=True)
class AKSBundleDecision:
    """AKS-shaped decision record with flow-step provenance."""

    decision_id: str
    summary: str
    flow_steps: tuple[str, ...]
    provenance_refs: tuple[AKSBundleProvenance, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.decision_id, "AKSBundleDecision.decision_id")
        _require_non_empty(self.summary, "AKSBundleDecision.summary")
        object.__setattr__(self, "flow_steps", tuple(str(step) for step in self.flow_steps if str(step).strip()))
        object.__setattr__(self, "provenance_refs", _provenance_tuple(self.provenance_refs, "decision"))
        object.__setattr__(self, "metadata", _string_mapping(self.metadata, "metadata"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "summary": self.summary,
            "flow_steps": list(self.flow_steps),
            "metadata": dict(self.metadata),
            "provenance_refs": _provenance_payloads(self.provenance_refs),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            decision_id=_require_str(payload, "decision_id"),
            summary=_require_str(payload, "summary"),
            flow_steps=tuple(str(step) for step in payload.get("flow_steps", ())),
            metadata=_string_mapping(payload.get("metadata", {}), "metadata"),
            provenance_refs=_provenance_from_payload(payload, "provenance_refs"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AKSBundleDecision(decision_id={self.decision_id!r}, summary={self.summary!r}, flow_steps={self.flow_steps!r})"


@dataclass(frozen=True, slots=True)
class AKSBundleEvalResult:
    """AKS-shaped evaluation result."""

    eval_id: str
    summary: str
    passed: bool
    score: float
    evidence_refs: tuple[str, ...]
    provenance_refs: tuple[AKSBundleProvenance, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.eval_id, "AKSBundleEvalResult.eval_id")
        _require_non_empty(self.summary, "AKSBundleEvalResult.summary")
        object.__setattr__(self, "passed", bool(self.passed))
        score = float(self.score)
        if not 0.0 <= score <= 1.0:
            raise BundleExportError("AKSBundleEvalResult.score must be between 0 and 1")
        object.__setattr__(self, "score", score)
        evidence_refs = tuple(str(ref) for ref in self.evidence_refs if str(ref).strip())
        if not evidence_refs:
            raise BundleExportError("AKSBundleEvalResult.evidence_refs must be non-empty")
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "provenance_refs", _provenance_tuple(self.provenance_refs, "eval_result"))
        object.__setattr__(self, "metadata", _string_mapping(self.metadata, "metadata"))

    def to_payload(self) -> dict[str, Any]:
        return _record_payload(
            record_id_key="eval_id",
            record_id=self.eval_id,
            provenance_refs=self.provenance_refs,
            summary=self.summary,
            passed=self.passed,
            score=self.score,
            evidence_refs=list(self.evidence_refs),
            metadata=self.metadata,
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            eval_id=_require_str(payload, "eval_id"),
            summary=_require_str(payload, "summary"),
            passed=_require_bool(payload.get("passed"), "passed"),
            score=float(payload["score"]),
            evidence_refs=tuple(str(ref) for ref in _require_sequence(payload, "evidence_refs")),
            metadata=_string_mapping(payload.get("metadata", {}), "metadata"),
            provenance_refs=_provenance_from_payload(payload, "provenance_refs"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AKSBundleEvalResult(eval_id={self.eval_id!r}, summary={self.summary!r}, passed={self.passed!r})"


@dataclass(frozen=True, slots=True)
class AKSBundleMemory:
    """AKS-shaped memory record."""

    memory_id: str
    summary: str
    scope: ScopeBoundary | None
    validation_state: str
    evidence_refs: tuple[str, ...]
    provenance_refs: tuple[AKSBundleProvenance, ...]
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.memory_id, "AKSBundleMemory.memory_id")
        _require_non_empty(self.summary, "AKSBundleMemory.summary")
        if self.scope is not None:
            object.__setattr__(self, "scope", ScopeBoundary(_enum_or_value(self.scope)))
        if self.validation_state != "verified":
            raise BundleExportError("AKSBundleMemory.validation_state must be verified")
        evidence_refs = tuple(str(ref) for ref in self.evidence_refs if str(ref).strip())
        if not evidence_refs:
            raise BundleExportError("AKSBundleMemory.evidence_refs must be non-empty")
        object.__setattr__(self, "evidence_refs", evidence_refs)
        object.__setattr__(self, "provenance_refs", _provenance_tuple(self.provenance_refs, "memory"))
        object.__setattr__(self, "metadata", _string_mapping(self.metadata, "metadata"))

    def to_payload(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "summary": self.summary,
            "scope": None if self.scope is None else self.scope.value,
            "validation_state": self.validation_state,
            "evidence_refs": list(self.evidence_refs),
            "metadata": dict(self.metadata),
            "provenance_refs": _provenance_payloads(self.provenance_refs),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        return cls(
            memory_id=_require_str(payload, "memory_id"),
            summary=_require_str(payload, "summary"),
            scope=_optional_scope(payload.get("scope")),
            validation_state=_require_str(payload, "validation_state"),
            evidence_refs=tuple(str(ref) for ref in _require_sequence(payload, "evidence_refs")),
            metadata=_string_mapping(payload.get("metadata", {}), "metadata"),
            provenance_refs=_provenance_from_payload(payload, "provenance_refs"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AKSBundleMemory(memory_id={self.memory_id!r}, summary={self.summary!r}, scope={self.scope!r})"


def _provenance_tuple(values: Iterable[Any], record_name: str) -> tuple[AKSBundleProvenance, ...]:
    refs = tuple(values)
    if not refs:
        raise BundleExportError(f"{record_name}.provenance_refs must be non-empty")
    if not all(isinstance(ref, AKSBundleProvenance) for ref in refs):
        raise BundleExportError(f"{record_name}.provenance_refs must contain AKSBundleProvenance")
    return refs


def _provenance_from_payload(payload: Mapping[str, Any], field_name: str) -> tuple[AKSBundleProvenance, ...]:
    return tuple(
        AKSBundleProvenance.from_payload(_require_mapping(item, field_name))
        for item in _require_sequence(payload, field_name)
    )


def _optional_scope(value: Any) -> ScopeBoundary | None:
    if value is None or value == "":
        return None
    return ScopeBoundary(_enum_or_value(value))


def __getattr__(name: str) -> Any:
    """Return split top-level bundle records for legacy import paths."""
    if name == "AKSBundle":
        from vetinari.workbench.knowledge.aks_bundle_core import AKSBundle

        return AKSBundle
    if name == "AKSBundleWorkflowLesson":
        from vetinari.workbench.knowledge.aks_bundle_core import AKSBundleWorkflowLesson

        return AKSBundleWorkflowLesson
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
