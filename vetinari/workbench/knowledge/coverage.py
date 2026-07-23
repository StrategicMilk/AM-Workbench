"""Knowledge coverage and deprecation signals for Workbench context.

This module is an adapter over already-loaded Workbench knowledge surfaces. It
does not mutate upstream stores and fails closed when provenance, freshness,
hash, confidence, or deprecation signals are missing or unsafe.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Any

from vetinari.workbench.knowledge.coverage_models import (
    CoverageStatus,
    KnowledgeCoverageReport,
    MissingEvidenceGap,
    RelationshipConfidenceSignal,
    SourceFreshnessSignal,
    SourceFreshnessStatus,
    TrustedContextDecision,
    TrustedCurrentContextResult,
    TrustedCurrentVerdict,
)
from vetinari.workbench.knowledge.coverage_signals import (
    _aggregate_status,
    _confidence_signal,
    _CoverageSignals,
    _dedupe_gaps,
    _freshness_signal,
    _ratio,
    _record_evidence_refs,
    _record_id,
    _record_payload,
    _record_relationship_ids,
    _record_source_hashes,
    _record_source_ids,
    _verdict_for_reasons,
)

logger = logging.getLogger(__name__)


SCHEMA_VERSION = 1
LOW_CONFIDENCE_THRESHOLD = 0.7


def evaluate_knowledge_coverage(
    *,
    aks_bundle: Any | None = None,
    backfeed_records: Iterable[Any] = (),
    semantic_snapshot: Any | None = None,
    retrieval_traces: Iterable[Any] = (),
    context_assemblies: Iterable[Any] = (),
    trusted_records: Iterable[Any] = (),
    current_source_hashes: Mapping[str, str] | None = None,
    available_evidence_refs: Iterable[str] | None = None,
    required_evidence_refs: Iterable[str] = (),
    low_confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD,
) -> KnowledgeCoverageReport:
    """Evaluate coverage and trust quality from already-loaded upstream records.

    Returns:
        KnowledgeCoverageReport value produced by evaluate_knowledge_coverage().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not 0.0 <= low_confidence_threshold <= 1.0:
        raise ValueError("low_confidence_threshold must be between 0 and 1")

    try:
        signals = _CoverageSignals.from_inputs(
            aks_bundle=aks_bundle,
            backfeed_records=tuple(backfeed_records),
            semantic_snapshot=semantic_snapshot,
            retrieval_traces=tuple(retrieval_traces),
            context_assemblies=tuple(context_assemblies),
            trusted_records=tuple(trusted_records),
            current_source_hashes=current_source_hashes or {},
            available_evidence_refs=None if available_evidence_refs is None else set(map(str, available_evidence_refs)),
            required_evidence_refs=tuple(str(ref) for ref in required_evidence_refs if str(ref).strip()),
            low_confidence_threshold=low_confidence_threshold,
        )
    except (AttributeError, TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return KnowledgeCoverageReport.blocked("malformed upstream coverage input")

    if not signals.has_any_input:
        return KnowledgeCoverageReport.blocked("no upstream knowledge inputs were available")

    filter_result = _filter_trusted_signals(signals, current_source_hashes or {}, low_confidence_threshold)
    checks = signals.coverage_checks + tuple(
        decision.verdict is TrustedCurrentVerdict.ALLOWED for decision in filter_result.decisions
    )
    coverage_score = _ratio(checks)
    all_gaps = _coverage_gaps_from_filter(signals, filter_result)
    status, verdict = _aggregate_status(
        source_freshness=signals.source_freshness,
        relationship_confidence=signals.relationship_confidence,
        deprecated_entities=signals.deprecated_entities,
        source_hash_drift=signals.source_hash_drift,
        missing_gaps=all_gaps,
        filter_decisions=filter_result.decisions,
    )
    provenance_refs = tuple(sorted(signals.provenance_refs or {"coverage:evaluation"}))
    return KnowledgeCoverageReport(
        coverage_score=coverage_score,
        source_freshness=signals.source_freshness,
        consolidation_count=signals.consolidation_count,
        deprecated_entities=tuple(signals.deprecated_entities),
        source_hash_drift=tuple(signals.source_hash_drift),
        relationship_confidence=signals.relationship_confidence,
        missing_evidence_gaps=tuple(_dedupe_gaps(all_gaps)),
        trusted_current_verdict=verdict,
        status=status,
        provenance_refs=provenance_refs,
        decisions=filter_result.decisions,
    )


def _filter_trusted_signals(
    signals: _CoverageSignals,
    current_source_hashes: Mapping[str, str],
    low_confidence_threshold: float,
) -> TrustedCurrentContextResult:
    return filter_trusted_current_context(
        signals.trusted_records,
        deprecated_entities=signals.deprecated_entities,
        source_freshness=signals.source_freshness,
        relationship_confidence=signals.relationship_confidence,
        current_source_hashes=current_source_hashes,
        available_evidence_refs=signals.available_evidence_refs,
        required_evidence_refs=signals.required_evidence_refs,
        source_hash_drift=signals.source_hash_drift,
        low_confidence_threshold=low_confidence_threshold,
    )


def _coverage_gaps_from_filter(
    signals: _CoverageSignals,
    filter_result: TrustedCurrentContextResult,
) -> tuple[MissingEvidenceGap, ...]:
    return signals.missing_evidence_gaps + tuple(
        MissingEvidenceGap(
            record_id=decision.record_id,
            evidence_ref=reason.removeprefix("missing_evidence:"),
            reason="trusted context record is missing required evidence",
        )
        for decision in filter_result.decisions
        for reason in decision.reasons
        if reason.startswith("missing_evidence:")
    )


def filter_trusted_current_context(
    records: Iterable[Any],
    *,
    deprecated_entities: Iterable[str] = (),
    source_freshness: Mapping[str, SourceFreshnessSignal | str] | None = None,
    relationship_confidence: Mapping[str, RelationshipConfidenceSignal | float] | None = None,
    current_source_hashes: Mapping[str, str] | None = None,
    available_evidence_refs: Iterable[str] | None = None,
    required_evidence_refs: Iterable[str] = (),
    source_hash_drift: Iterable[str] = (),
    low_confidence_threshold: float = LOW_CONFIDENCE_THRESHOLD,
) -> TrustedCurrentContextResult:
    """Return ordinary trusted-current context only when every signal is safe.

    Returns:
        TrustedCurrentContextResult value produced by filter_trusted_current_context().
    """
    deprecated = set(map(str, deprecated_entities))
    freshness = source_freshness or {}
    confidence = relationship_confidence or {}
    current_hashes = current_source_hashes or {}
    available = None if available_evidence_refs is None else set(map(str, available_evidence_refs))
    global_required = tuple(str(ref) for ref in required_evidence_refs if str(ref).strip())
    drifted_sources = set(map(str, source_hash_drift))

    trusted: list[Mapping[str, Any]] = []
    decisions: list[TrustedContextDecision] = []
    for raw_record in records:
        record = _record_payload(raw_record)
        record_id = _record_id(record)
        entity_id = str(record.get("entity_id") or record.get("subject_ref") or record_id)
        reasons: list[str] = []

        if entity_id in deprecated or record_id in deprecated:
            reasons.append(f"deprecated_entity:{entity_id}")

        for source_id, expected_hash in _record_source_hashes(record).items():
            current_hash = current_hashes.get(source_id)
            if source_id in drifted_sources or (
                current_hash is not None and expected_hash and current_hash != expected_hash
            ):
                reasons.append(f"source_hash_drift:{source_id}")

        for source_id in _record_source_ids(record):
            freshness_signal = _freshness_signal(source_id, freshness)
            if freshness_signal.status is SourceFreshnessStatus.STALE:
                reasons.append(f"stale_source:{source_id}")
            elif freshness_signal.status in {SourceFreshnessStatus.UNKNOWN, SourceFreshnessStatus.UNAVAILABLE}:
                reasons.append(f"{freshness_signal.status.value}_source:{source_id}")

        required = tuple(dict.fromkeys((*global_required, *_record_evidence_refs(record))))
        if available is not None:
            reasons.extend(
                f"missing_evidence:{evidence_ref}" for evidence_ref in required if evidence_ref not in available
            )
        elif not required:
            reasons.append("missing_evidence:record_provenance")

        for relationship_id in _record_relationship_ids(record):
            signal = _confidence_signal(relationship_id, confidence, low_confidence_threshold)
            if signal.verdict is TrustedCurrentVerdict.DOWNRANKED:
                reasons.append(f"low_relationship_confidence:{relationship_id}")
            elif signal.verdict is TrustedCurrentVerdict.BLOCKED:
                reasons.append(f"missing_relationship_confidence:{relationship_id}")

        verdict, rank = _verdict_for_reasons(reasons)
        decision = TrustedContextDecision(
            record_id=record_id,
            entity_id=entity_id,
            verdict=verdict,
            reasons=tuple(reasons),
            rank_multiplier=rank,
            text=str(record.get("text", "")),
        )
        decisions.append(decision)
        if verdict is TrustedCurrentVerdict.ALLOWED:
            trusted.append(record)

    return TrustedCurrentContextResult(trusted_context=tuple(trusted), decisions=tuple(decisions))


__all__ = [
    "CoverageStatus",
    "KnowledgeCoverageReport",
    "MissingEvidenceGap",
    "RelationshipConfidenceSignal",
    "SourceFreshnessSignal",
    "SourceFreshnessStatus",
    "TrustedContextDecision",
    "TrustedCurrentContextResult",
    "TrustedCurrentVerdict",
    "evaluate_knowledge_coverage",
    "filter_trusted_current_context",
]
