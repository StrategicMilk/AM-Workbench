"""Knowledge coverage public records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Self

SCHEMA_VERSION = 1


class CoverageStatus(str, Enum):
    """Aggregate quality status for a knowledge coverage report."""

    ALLOWED = "allowed"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class SourceFreshnessStatus(str, Enum):
    """Fail-closed source freshness states."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class TrustedCurrentVerdict(str, Enum):
    """Trust verdicts for current-context filtering."""

    ALLOWED = "allowed"
    CAVEATED = "caveated"
    DOWNRANKED = "downranked"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SourceFreshnessSignal:
    """Freshness status for one upstream source."""

    source_id: str
    status: SourceFreshnessStatus | str
    reason: str = ""

    def __post_init__(self) -> None:
        _require_text(self.source_id, "source_id")
        object.__setattr__(self, "status", SourceFreshnessStatus(_enum_value(self.status)))

    def to_payload(self) -> dict[str, str]:
        return {"source_id": self.source_id, "status": self.status.value, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class RelationshipConfidenceSignal:
    """Confidence and trust verdict for one knowledge relationship."""

    relationship_id: str
    confidence: float | None
    verdict: TrustedCurrentVerdict | str
    reason: str = ""

    def __post_init__(self) -> None:
        _require_text(self.relationship_id, "relationship_id")
        if self.confidence is not None and not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("relationship confidence must be between 0 and 1")
        object.__setattr__(self, "verdict", TrustedCurrentVerdict(_enum_value(self.verdict)))

    def to_payload(self) -> dict[str, Any]:
        return {
            "relationship_id": self.relationship_id,
            "confidence": self.confidence,
            "verdict": self.verdict.value,
            "reason": self.reason,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RelationshipConfidenceSignal(relationship_id={self.relationship_id!r}, confidence={self.confidence!r}, verdict={self.verdict!r})"


@dataclass(frozen=True, slots=True)
class MissingEvidenceGap:
    """Evidence required by a record but absent from the trusted evidence set."""

    record_id: str
    evidence_ref: str
    reason: str

    def __post_init__(self) -> None:
        _require_text(self.record_id, "record_id")
        _require_text(self.evidence_ref, "evidence_ref")
        _require_text(self.reason, "reason")

    def to_payload(self) -> dict[str, str]:
        return {"record_id": self.record_id, "evidence_ref": self.evidence_ref, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class TrustedContextDecision:
    """Decision produced by the trusted-current context filter."""

    record_id: str
    entity_id: str
    verdict: TrustedCurrentVerdict | str
    reasons: tuple[str, ...]
    rank_multiplier: float
    text: str = ""

    def __post_init__(self) -> None:
        _require_text(self.record_id, "record_id")
        _require_text(self.entity_id, "entity_id")
        reasons = tuple(str(reason) for reason in self.reasons if str(reason).strip())
        object.__setattr__(self, "verdict", TrustedCurrentVerdict(_enum_value(self.verdict)))
        object.__setattr__(self, "reasons", reasons)
        if not 0.0 <= self.rank_multiplier <= 1.0:
            raise ValueError("rank_multiplier must be between 0 and 1")

    def to_payload(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "entity_id": self.entity_id,
            "verdict": self.verdict.value,
            "reasons": list(self.reasons),
            "rank_multiplier": self.rank_multiplier,
            "text": self.text,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TrustedContextDecision(record_id={self.record_id!r}, entity_id={self.entity_id!r}, verdict={self.verdict!r})"


@dataclass(frozen=True, slots=True)
class TrustedCurrentContextResult:
    """Accepted and non-ordinary context decisions from the trust gate."""

    trusted_context: tuple[Mapping[str, Any], ...]
    decisions: tuple[TrustedContextDecision, ...]

    @property
    def blocked_records(self) -> tuple[TrustedContextDecision, ...]:
        return tuple(decision for decision in self.decisions if decision.verdict is TrustedCurrentVerdict.BLOCKED)

    @property
    def caveated_records(self) -> tuple[TrustedContextDecision, ...]:
        return tuple(decision for decision in self.decisions if decision.verdict is TrustedCurrentVerdict.CAVEATED)

    @property
    def downranked_records(self) -> tuple[TrustedContextDecision, ...]:
        return tuple(decision for decision in self.decisions if decision.verdict is TrustedCurrentVerdict.DOWNRANKED)

    def to_payload(self) -> dict[str, Any]:
        return {
            "trusted_context": [dict(record) for record in self.trusted_context],
            "decisions": [decision.to_payload() for decision in self.decisions],
        }


@dataclass(frozen=True, slots=True)
class KnowledgeCoverageReport:
    """Operational report over Workbench knowledge quality signals."""

    coverage_score: float
    source_freshness: Mapping[str, SourceFreshnessSignal]
    consolidation_count: int
    deprecated_entities: tuple[str, ...]
    source_hash_drift: tuple[str, ...]
    relationship_confidence: Mapping[str, RelationshipConfidenceSignal]
    missing_evidence_gaps: tuple[MissingEvidenceGap, ...]
    trusted_current_verdict: TrustedCurrentVerdict | str
    status: CoverageStatus | str
    provenance_refs: tuple[str, ...]
    decisions: tuple[TrustedContextDecision, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported knowledge coverage schema_version")
        if not 0.0 <= float(self.coverage_score) <= 1.0:
            raise ValueError("coverage_score must be between 0 and 1")
        if self.consolidation_count < 0:
            raise ValueError("consolidation_count must be non-negative")
        provenance_refs = tuple(str(ref) for ref in self.provenance_refs if str(ref).strip())
        if not provenance_refs:
            raise ValueError("provenance_refs must be non-empty")
        object.__setattr__(
            self, "trusted_current_verdict", TrustedCurrentVerdict(_enum_value(self.trusted_current_verdict))
        )
        object.__setattr__(self, "status", CoverageStatus(_enum_value(self.status)))
        object.__setattr__(self, "provenance_refs", provenance_refs)
        object.__setattr__(self, "deprecated_entities", tuple(sorted(set(self.deprecated_entities))))
        object.__setattr__(self, "source_hash_drift", tuple(sorted(set(self.source_hash_drift))))
        object.__setattr__(
            self,
            "source_freshness",
            dict(sorted(self.source_freshness.items())),
        )
        object.__setattr__(
            self,
            "relationship_confidence",
            dict(sorted(self.relationship_confidence.items())),
        )

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-schema-valid payload for persistence or dashboards."""
        return {
            "schema_version": self.schema_version,
            "status": self.status.value,
            "coverage_score": self.coverage_score,
            "source_freshness": {key: signal.to_payload() for key, signal in self.source_freshness.items()},
            "consolidation_count": self.consolidation_count,
            "deprecated_entities": list(self.deprecated_entities),
            "source_hash_drift": list(self.source_hash_drift),
            "relationship_confidence": {
                key: signal.to_payload() for key, signal in self.relationship_confidence.items()
            },
            "missing_evidence_gaps": [gap.to_payload() for gap in self.missing_evidence_gaps],
            "trusted_current_verdict": self.trusted_current_verdict.value,
            "provenance_refs": list(self.provenance_refs),
            "decisions": [decision.to_payload() for decision in self.decisions],
        }

    @classmethod
    def blocked(cls, reason: str) -> Self:
        """Build an explicit blocked report for unavailable or malformed inputs.

        Returns:
            Self value produced by blocked().
        """
        _require_text(reason, "reason")
        gap = MissingEvidenceGap(record_id="upstream", evidence_ref="upstream:unavailable", reason=reason)
        decision = TrustedContextDecision(
            record_id="upstream",
            entity_id="upstream",
            verdict=TrustedCurrentVerdict.BLOCKED,
            reasons=(reason,),
            rank_multiplier=0.0,
        )
        return cls(
            coverage_score=0.0,
            source_freshness={
                "upstream:unavailable": SourceFreshnessSignal(
                    "upstream:unavailable",
                    SourceFreshnessStatus.UNAVAILABLE,
                    reason,
                )
            },
            consolidation_count=0,
            deprecated_entities=(),
            source_hash_drift=(),
            relationship_confidence={},
            missing_evidence_gaps=(gap,),
            trusted_current_verdict=TrustedCurrentVerdict.BLOCKED,
            status=CoverageStatus.BLOCKED,
            provenance_refs=("upstream:unavailable",),
            decisions=(decision,),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"KnowledgeCoverageReport(coverage_score={self.coverage_score!r}, source_freshness={self.source_freshness!r}, consolidation_count={self.consolidation_count!r})"


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _enum_value(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value)
