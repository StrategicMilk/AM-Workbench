"""Private signal aggregation helpers for knowledge coverage."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self

from vetinari.workbench.knowledge.coverage_models import (
    CoverageStatus,
    MissingEvidenceGap,
    RelationshipConfidenceSignal,
    SourceFreshnessSignal,
    SourceFreshnessStatus,
    TrustedContextDecision,
    TrustedCurrentVerdict,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CoverageSignals:
    has_any_input: bool
    source_freshness: Mapping[str, SourceFreshnessSignal]
    relationship_confidence: Mapping[str, RelationshipConfidenceSignal]
    deprecated_entities: frozenset[str]
    source_hash_drift: frozenset[str]
    consolidation_count: int
    missing_evidence_gaps: tuple[MissingEvidenceGap, ...]
    provenance_refs: frozenset[str]
    available_evidence_refs: frozenset[str] | None
    required_evidence_refs: tuple[str, ...]
    trusted_records: tuple[Any, ...]
    coverage_checks: tuple[bool, ...]

    @classmethod
    def from_inputs(
        cls,
        *,
        aks_bundle: Any | None,
        backfeed_records: tuple[Any, ...],
        semantic_snapshot: Any | None,
        retrieval_traces: tuple[Any, ...],
        context_assemblies: tuple[Any, ...],
        trusted_records: tuple[Any, ...],
        current_source_hashes: Mapping[str, str],
        available_evidence_refs: set[str] | None,
        required_evidence_refs: tuple[str, ...],
        low_confidence_threshold: float,
    ) -> Self:
        """Execute the from inputs operation.

        Returns:
            Self value produced by from_inputs().
        """
        state = _collect_coverage_signal_state(
            aks_bundle=aks_bundle,
            backfeed_records=backfeed_records,
            semantic_snapshot=semantic_snapshot,
            retrieval_traces=retrieval_traces,
            context_assemblies=context_assemblies,
            current_source_hashes=current_source_hashes,
            available_evidence_refs=available_evidence_refs,
            low_confidence_threshold=low_confidence_threshold,
        )
        required = tuple(dict.fromkeys(required_evidence_refs))
        if state.available is not None:
            _append_required_evidence_gaps(required, state.available, state.missing_gaps, state.coverage_checks)

        return cls(
            has_any_input=_has_any_coverage_input(
                aks_bundle,
                backfeed_records,
                semantic_snapshot,
                retrieval_traces,
                context_assemblies,
                trusted_records,
            ),
            source_freshness=state.source_freshness,
            relationship_confidence=state.relationship_confidence,
            deprecated_entities=frozenset(state.deprecated_entities),
            source_hash_drift=frozenset(state.source_hash_drift),
            consolidation_count=state.consolidation_count,
            missing_evidence_gaps=tuple(state.missing_gaps),
            provenance_refs=frozenset(state.provenance_refs),
            available_evidence_refs=None if state.available is None else frozenset(state.available),
            required_evidence_refs=required,
            trusted_records=trusted_records,
            coverage_checks=tuple(state.coverage_checks),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"_CoverageSignals(has_any_input={self.has_any_input!r}, source_freshness={self.source_freshness!r}, relationship_confidence={self.relationship_confidence!r})"


@dataclass(slots=True)
class _CoverageSignalState:
    source_freshness: dict[str, SourceFreshnessSignal]
    relationship_confidence: dict[str, RelationshipConfidenceSignal]
    deprecated_entities: set[str]
    source_hash_drift: set[str]
    missing_gaps: list[MissingEvidenceGap]
    provenance_refs: set[str]
    available: set[str] | None
    coverage_checks: list[bool]
    consolidation_count: int = 0

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"source_freshness={self.source_freshness!r}, "
            f"relationship_confidence={self.relationship_confidence!r}, "
            f"deprecated_entities={self.deprecated_entities!r}, "
            f"source_hash_drift={self.source_hash_drift!r}"
            ")"
        )


def _collect_coverage_signal_state(
    *,
    aks_bundle: Any | None,
    backfeed_records: tuple[Any, ...],
    semantic_snapshot: Any | None,
    retrieval_traces: tuple[Any, ...],
    context_assemblies: tuple[Any, ...],
    current_source_hashes: Mapping[str, str],
    available_evidence_refs: set[str] | None,
    low_confidence_threshold: float,
) -> _CoverageSignalState:
    state = _CoverageSignalState({}, {}, set(), set(), [], set(), None, [])
    state.available = None if available_evidence_refs is None else set(available_evidence_refs)
    if aks_bundle is not None:
        _collect_aks_bundle_signals(
            aks_bundle,
            current_source_hashes,
            low_confidence_threshold,
            state.source_freshness,
            state.relationship_confidence,
            state.deprecated_entities,
            state.source_hash_drift,
            state.missing_gaps,
            state.provenance_refs,
            state.available,
            state.coverage_checks,
        )
    state.consolidation_count += _collect_backfeed_signals(
        backfeed_records,
        low_confidence_threshold,
        state.deprecated_entities,
        state.provenance_refs,
        state.available,
        state.coverage_checks,
    )
    if semantic_snapshot is not None:
        state.consolidation_count += _collect_semantic_snapshot_signals(
            semantic_snapshot,
            low_confidence_threshold,
            state.relationship_confidence,
            state.deprecated_entities,
            state.provenance_refs,
            state.coverage_checks,
        )
    _collect_retrieval_trace_signals(
        retrieval_traces, state.provenance_refs, state.available, state.coverage_checks, state.missing_gaps
    )
    _collect_context_assembly_signals(
        context_assemblies, state.provenance_refs, state.coverage_checks, state.missing_gaps
    )
    return state


def _has_any_coverage_input(
    aks_bundle: Any | None,
    backfeed_records: tuple[Any, ...],
    semantic_snapshot: Any | None,
    retrieval_traces: tuple[Any, ...],
    context_assemblies: tuple[Any, ...],
    trusted_records: tuple[Any, ...],
) -> bool:
    return any((
        aks_bundle is not None,
        bool(backfeed_records),
        semantic_snapshot is not None,
        bool(retrieval_traces),
        bool(context_assemblies),
        bool(trusted_records),
    ))


def _collect_aks_bundle_signals(
    aks_bundle: Any,
    current_source_hashes: Mapping[str, str],
    low_confidence_threshold: float,
    source_freshness: dict[str, SourceFreshnessSignal],
    relationship_confidence: dict[str, RelationshipConfidenceSignal],
    deprecated_entities: set[str],
    source_hash_drift: set[str],
    missing_gaps: list[MissingEvidenceGap],
    provenance_refs: set[str],
    available: set[str] | None,
    coverage_checks: list[bool],
) -> None:
    for source in _iter_field(aks_bundle, "sources"):
        _collect_aks_source_signal(
            source,
            current_source_hashes,
            source_freshness,
            source_hash_drift,
            provenance_refs,
            available,
            coverage_checks,
        )
    for entity in _iter_field(aks_bundle, "entities"):
        entity_id = str(_field(entity, "entity_id", ""))
        evidence = _provenance_evidence(_field(entity, "provenance_refs", ()))
        provenance_refs.update(evidence)
        coverage_checks.append(bool(evidence) and entity_id not in deprecated_entities)
        if not evidence:
            missing_gaps.append(
                MissingEvidenceGap(entity_id or "aks_entity", "provenance_refs", "entity lacks provenance")
            )
    for relationship in _iter_field(aks_bundle, "relationships"):
        _collect_relationship_signal(
            relationship,
            "aks_relationship_confidence",
            low_confidence_threshold,
            relationship_confidence,
            provenance_refs,
            coverage_checks,
        )


def _collect_aks_source_signal(
    source: Any,
    current_source_hashes: Mapping[str, str],
    source_freshness: dict[str, SourceFreshnessSignal],
    source_hash_drift: set[str],
    provenance_refs: set[str],
    available: set[str] | None,
    coverage_checks: list[bool],
) -> None:
    source_id = str(_field(source, "source_id", ""))
    if not source_id:
        return
    freshness = _source_status(str(_field(source, "freshness", "unknown")))
    source_freshness[source_id] = SourceFreshnessSignal(source_id, freshness, f"aks_source:{source_id}")
    evidence = _provenance_evidence(_field(source, "provenance_refs", ()))
    provenance_refs.update(evidence)
    if available is not None:
        available.update(evidence)
    expected_hash = _source_expected_hash(source)
    current_hash = current_source_hashes.get(source_id)
    if expected_hash and current_hash and expected_hash != current_hash:
        source_hash_drift.add(source_id)
    coverage_checks.append(freshness is SourceFreshnessStatus.FRESH and source_id not in source_hash_drift)


def _collect_relationship_signal(
    relationship: Any,
    reason: str,
    low_confidence_threshold: float,
    relationship_confidence: dict[str, RelationshipConfidenceSignal],
    provenance_refs: set[str],
    coverage_checks: list[bool],
) -> None:
    relation_id = str(_field(relationship, "relationship_id", _field(relationship, "relation_id", "")))
    confidence = float(_field(relationship, "confidence", 0.0))
    verdict = (
        TrustedCurrentVerdict.ALLOWED if confidence >= low_confidence_threshold else TrustedCurrentVerdict.DOWNRANKED
    )
    relationship_confidence[relation_id] = RelationshipConfidenceSignal(
        relationship_id=relation_id,
        confidence=confidence,
        verdict=verdict,
        reason=reason,
    )
    evidence = _provenance_evidence(_field(relationship, "provenance_refs", ()))
    provenance_refs.update(evidence)
    coverage_checks.append(bool(evidence) and verdict is TrustedCurrentVerdict.ALLOWED)


def _collect_backfeed_signals(
    backfeed_records: tuple[Any, ...],
    low_confidence_threshold: float,
    deprecated_entities: set[str],
    provenance_refs: set[str],
    available: set[str] | None,
    coverage_checks: list[bool],
) -> int:
    consolidation_count = 0
    for record in backfeed_records:
        payload = _record_payload(record)
        change = _as_mapping(payload.get("change", {}))
        deprecates = tuple(change.get("deprecates", ()) or ())
        supersedes = tuple(change.get("supersedes", ()) or ())
        deprecated_entities.update(map(str, deprecates))
        deprecated_entities.update(map(str, supersedes))
        consolidation_count += len(deprecates) + len(supersedes)
        source = _as_mapping(payload.get("source", {}))
        evidence_ref = str(source.get("evidence_ref", "")).strip()
        if evidence_ref:
            provenance_refs.add(evidence_ref)
            if available is not None:
                available.add(evidence_ref)
        confidence = _optional_float(source.get("confidence"))
        coverage_checks.append(confidence is not None and confidence >= low_confidence_threshold and bool(evidence_ref))
    return consolidation_count


def _collect_semantic_snapshot_signals(
    semantic_snapshot: Any,
    low_confidence_threshold: float,
    relationship_confidence: dict[str, RelationshipConfidenceSignal],
    deprecated_entities: set[str],
    provenance_refs: set[str],
    coverage_checks: list[bool],
) -> int:
    consolidation_count = 0
    for entity_id, properties in _semantic_entity_properties(semantic_snapshot).items():
        status = str(properties.get("status", properties.get("lifecycle", ""))).casefold()
        if status in {"deprecated", "superseded"}:
            deprecated_entities.add(entity_id)
        if properties.get("canonical_id") and properties.get("canonical_id") != entity_id:
            consolidation_count += 1
    for relation in _iter_field(semantic_snapshot, "relations"):
        _collect_relationship_signal(
            relation,
            "semantic_relation_confidence",
            low_confidence_threshold,
            relationship_confidence,
            provenance_refs,
            coverage_checks,
        )
    return consolidation_count


def _semantic_entity_properties(semantic_snapshot: Any) -> dict[str, Mapping[str, Any]]:
    return {
        str(_field(entity, "entity_id", "")): _as_mapping(_field(entity, "properties", {}))
        for entity in _iter_field(semantic_snapshot, "entities")
    }


def _collect_retrieval_trace_signals(
    retrieval_traces: tuple[Any, ...],
    provenance_refs: set[str],
    available: set[str] | None,
    coverage_checks: list[bool],
    missing_gaps: list[MissingEvidenceGap],
) -> None:
    for trace in retrieval_traces:
        for candidate in (*_iter_field(trace, "candidates"), *_iter_field(trace, "rejected_candidates")):
            _collect_retrieval_candidate_signal(candidate, provenance_refs, available, coverage_checks, missing_gaps)


def _collect_retrieval_candidate_signal(
    candidate: Any,
    provenance_refs: set[str],
    available: set[str] | None,
    coverage_checks: list[bool],
    missing_gaps: list[MissingEvidenceGap],
) -> None:
    candidate_id = str(_field(candidate, "chunk_id", "retrieval_candidate"))
    rejected = bool(_field(candidate, "rejected", False))
    coverage_checks.append(not rejected)
    metadata = _as_mapping(_field(candidate, "metadata", {}))
    evidence = str(metadata.get("evidence_ref", "")).strip()
    if evidence:
        provenance_refs.add(evidence)
        if available is not None:
            available.add(evidence)
    elif not rejected:
        missing_gaps.append(
            MissingEvidenceGap(candidate_id, "metadata.evidence_ref", "retrieval candidate lacks evidence")
        )


def _collect_context_assembly_signals(
    context_assemblies: tuple[Any, ...],
    provenance_refs: set[str],
    coverage_checks: list[bool],
    missing_gaps: list[MissingEvidenceGap],
) -> None:
    for context in context_assemblies:
        coverage = _as_mapping(_field(context, "source_coverage", {}))
        if not coverage:
            missing_gaps.append(
                MissingEvidenceGap("context_assembly", "source_coverage", "context has no source coverage")
            )
            coverage_checks.append(False)
        for source_id, count in coverage.items():
            coverage_checks.append(int(count) > 0)
            provenance_refs.add(f"rag_source_coverage:{source_id}")


def _append_required_evidence_gaps(
    required: tuple[str, ...],
    available: set[str],
    missing_gaps: list[MissingEvidenceGap],
    coverage_checks: list[bool],
) -> None:
    for evidence_ref in required:
        if evidence_ref not in available:
            missing_gaps.append(MissingEvidenceGap("coverage", evidence_ref, "required evidence ref unavailable"))
            coverage_checks.append(False)


def _aggregate_status(
    *,
    source_freshness: Mapping[str, SourceFreshnessSignal],
    relationship_confidence: Mapping[str, RelationshipConfidenceSignal],
    deprecated_entities: frozenset[str],
    source_hash_drift: frozenset[str],
    missing_gaps: Sequence[MissingEvidenceGap],
    filter_decisions: Sequence[TrustedContextDecision],
) -> tuple[CoverageStatus, TrustedCurrentVerdict]:
    if source_hash_drift or any(decision.verdict is TrustedCurrentVerdict.BLOCKED for decision in filter_decisions):
        return CoverageStatus.BLOCKED, TrustedCurrentVerdict.BLOCKED
    if deprecated_entities and filter_decisions:
        return CoverageStatus.BLOCKED, TrustedCurrentVerdict.BLOCKED
    degraded_source = any(signal.status is not SourceFreshnessStatus.FRESH for signal in source_freshness.values())
    degraded_relation = any(
        signal.verdict is not TrustedCurrentVerdict.ALLOWED for signal in relationship_confidence.values()
    )
    degraded_filter = any(decision.verdict is not TrustedCurrentVerdict.ALLOWED for decision in filter_decisions)
    if missing_gaps or deprecated_entities or degraded_source or degraded_relation or degraded_filter:
        return CoverageStatus.DEGRADED, TrustedCurrentVerdict.DOWNRANKED
    return CoverageStatus.ALLOWED, TrustedCurrentVerdict.ALLOWED


def _verdict_for_reasons(reasons: Sequence[str]) -> tuple[TrustedCurrentVerdict, float]:
    if not reasons:
        return TrustedCurrentVerdict.ALLOWED, 1.0
    if any(
        reason.startswith(("deprecated_entity:", "source_hash_drift:", "unavailable_source:")) for reason in reasons
    ):
        return TrustedCurrentVerdict.BLOCKED, 0.0
    if any(
        reason.startswith(("missing_evidence:", "unknown_source:", "missing_relationship_confidence:"))
        for reason in reasons
    ):
        return TrustedCurrentVerdict.CAVEATED, 0.4
    return TrustedCurrentVerdict.DOWNRANKED, 0.6


def _record_payload(record: Any) -> Mapping[str, Any]:
    if isinstance(record, Mapping):
        return record
    if hasattr(record, "to_payload"):
        payload = record.to_payload()
        if isinstance(payload, Mapping):
            return payload
    return {
        key: getattr(record, key)
        for key in (
            "record_id",
            "entity_id",
            "subject_ref",
            "text",
            "source_ids",
            "source_hashes",
            "relationship_ids",
            "evidence_refs",
        )
        if hasattr(record, key)
    }


def _record_id(record: Mapping[str, Any]) -> str:
    return str(
        record.get("record_id")
        or record.get("proposal_id")
        or record.get("entity_id")
        or record.get("subject_ref")
        or "record"
    )


def _record_source_ids(record: Mapping[str, Any]) -> tuple[str, ...]:
    source_ids = record.get("source_ids")
    if source_ids is None:
        source_id = _as_mapping(record.get("source", {})).get("source_id") or record.get("source_id")
        source_ids = (source_id,) if source_id else ()
    return tuple(str(source_id) for source_id in source_ids if str(source_id).strip())


def _record_relationship_ids(record: Mapping[str, Any]) -> tuple[str, ...]:
    ids = record.get("relationship_ids", ())
    return tuple(str(item) for item in ids if str(item).strip())


def _record_evidence_refs(record: Mapping[str, Any]) -> tuple[str, ...]:
    refs = record.get("evidence_refs")
    if refs is None:
        source = _as_mapping(record.get("source", {}))
        refs = (source.get("evidence_ref"),) if source.get("evidence_ref") else ()
    return tuple(str(ref) for ref in refs if str(ref).strip())


def _record_source_hashes(record: Mapping[str, Any]) -> Mapping[str, str]:
    return {str(key): str(value) for key, value in _as_mapping(record.get("source_hashes", {})).items()}


def _freshness_signal(
    source_id: str,
    source_freshness: Mapping[str, SourceFreshnessSignal | str],
) -> SourceFreshnessSignal:
    raw_signal = source_freshness.get(source_id)
    if isinstance(raw_signal, SourceFreshnessSignal):
        return raw_signal
    if raw_signal is not None:
        return SourceFreshnessSignal(source_id, str(raw_signal), "record supplied source freshness")
    return SourceFreshnessSignal(source_id, SourceFreshnessStatus.UNKNOWN, "source freshness unavailable")


def _confidence_signal(
    relationship_id: str,
    relationship_confidence: Mapping[str, RelationshipConfidenceSignal | float],
    low_confidence_threshold: float,
) -> RelationshipConfidenceSignal:
    raw_signal = relationship_confidence.get(relationship_id)
    if isinstance(raw_signal, RelationshipConfidenceSignal):
        return raw_signal
    if raw_signal is None:
        return RelationshipConfidenceSignal(
            relationship_id=relationship_id,
            confidence=None,
            verdict=TrustedCurrentVerdict.BLOCKED,
            reason="relationship confidence unavailable",
        )
    confidence = float(raw_signal)
    verdict = (
        TrustedCurrentVerdict.ALLOWED if confidence >= low_confidence_threshold else TrustedCurrentVerdict.DOWNRANKED
    )
    return RelationshipConfidenceSignal(relationship_id, confidence, verdict, "record supplied relationship confidence")


def _source_status(value: str) -> SourceFreshnessStatus:
    normalized = value.casefold()
    if normalized == "fresh":
        return SourceFreshnessStatus.FRESH
    if normalized in {"stale", "expired"}:
        return SourceFreshnessStatus.STALE
    if normalized in {"unavailable", "missing", "unreadable"}:
        return SourceFreshnessStatus.UNAVAILABLE
    return SourceFreshnessStatus.UNKNOWN


def _source_expected_hash(source: Any) -> str:
    metadata = _as_mapping(_field(source, "metadata", {}))
    document_audit = _as_mapping(_field(source, "document_audit", {}))
    for values in (metadata, document_audit):
        for key in ("source_hash", "content_hash", "hash", "stored_hash"):
            value = str(values.get(key, "")).strip()
            if value:
                return value
    return ""


def _provenance_evidence(refs: Iterable[Any]) -> set[str]:
    evidence: set[str] = set()
    for ref in refs:
        value = str(_field(ref, "evidence", "")).strip()
        if value:
            evidence.add(value)
    return evidence


def _iter_field(value: Any, field_name: str) -> tuple[Any, ...]:
    raw = _field(value, field_name, ())
    if raw is None:
        return ()
    return tuple(raw)


def _field(record: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(field_name, default)
    return getattr(record, field_name, default)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None


def _ratio(values: Sequence[bool]) -> float:
    if not values:
        return 0.0
    return round(sum(1 for value in values if value) / len(values), 4)


def _dedupe_gaps(gaps: Iterable[MissingEvidenceGap]) -> tuple[MissingEvidenceGap, ...]:
    unique: dict[tuple[str, str, str], MissingEvidenceGap] = {}
    for gap in gaps:
        unique[gap.record_id, gap.evidence_ref, gap.reason] = gap
    return tuple(unique[key] for key in sorted(unique))
