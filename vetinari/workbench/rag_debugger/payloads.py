"""Payload serialization helpers for the RAG debugger experiment log."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from vetinari.workbench.rag_debugger_records import (
    ContextAssembly,
    RagFaithfulnessVerdict,
    RerankBreakdown,
    RetrievalCandidate,
    RetrievalLabExperiment,
    RetrievalQuery,
    RetrievalTrace,
)

_MAX_RAG_CANDIDATES = 1_000
_MAX_RAG_BREAKDOWNS = 1_000
_MAX_RAG_FILTERS = 100
_MAX_CONTEXT_CHUNKS = 1_000
_MAX_UNSUPPORTED_CLAIMS = 100


def to_jsonable(value: Any) -> Any:
    """Return a JSON-serializable representation of a dataclass payload.

    Returns:
        A recursively JSON-safe value.
    """
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field_item.name: to_jsonable(getattr(value, field_item.name)) for field_item in fields(value)}
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value


def experiment_from_payload(payload: dict[str, Any]) -> RetrievalLabExperiment:
    """Rehydrate a stored experiment payload.

    Returns:
        The reconstructed retrieval-lab experiment.
    """
    query = query_from_payload(payload["query"])
    trace = trace_from_payload(payload["trace"])
    return RetrievalLabExperiment(
        experiment_id=payload["experiment_id"],
        project_id=payload["project_id"],
        revision_id=payload["revision_id"],
        query=query,
        trace=trace,
        rerank_breakdown=tuple(
            breakdown_from_payload(row) for row in _bounded_rows(payload, "rerank_breakdown", _MAX_RAG_BREAKDOWNS)
        ),
        context_assembly=context_from_payload(payload["context_assembly"]),
        verdict=verdict_from_payload(payload["verdict"]),
        created_at_utc=payload["created_at_utc"],
        notes=payload.get("notes", ""),
        promoted_eval_id=payload.get("promoted_eval_id", ""),
    )


def query_from_payload(payload: dict[str, Any]) -> RetrievalQuery:
    """Rehydrate a stored retrieval query."""
    return RetrievalQuery(
        query_text=payload["query_text"],
        top_k=int(payload["top_k"]),
        filters={str(key): str(value) for key, value in payload.get("filters", {}).items()},
        rewrite=payload.get("rewrite", ""),
        hyde_document=payload.get("hyde_document", ""),
        hybrid_alpha=float(payload.get("hybrid_alpha", 0.5)),
        embedding_model=payload.get("embedding_model", "sentence-transformers/all-mpnet-base-v2"),
        reranker=payload.get("reranker", "none"),
        min_score=float(payload.get("min_score", 0.0)),
    )


def candidate_from_payload(payload: dict[str, Any]) -> RetrievalCandidate:
    """Rehydrate a stored retrieval candidate."""
    return RetrievalCandidate(
        chunk_id=payload["chunk_id"],
        document_id=payload["document_id"],
        text=payload["text"],
        score=float(payload["score"]),
        dense_score=float(payload["dense_score"]),
        sparse_score=float(payload["sparse_score"]),
        rerank_score=float(payload["rerank_score"]),
        metadata={str(key): str(value) for key, value in payload.get("metadata", {}).items()},
        rejected=bool(payload.get("rejected")),
        rejection_reason=payload.get("rejection_reason", ""),
    )


def trace_from_payload(payload: dict[str, Any]) -> RetrievalTrace:
    """Rehydrate a stored retrieval trace."""
    return RetrievalTrace(
        revision_id=payload["revision_id"],
        query=query_from_payload(payload["query"]),
        candidates=tuple(
            candidate_from_payload(row) for row in _bounded_rows(payload, "candidates", _MAX_RAG_CANDIDATES)
        ),
        rejected_candidates=tuple(
            candidate_from_payload(row) for row in _bounded_rows(payload, "rejected_candidates", _MAX_RAG_CANDIDATES)
        ),
        filters_applied=tuple(_bounded_rows(payload, "filters_applied", _MAX_RAG_FILTERS, default=())),
        reranker=payload["reranker"],
        embedding_model=payload["embedding_model"],
        index_embedding_model=payload["index_embedding_model"],
        collection=payload["collection"],
    )


def breakdown_from_payload(payload: dict[str, Any]) -> RerankBreakdown:
    """Rehydrate a stored rerank breakdown."""
    return RerankBreakdown(
        candidate_id=payload["candidate_id"],
        pre_rerank_score=float(payload["pre_rerank_score"]),
        post_rerank_score=float(payload["post_rerank_score"]),
        delta=float(payload["delta"]),
        dense_weight=float(payload["dense_weight"]),
        sparse_weight=float(payload["sparse_weight"]),
        metadata_filters=tuple(payload.get("metadata_filters", ())),
    )


def context_from_payload(payload: dict[str, Any]) -> ContextAssembly:
    """Rehydrate a stored context assembly."""
    return ContextAssembly(
        context_text=payload["context_text"],
        included_chunk_ids=tuple(_bounded_rows(payload, "included_chunk_ids", _MAX_CONTEXT_CHUNKS)),
        excluded_chunks=tuple(tuple(row) for row in _bounded_rows(payload, "excluded_chunks", _MAX_CONTEXT_CHUNKS)),
        token_count=int(payload["token_count"]),
        source_coverage={str(key): int(value) for key, value in payload.get("source_coverage", {}).items()},
    )


def verdict_from_payload(payload: dict[str, Any]) -> RagFaithfulnessVerdict:
    """Rehydrate a stored faithfulness verdict."""
    return RagFaithfulnessVerdict(
        passed=bool(payload["passed"]),
        groundedness_score=float(payload["groundedness_score"]),
        faithfulness_score=float(payload["faithfulness_score"]),
        unsupported_claims=tuple(_bounded_rows(payload, "unsupported_claims", _MAX_UNSUPPORTED_CLAIMS, default=())),
        notes=payload.get("notes", ""),
        answer_relevance_score=float(payload.get("answer_relevance_score", 0.0)),
        context_recall_score=float(payload.get("context_recall_score", 0.0)),
        context_precision_score=float(payload.get("context_precision_score", 0.0)),
    )


def _bounded_rows(
    payload: dict[str, Any],
    key: str,
    max_items: int,
    *,
    default: tuple[Any, ...] | None = None,
) -> tuple[Any, ...]:
    rows = payload.get(key, default) if default is not None else payload[key]
    if not isinstance(rows, list | tuple):
        raise ValueError(f"{key} must be a list")
    if len(rows) > max_items:
        raise ValueError(f"{key} exceeds max_items={max_items}")
    return tuple(rows)
