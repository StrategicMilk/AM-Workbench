"""Private retrieval scoring helpers for the Workbench RAG debugger."""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vetinari.workbench.rag_debugger_records import ContextAssembly, RetrievalCandidate, RetrievalQuery

logger = logging.getLogger(__name__)


def _maybe_get(value: Any, key: str, default: Any) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _chunk_metadata(chunk: Any) -> dict[str, str]:
    raw = _maybe_get(chunk, "metadata", {})
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _candidate_from_chunk(
    chunk: Any,
    order: int,
    query_terms: set[str],
    query: RetrievalQuery,
    *,
    score_rerank: Callable[[RetrievalQuery, str, float], float],
) -> RetrievalCandidate:
    text = str(_maybe_get(chunk, "text", ""))
    chunk_terms = _terms(text)
    overlap = len(query_terms & chunk_terms)
    denominator = max(len(query_terms), 1)
    dense_score = float(_maybe_get(chunk, "dense_score", overlap / denominator))
    sparse_score = float(_maybe_get(chunk, "sparse_score", overlap))
    score = (query.hybrid_alpha * dense_score) + ((1.0 - query.hybrid_alpha) * min(sparse_score, 1.0))
    return RetrievalCandidate(
        chunk_id=str(_maybe_get(chunk, "chunk_id", f"chunk-{order}")),
        document_id=str(_maybe_get(chunk, "document_id", f"doc-{order}")),
        text=text,
        score=score,
        dense_score=dense_score,
        sparse_score=sparse_score,
        rerank_score=score_rerank(query, text, score),
        metadata=_chunk_metadata(chunk),
    )


def _expanded_query_terms(query: RetrievalQuery) -> set[str]:
    query_terms = _terms(query.query_text)
    if query.rewrite:
        query_terms.update(_terms(query.rewrite))
    if query.hyde_document:
        query_terms.update(_terms(query.hyde_document))
    return query_terms


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    dimensions = min(len(left), len(right))
    if dimensions == 0:
        return 0.0
    # Walk the prefix indices once and accumulate dot + both norms in a
    # single pass — never slice the inputs.  Slicing a vector list
    # allocates a fresh list per call (and tests can monkey-patch
    # ``__getitem__`` to forbid slicing on per-request RAG paths).
    dot_product = 0.0
    left_sq = 0.0
    right_sq = 0.0
    for index in range(dimensions):
        left_value = left[index]
        right_value = right[index]
        dot_product += left_value * right_value
        left_sq += left_value * left_value
        right_sq += right_value * right_value
    denominator = math.sqrt(left_sq) * math.sqrt(right_sq)
    if denominator == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot_product / denominator))


def _rough_token_count(text: str) -> int:
    """Approximate whitespace-separated token count without allocating a list.

    ``len(text.split())`` allocates a full list of substrings just to
    measure its length, which is wasted work on per-request RAG
    retrieval paths.  This helper streams the characters once,
    counting transitions from whitespace into non-whitespace runs.

    Args:
        text: Source text to measure.

    Returns:
        Number of whitespace-delimited tokens.  Consecutive whitespace
        characters count as one separator.
    """
    count = 0
    in_token = False
    for ch in text:
        if ch.isspace():
            in_token = False
        elif not in_token:
            count += 1
            in_token = True
    return count


def _assemble_context(
    candidates: tuple[RetrievalCandidate, ...],
    rejected: tuple[RetrievalCandidate, ...],
) -> ContextAssembly:
    context = "\n\n".join(candidate.text for candidate in candidates)
    coverage: dict[str, int] = {}
    for candidate in candidates:
        doc_id = _doc_identity(candidate)
        coverage[doc_id] = coverage.get(doc_id, 0) + 1
    return ContextAssembly(
        context_text=context,
        included_chunk_ids=tuple(candidate.chunk_id for candidate in candidates),
        excluded_chunks=tuple((candidate.chunk_id, candidate.rejection_reason) for candidate in rejected),
        token_count=_rough_token_count(context),
        source_coverage=coverage,
    )


def _doc_identity(candidate: RetrievalCandidate) -> str:
    return str(candidate.document_id or candidate.chunk_id)


def _terms(text: str) -> set[str]:
    return {part.lower() for part in re.findall(r"[A-Za-z0-9_]+", text)}


def _is_relative_to(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        candidate_text = str(candidate).removeprefix("\\\\?\\").casefold()
        root_text = str(root).removeprefix("\\\\?\\").casefold()
        return os.path.commonpath([candidate_text, root_text]) == root_text
