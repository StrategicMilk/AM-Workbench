"""Contracts for runtime outcome records and proposals."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any


class OutcomeFailureKind(str, Enum):
    """Failures captured across query, retrieval, runtime, and governance stages."""

    CITATION_FAILURE = "citation_failure"
    PARSER_FAILURE = "parser_failure"
    DUPLICATE_CHUNK = "duplicate_chunk"
    EMBEDDING_DRIFT = "embedding_drift"
    PROVIDER_ACCEPTANCE_FAILED = "provider_acceptance_failed"
    FALLBACK_USED = "fallback_used"
    CANCELLATION = "cancellation"
    QUEUE_DELAY = "queue_delay"
    MEMORY_PRESSURE = "memory_pressure"
    GPU_PRESSURE = "gpu_pressure"
    COST_EXCEEDED = "cost_exceeded"
    LATENCY_EXCEEDED = "latency_exceeded"


class OutcomeStage(str, Enum):
    """Query-to-answer stages scored by the data mart."""

    QUERY = "query"
    RETRIEVAL = "retrieval"
    RERANK = "rerank"
    CONTEXT = "context"
    ANSWER = "answer"
    RUNTIME = "runtime"


class OutcomeProposalKind(str, Enum):
    """Downstream proposal classes emitted from outcome records."""

    MODEL_DOWNLOAD = "model_download"
    ROUTE_POLICY_CHANGE = "route_policy_change"
    RAG_TUNING = "rag_tuning"
    SOURCE_REFRESH = "source_refresh"
    RUNTIME_REMEDIATION = "runtime_remediation"
    QUANTIZATION_REVIEW = "quantization_review"


@dataclass(frozen=True, slots=True)
class OutcomeMartPolicy:
    """Thresholds used to decide whether an outcome record is trusted."""

    min_stage_score: float = 0.75
    min_confidence: float = 0.8
    max_latency_ms: int = 2_000
    max_queue_delay_ms: int = 500
    max_token_cost_usd: float = 0.05
    max_memory_pressure: float = 0.85
    max_gpu_pressure: float = 0.85

    def __post_init__(self) -> None:
        _require_probability(self.min_stage_score, "min_stage_score")
        _require_probability(self.min_confidence, "min_confidence")
        if self.max_latency_ms < 0:
            raise ValueError("max_latency_ms must be non-negative")
        if self.max_queue_delay_ms < 0:
            raise ValueError("max_queue_delay_ms must be non-negative")
        if self.max_token_cost_usd < 0:
            raise ValueError("max_token_cost_usd must be non-negative")
        _require_probability(self.max_memory_pressure, "max_memory_pressure")
        _require_probability(self.max_gpu_pressure, "max_gpu_pressure")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OutcomeMartPolicy(min_stage_score={self.min_stage_score!r}, min_confidence={self.min_confidence!r}, max_latency_ms={self.max_latency_ms!r})"


@dataclass(frozen=True, slots=True)
class OutcomeStageScore:
    """One stage score with evidence and failure vocabulary."""

    stage: OutcomeStage
    score: float
    evidence_refs: tuple[str, ...]
    failure_kinds: tuple[OutcomeFailureKind, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.stage, OutcomeStage):
            raise ValueError("stage must be OutcomeStage")
        _require_probability(self.score, "score")
        _require_string_tuple(self.evidence_refs, "evidence_refs")
        for failure in self.failure_kinds:
            if not isinstance(failure, OutcomeFailureKind):
                raise ValueError("failure_kinds must contain OutcomeFailureKind values")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OutcomeStageScore(stage={self.stage!r}, score={self.score!r}, evidence_refs={self.evidence_refs!r})"


@dataclass(frozen=True, slots=True)
class ResourcePressure:
    """Runtime and capacity measurements for one RAG request."""

    latency_ms: int
    token_cost_usd: float
    memory_pressure: float
    gpu_pressure: float
    queue_delay_ms: int = 0
    fallback_used: bool = False
    cancelled: bool = False
    provider_acceptance_failed: bool = False

    def __post_init__(self) -> None:
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        if self.queue_delay_ms < 0:
            raise ValueError("queue_delay_ms must be non-negative")
        if self.token_cost_usd < 0 or not isfinite(self.token_cost_usd):
            raise ValueError("token_cost_usd must be finite and non-negative")
        _require_probability(self.memory_pressure, "memory_pressure")
        _require_probability(self.gpu_pressure, "gpu_pressure")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ResourcePressure(latency_ms={self.latency_ms!r}, token_cost_usd={self.token_cost_usd!r}, memory_pressure={self.memory_pressure!r})"


@dataclass(frozen=True, slots=True)
class RuntimeOutcomeGovernance:
    """Governance fields required before an outcome can influence defaults."""

    evidence_refs: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    authority_refs: tuple[str, ...]
    safety_refs: tuple[str, ...]
    budget_ref: str
    persisted_state_ref: str
    confidence: float | None

    def __post_init__(self) -> None:
        _require_string_tuple(self.evidence_refs, "evidence_refs", allow_empty=True)
        _require_string_tuple(self.provenance_refs, "provenance_refs", allow_empty=True)
        _require_string_tuple(self.authority_refs, "authority_refs", allow_empty=True)
        _require_string_tuple(self.safety_refs, "safety_refs", allow_empty=True)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RuntimeOutcomeGovernance(evidence_refs={self.evidence_refs!r}, provenance_refs={self.provenance_refs!r}, authority_refs={self.authority_refs!r})"


@dataclass(frozen=True, slots=True)
class RetentionGate:
    """Taint and retention controls for private RAG content."""

    content_taint: str = "clean"
    redaction_ref: str = ""
    raw_content_ref: str = ""
    raw_content_retention_days: int = 0

    def __post_init__(self) -> None:
        _require_text(self.content_taint, "content_taint")
        if self.raw_content_retention_days < 0:
            raise ValueError("raw_content_retention_days must be non-negative")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetentionGate(content_taint={self.content_taint!r}, redaction_ref={self.redaction_ref!r}, raw_content_ref={self.raw_content_ref!r})"


@dataclass(frozen=True, slots=True)
class OutcomeRecord:
    """One query-to-answer and runtime outcome row."""

    outcome_id: str
    project_id: str
    query_digest: str
    answer_digest: str
    model_version_id: str
    runtime_ref: str
    retrieval_index_ref: str
    source_health_report_id: str
    captured_at_utc: str
    stage_scores: tuple[OutcomeStageScore, ...]
    resource_pressure: ResourcePressure
    governance: RuntimeOutcomeGovernance
    retention: RetentionGate = field(default_factory=RetentionGate)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.outcome_id, "outcome_id")
        _require_text(self.project_id, "project_id")
        _require_text(self.query_digest, "query_digest")
        _require_text(self.answer_digest, "answer_digest")
        _require_text(self.model_version_id, "model_version_id")
        _require_text(self.runtime_ref, "runtime_ref")
        _require_text(self.retrieval_index_ref, "retrieval_index_ref")
        _require_text(self.source_health_report_id, "source_health_report_id")
        _parse_utc(self.captured_at_utc, "captured_at_utc")
        if not self.stage_scores:
            raise ValueError("stage_scores must be non-empty")
        for score in self.stage_scores:
            if not isinstance(score, OutcomeStageScore):
                raise ValueError("stage_scores must contain OutcomeStageScore instances")
        if not isinstance(self.resource_pressure, ResourcePressure):
            raise ValueError("resource_pressure must be ResourcePressure")
        if not isinstance(self.governance, RuntimeOutcomeGovernance):
            raise ValueError("governance must be RuntimeOutcomeGovernance")
        if not isinstance(self.retention, RetentionGate):
            raise ValueError("retention must be RetentionGate")

    def to_schema_payload(self) -> dict[str, Any]:
        """Return a JSON-schema-compatible record payload."""
        return {
            "outcome_id": self.outcome_id,
            "project_id": self.project_id,
            "query_digest": self.query_digest,
            "answer_digest": self.answer_digest,
            "model_version_id": self.model_version_id,
            "runtime_ref": self.runtime_ref,
            "retrieval_index_ref": self.retrieval_index_ref,
            "source_health_report_id": self.source_health_report_id,
            "captured_at_utc": self.captured_at_utc,
            "stage_scores": [
                {
                    "stage": stage.stage.value,
                    "score": stage.score,
                    "evidence_refs": list(stage.evidence_refs),
                    "failure_kinds": [failure.value for failure in stage.failure_kinds],
                }
                for stage in self.stage_scores
            ],
            "resource_pressure": {
                "latency_ms": self.resource_pressure.latency_ms,
                "token_cost_usd": self.resource_pressure.token_cost_usd,
                "memory_pressure": self.resource_pressure.memory_pressure,
                "gpu_pressure": self.resource_pressure.gpu_pressure,
                "queue_delay_ms": self.resource_pressure.queue_delay_ms,
                "fallback_used": self.resource_pressure.fallback_used,
                "cancelled": self.resource_pressure.cancelled,
                "provider_acceptance_failed": self.resource_pressure.provider_acceptance_failed,
            },
            "governance": {
                "evidence_refs": list(self.governance.evidence_refs),
                "provenance_refs": list(self.governance.provenance_refs),
                "authority_refs": list(self.governance.authority_refs),
                "safety_refs": list(self.governance.safety_refs),
                "budget_ref": self.governance.budget_ref,
                "persisted_state_ref": self.governance.persisted_state_ref,
                "confidence": self.governance.confidence,
            },
            "retention": {
                "content_taint": self.retention.content_taint,
                "redaction_ref": self.retention.redaction_ref,
                "raw_content_ref": self.retention.raw_content_ref,
                "raw_content_retention_days": self.retention.raw_content_retention_days,
            },
            "metadata": dict(self.metadata),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OutcomeRecord(outcome_id={self.outcome_id!r}, project_id={self.project_id!r}, query_digest={self.query_digest!r})"


@dataclass(frozen=True, slots=True)
class OutcomeDecision:
    """Fail-closed trust decision for one outcome row."""

    outcome_id: str
    accepted: bool
    blockers: tuple[str, ...]
    degraded_status: str
    evidence_refs: tuple[str, ...]
    failure_kinds: tuple[OutcomeFailureKind, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"OutcomeDecision(outcome_id={self.outcome_id!r}, accepted={self.accepted!r}, blockers={self.blockers!r})"
        )


@dataclass(frozen=True, slots=True)
class OutcomeProposal:
    """Candidate downstream action derived from accepted outcome evidence."""

    proposal_id: str
    kind: OutcomeProposalKind
    reason: str
    source_outcome_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    target_ref: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OutcomeProposal(proposal_id={self.proposal_id!r}, kind={self.kind!r}, reason={self.reason!r})"


def _parse_utc(value: str, field_name: str) -> datetime:
    _require_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _require_probability(value: float | int, field_name: str) -> None:
    numeric = float(value)
    if not isfinite(numeric) or not 0 <= numeric <= 1:
        raise ValueError(f"{field_name} must be finite and between 0 and 1")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise ValueError(f"{field_name} must be a tuple")
    if not allow_empty and not values:
        raise ValueError(f"{field_name} must be non-empty")
    for value in values:
        _require_text(value, f"{field_name} entry")


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
