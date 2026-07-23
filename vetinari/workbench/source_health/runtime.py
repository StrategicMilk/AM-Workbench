"""Fail-closed source-health assessment for Workbench RAG evidence."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from vetinari.workbench.monitoring.signals import (
    MonitoringSignal,
    MonitoringSignalKind,
    MonitoringSignalSeverity,
)
from vetinari.workbench.source_cards import FreshnessPolicy, SourceKind, StalenessAction

logger = logging.getLogger(__name__)


class SourceHealthIssueKind(str, Enum):
    """Health failures tracked before RAG evidence can be trusted."""

    STALE_SOURCE = "stale_source"
    PARSER_FAILURE = "parser_failure"
    CITATION_MISMATCH = "citation_mismatch"
    CHUNK_COLLISION = "chunk_collision"
    DUPLICATE_CHUNK = "duplicate_chunk"
    EMBEDDING_DRIFT = "embedding_drift"
    RETRIEVAL_LATENCY = "retrieval_latency"
    MISSING_SOURCE = "missing_source"
    ANSWER_SOURCE_DISAGREEMENT = "answer_source_disagreement"


class SourceHealthSeverity(str, Enum):
    """Severity vocabulary for source-health findings."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class SourceHealthPolicy:
    """Thresholds used to decide whether retrieved evidence is usable."""

    freshness: FreshnessPolicy = field(default_factory=lambda: FreshnessPolicy(86_400, StalenessAction.REJECT))
    max_retrieval_latency_ms: int = 2_000
    min_answer_confidence: float = 0.8
    embedding_model: str = ""
    min_claim_term_coverage: float = 0.5

    def __post_init__(self) -> None:
        if self.max_retrieval_latency_ms < 0:
            raise ValueError("max_retrieval_latency_ms must be non-negative")
        if not 0 <= self.min_answer_confidence <= 1:
            raise ValueError("min_answer_confidence must be between 0 and 1")
        if not 0 <= self.min_claim_term_coverage <= 1:
            raise ValueError("min_claim_term_coverage must be between 0 and 1")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SourceHealthPolicy(freshness={self.freshness!r}, max_retrieval_latency_ms={self.max_retrieval_latency_ms!r}, min_answer_confidence={self.min_answer_confidence!r})"


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    """One source or chunk participating in a retrieval answer."""

    source_id: str
    source_kind: SourceKind | str
    chunk_id: str
    excerpt: str
    observed_at_utc: str | None
    provenance_refs: tuple[str, ...]
    citation_refs: tuple[str, ...]
    parser_status: str = "parsed"
    content_hash: str = ""
    embedding_model: str = ""
    embedding_fingerprint: str = ""
    retrieval_latency_ms: int | None = None
    retrieval_score: float | None = None
    source_card_id: str = ""
    data_revision_id: str = ""

    def __post_init__(self) -> None:
        _require_text(self.source_id, "source_id")
        _require_text(self.chunk_id, "chunk_id")
        _require_text(self.excerpt, "excerpt")
        if not isinstance(self.source_kind, SourceKind):
            SourceKind(str(self.source_kind))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SourceEvidence(source_id={self.source_id!r}, source_kind={self.source_kind!r}, chunk_id={self.chunk_id!r})"


@dataclass(frozen=True, slots=True)
class AnswerEvidence:
    """Answer-side evidence needed before a critical RAG answer can be reused."""

    answer_text: str
    claims: tuple[str, ...]
    cited_source_ids: tuple[str, ...]
    confidence: float | None
    authority_refs: tuple[str, ...]
    safety_refs: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AnswerEvidence(answer_text={self.answer_text!r}, claims={self.claims!r}, cited_source_ids={self.cited_source_ids!r})"


@dataclass(frozen=True, slots=True)
class SourceHealthFinding:
    """One fail-closed source-health finding."""

    issue_kind: SourceHealthIssueKind
    severity: SourceHealthSeverity
    source_id: str
    message: str
    evidence_refs: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SourceHealthFinding(issue_kind={self.issue_kind!r}, severity={self.severity!r}, source_id={self.source_id!r})"


@dataclass(frozen=True, slots=True)
class SourceHealthReport:
    """Closed-loop RAG source-health report emitted to dependent Workbench lanes."""

    report_id: str
    project_id: str
    passed: bool
    degraded: bool
    findings: tuple[SourceHealthFinding, ...]
    monitoring_signals: tuple[MonitoringSignal, ...]
    eval_refs: tuple[str, ...]
    annotation_refs: tuple[str, ...]
    source_card_refs: tuple[str, ...]
    data_revision_refs: tuple[str, ...]
    model_routing_hints: tuple[str, ...]
    user_trust_labels: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"SourceHealthReport(report_id={self.report_id!r}, project_id={self.project_id!r}, passed={self.passed!r})"
        )


def evaluate_source_health(
    *,
    report_id: str,
    project_id: str,
    sources: tuple[SourceEvidence, ...],
    answer: AnswerEvidence,
    now_utc: datetime | None = None,
    policy: SourceHealthPolicy | None = None,
) -> SourceHealthReport:
    """Evaluate retrieved sources and answer evidence, failing closed on unknowns.

    Returns:
        SourceHealthReport value produced by evaluate_source_health().
    """
    _require_text(report_id, "report_id")
    _require_text(project_id, "project_id")
    selected_policy = policy or SourceHealthPolicy()
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    findings: list[SourceHealthFinding] = []
    by_source = {source.source_id: source for source in sources}

    if not sources:
        findings.append(
            _finding(SourceHealthIssueKind.MISSING_SOURCE, SourceHealthSeverity.CRITICAL, "", "no retrieved sources")
        )

    _append_source_health_findings(findings, sources, now, selected_policy)
    _append_answer_health_findings(findings, by_source, answer, selected_policy)
    _append_claim_coverage_findings(findings, sources, answer, selected_policy)

    return _build_source_health_report(report_id, project_id, sources, findings)


def _append_source_health_findings(
    findings: list[SourceHealthFinding],
    sources: tuple[SourceEvidence, ...],
    now: datetime,
    policy: SourceHealthPolicy,
) -> None:
    seen_chunks: dict[str, str] = {}
    seen_hashes: dict[str, str] = {}
    for source in sources:
        if not source.provenance_refs:
            findings.append(
                _finding(
                    SourceHealthIssueKind.MISSING_SOURCE,
                    SourceHealthSeverity.CRITICAL,
                    source.source_id,
                    "source provenance is unavailable",
                    source.citation_refs,
                )
            )
        if source.parser_status not in {"parsed", "ok"}:
            findings.append(
                _finding(
                    SourceHealthIssueKind.PARSER_FAILURE,
                    SourceHealthSeverity.ERROR,
                    source.source_id,
                    f"parser_status={source.parser_status}",
                    source.provenance_refs,
                )
            )
        _append_freshness_findings(findings, source, now, policy)
        if source.chunk_id in seen_chunks and seen_chunks[source.chunk_id] != source.source_id:
            findings.append(
                _finding(
                    SourceHealthIssueKind.CHUNK_COLLISION,
                    SourceHealthSeverity.ERROR,
                    source.source_id,
                    f"chunk_id {source.chunk_id} collides with source {seen_chunks[source.chunk_id]}",
                    source.provenance_refs,
                )
            )
        seen_chunks[source.chunk_id] = source.source_id
        if source.content_hash:
            if source.content_hash in seen_hashes and seen_hashes[source.content_hash] != source.source_id:
                findings.append(
                    _finding(
                        SourceHealthIssueKind.DUPLICATE_CHUNK,
                        SourceHealthSeverity.WARNING,
                        source.source_id,
                        f"content_hash duplicates source {seen_hashes[source.content_hash]}",
                        source.provenance_refs,
                    )
                )
            seen_hashes[source.content_hash] = source.source_id
        if policy.embedding_model and source.embedding_model != policy.embedding_model:
            findings.append(
                _finding(
                    SourceHealthIssueKind.EMBEDDING_DRIFT,
                    SourceHealthSeverity.ERROR,
                    source.source_id,
                    f"embedding_model {source.embedding_model!r} differs from {policy.embedding_model!r}",
                    source.provenance_refs,
                )
            )
        if source.retrieval_latency_ms is not None and source.retrieval_latency_ms > policy.max_retrieval_latency_ms:
            findings.append(
                _finding(
                    SourceHealthIssueKind.RETRIEVAL_LATENCY,
                    SourceHealthSeverity.WARNING,
                    source.source_id,
                    f"retrieval latency {source.retrieval_latency_ms}ms exceeded policy",
                    source.provenance_refs,
                )
            )


def _append_answer_health_findings(
    findings: list[SourceHealthFinding],
    by_source: dict[str, SourceEvidence],
    answer: AnswerEvidence,
    policy: SourceHealthPolicy,
) -> None:
    if not answer.answer_text.strip():
        findings.append(
            _finding(
                SourceHealthIssueKind.ANSWER_SOURCE_DISAGREEMENT,
                SourceHealthSeverity.CRITICAL,
                "",
                "answer text is unavailable",
            )
        )
    if not answer.cited_source_ids:
        findings.append(
            _finding(
                SourceHealthIssueKind.CITATION_MISMATCH,
                SourceHealthSeverity.CRITICAL,
                "",
                "answer has claims but no citations",
            )
        )
    missing_citations = [source_id for source_id in answer.cited_source_ids if source_id not in by_source]
    findings.extend(
        _finding(
            SourceHealthIssueKind.CITATION_MISMATCH,
            SourceHealthSeverity.CRITICAL,
            source_id,
            "answer cites a source that was not retrieved",
        )
        for source_id in missing_citations
    )
    if answer.confidence is None or answer.confidence < policy.min_answer_confidence:
        findings.append(
            _finding(
                SourceHealthIssueKind.ANSWER_SOURCE_DISAGREEMENT,
                SourceHealthSeverity.CRITICAL,
                "",
                "answer confidence is unavailable or below policy",
            )
        )
    if not answer.authority_refs or not answer.safety_refs:
        findings.append(
            _finding(
                SourceHealthIssueKind.ANSWER_SOURCE_DISAGREEMENT,
                SourceHealthSeverity.CRITICAL,
                "",
                "authority or safety evidence is unavailable",
                answer.authority_refs + answer.safety_refs,
            )
        )


def _build_source_health_report(
    report_id: str,
    project_id: str,
    sources: tuple[SourceEvidence, ...],
    findings: list[SourceHealthFinding],
) -> SourceHealthReport:
    failed = any(item.severity in {SourceHealthSeverity.ERROR, SourceHealthSeverity.CRITICAL} for item in findings)
    refs = tuple(
        dict.fromkeys(ref for source in sources for ref in source.provenance_refs + source.citation_refs if ref)
    )
    signals = () if not findings else (_monitoring_signal(report_id, project_id, findings, refs),)
    return SourceHealthReport(
        report_id=report_id,
        project_id=project_id,
        passed=not failed,
        degraded=bool(findings),
        findings=tuple(findings),
        monitoring_signals=signals,
        eval_refs=(f"source-health-eval:{report_id}",),
        annotation_refs=tuple(f"source-health-annotation:{finding.issue_kind.value}" for finding in findings),
        source_card_refs=tuple(dict.fromkeys(source.source_card_id for source in sources if source.source_card_id)),
        data_revision_refs=tuple(
            dict.fromkeys(source.data_revision_id for source in sources if source.data_revision_id)
        ),
        model_routing_hints=("demote_rag_route",) if failed else ("eligible_for_critical_answer_reuse",),
        user_trust_labels=("source-health-degraded",) if findings else ("source-health-clear",),
    )


def _append_freshness_findings(
    findings: list[SourceHealthFinding],
    source: SourceEvidence,
    now: datetime,
    policy: SourceHealthPolicy,
) -> None:
    if source.observed_at_utc is None:
        findings.append(
            _finding(
                SourceHealthIssueKind.STALE_SOURCE,
                SourceHealthSeverity.CRITICAL,
                source.source_id,
                "source freshness is unknown",
                source.provenance_refs,
            )
        )
        return
    try:
        observed = datetime.fromisoformat(source.observed_at_utc.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        findings.append(
            _finding(
                SourceHealthIssueKind.STALE_SOURCE,
                SourceHealthSeverity.CRITICAL,
                source.source_id,
                "source observed_at_utc is malformed",
                source.provenance_refs,
            )
        )
        return
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    age = int((now - observed.astimezone(timezone.utc)).total_seconds())
    if age < 0 or age > policy.freshness.max_age_seconds:
        findings.append(
            _finding(
                SourceHealthIssueKind.STALE_SOURCE,
                SourceHealthSeverity.ERROR,
                source.source_id,
                f"source age {age}s violates freshness policy",
                source.provenance_refs,
            )
        )


def _append_claim_coverage_findings(
    findings: list[SourceHealthFinding],
    sources: tuple[SourceEvidence, ...],
    answer: AnswerEvidence,
    policy: SourceHealthPolicy,
) -> None:
    corpus_terms = _terms(" ".join(source.excerpt for source in sources))
    for claim in answer.claims:
        claim_terms = _terms(claim)
        if not claim_terms:
            continue
        coverage = len(claim_terms & corpus_terms) / max(len(claim_terms), 1)
        if coverage < policy.min_claim_term_coverage:
            findings.append(
                _finding(
                    SourceHealthIssueKind.ANSWER_SOURCE_DISAGREEMENT,
                    SourceHealthSeverity.CRITICAL,
                    "",
                    f"claim term coverage {coverage:.2f} below policy for {claim!r}",
                )
            )


def _monitoring_signal(
    report_id: str,
    project_id: str,
    findings: list[SourceHealthFinding],
    evidence_refs: tuple[str, ...],
) -> MonitoringSignal:
    critical = any(item.severity is SourceHealthSeverity.CRITICAL for item in findings)
    return MonitoringSignal(
        signal_id=f"source-health-{report_id}",
        kind=MonitoringSignalKind.RETRIEVAL_FAILURE,
        project_id=project_id,
        run_id=report_id,
        endpoint_id="workbench-rag",
        asset_id=report_id,
        severity=MonitoringSignalSeverity.CRITICAL if critical else MonitoringSignalSeverity.ERROR,
        score=float(len(findings)),
        threshold=1.0,
        evidence_refs=evidence_refs or tuple(f"finding:{item.issue_kind.value}" for item in findings),
        captured_at_utc=datetime.now(timezone.utc).isoformat(),
        routing_hint="source_health",
    )


def _finding(
    issue_kind: SourceHealthIssueKind,
    severity: SourceHealthSeverity,
    source_id: str,
    message: str,
    evidence_refs: tuple[str, ...] = (),
) -> SourceHealthFinding:
    return SourceHealthFinding(
        issue_kind=issue_kind,
        severity=severity,
        source_id=source_id,
        message=message,
        evidence_refs=tuple(ref for ref in evidence_refs if str(ref).strip()) or (f"finding:{issue_kind.value}",),
    )


def _terms(text: str) -> set[str]:
    return {part.lower() for part in re.findall(r"[A-Za-z0-9_]+", text)}


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


__all__ = [
    "AnswerEvidence",
    "SourceEvidence",
    "SourceHealthFinding",
    "SourceHealthIssueKind",
    "SourceHealthPolicy",
    "SourceHealthReport",
    "SourceHealthSeverity",
    "evaluate_source_health",
]
