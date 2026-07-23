"""RAG debugger records, errors, and ID validation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class RagDebuggerError(Exception):
    """Base exception for fail-closed retrieval lab failures."""


class RagIndexMissing(RagDebuggerError):
    """Raised when a dataset revision has no inspectable retrieval index."""


class RagEmbeddingModelMismatch(RagDebuggerError):
    """Raised when query and index embedding model identities differ."""


class RagQueryTooLarge(RagDebuggerError):
    """Raised when query text exceeds the bounded inspection size."""


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """A replayable retrieval query plus knobs used by the lab."""

    query_text: str
    top_k: int = 5
    filters: dict[str, str] = field(default_factory=dict)
    rewrite: str = ""
    hyde_document: str = ""
    hybrid_alpha: float = 0.5
    embedding_model: str = "sentence-transformers/all-mpnet-base-v2"
    reranker: str = "none"
    min_score: float = 0.0

    def __post_init__(self) -> None:
        if not self.query_text or not self.query_text.strip():
            raise ValueError("RetrievalQuery.query_text must be non-empty")
        if self.top_k < 1:
            raise ValueError("RetrievalQuery.top_k must be >= 1")
        if not 0.0 <= self.hybrid_alpha <= 1.0:
            raise ValueError("RetrievalQuery.hybrid_alpha must be between 0 and 1")
        if self.min_score < 0:
            raise ValueError("RetrievalQuery.min_score must be >= 0")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetrievalQuery(query_text={self.query_text!r}, top_k={self.top_k!r}, filters={self.filters!r})"


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    """One retrieved or rejected chunk with score and metadata context."""

    chunk_id: str
    document_id: str
    text: str
    score: float
    dense_score: float
    sparse_score: float
    rerank_score: float
    metadata: dict[str, str] = field(default_factory=dict)
    rejected: bool = False
    rejection_reason: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.chunk_id, "RetrievalCandidate.chunk_id")
        _require_non_empty(self.document_id, "RetrievalCandidate.document_id")
        _require_non_empty(self.text, "RetrievalCandidate.text")
        if self.rejected and not self.rejection_reason.strip():
            raise ValueError("rejected candidates require rejection_reason")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetrievalCandidate(chunk_id={self.chunk_id!r}, document_id={self.document_id!r}, text={self.text!r})"


@dataclass(frozen=True, slots=True)
class RetrievalTrace:
    """Full retrieval decision trace for one replay."""

    revision_id: str
    query: RetrievalQuery
    candidates: tuple[RetrievalCandidate, ...]
    rejected_candidates: tuple[RetrievalCandidate, ...]
    filters_applied: tuple[str, ...]
    reranker: str
    embedding_model: str
    index_embedding_model: str
    collection: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetrievalTrace(revision_id={self.revision_id!r}, query={self.query!r}, candidates={self.candidates!r})"


@dataclass(frozen=True, slots=True)
class RerankBreakdown:
    """Pre-rerank versus post-rerank score explanation for one candidate."""

    candidate_id: str
    pre_rerank_score: float
    post_rerank_score: float
    delta: float
    dense_weight: float
    sparse_weight: float
    metadata_filters: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RerankBreakdown(candidate_id={self.candidate_id!r}, pre_rerank_score={self.pre_rerank_score!r}, post_rerank_score={self.post_rerank_score!r})"


@dataclass(frozen=True, slots=True)
class ContextAssembly:
    """The final context window and per-chunk inclusion decisions."""

    context_text: str
    included_chunk_ids: tuple[str, ...]
    excluded_chunks: tuple[tuple[str, str], ...]
    token_count: int
    source_coverage: dict[str, int] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ContextAssembly(context_text={self.context_text!r}, included_chunk_ids={self.included_chunk_ids!r}, excluded_chunks={self.excluded_chunks!r})"


@dataclass(frozen=True, slots=True)
class RagFaithfulnessVerdict:
    """Groundedness and RAGAS-style verdict over the assembled context."""

    passed: bool
    groundedness_score: float
    faithfulness_score: float
    unsupported_claims: tuple[str, ...] = ()
    notes: str = ""
    answer_relevance_score: float = 0.0
    context_recall_score: float = 0.0
    context_precision_score: float = 0.0

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            "RagFaithfulnessVerdict("
            f"passed={self.passed!r}, groundedness_score={self.groundedness_score!r}, "
            f"faithfulness_score={self.faithfulness_score!r}, "
            f"answer_relevance_score={self.answer_relevance_score!r})"
        )


@dataclass(frozen=True, slots=True)
class RetrievalLabExperiment:
    """Persisted replay result that can be promoted to an eval case."""

    experiment_id: str
    project_id: str
    revision_id: str
    query: RetrievalQuery
    trace: RetrievalTrace
    rerank_breakdown: tuple[RerankBreakdown, ...]
    context_assembly: ContextAssembly
    verdict: RagFaithfulnessVerdict
    created_at_utc: str
    notes: str = ""
    promoted_eval_id: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.experiment_id, "RetrievalLabExperiment.experiment_id")
        _validate_project_id(self.project_id)
        _require_non_empty(self.revision_id, "RetrievalLabExperiment.revision_id")
        _require_non_empty(self.created_at_utc, "RetrievalLabExperiment.created_at_utc")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetrievalLabExperiment(experiment_id={self.experiment_id!r}, project_id={self.project_id!r}, revision_id={self.revision_id!r})"


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _validate_project_id(project_id: str) -> None:
    if not project_id or not _PROJECT_ID_RE.fullmatch(project_id):
        raise RagDebuggerError(f"project_id {project_id!r} fails path-traversal regex")
