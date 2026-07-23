"""Search and query methods for Vetinari's RAG knowledge base.

Contains ``KnowledgeBaseSearch`` — the class providing all private
query methods for ``KnowledgeBase``.  Extracted from ``knowledge_base.py``
to keep that module under the 550-line file limit and ``KnowledgeBase``
under the 500-line class limit.

``KnowledgeBase`` inherits from this search behavior and calls all methods via
``self``.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from typing import Any, Protocol, cast

from vetinari.rag.knowledge_base_helpers import (
    KBDocument,
    pack_embedding,
    unpack_embedding,
)
from vetinari.utils.math_helpers import cosine_similarity

logger = logging.getLogger(__name__)


class _KnowledgeBaseSearchOwner(Protocol):
    _conn: sqlite3.Connection


class KnowledgeBaseSearch:
    """Private query methods for KnowledgeBase.

    Expects ``self._conn``, ``self._has_vec``, and ``self._embedding_fallbacks``
    /``self._embedding_attempts`` to be set by the host class.
    """

    def _connection(self) -> sqlite3.Connection:
        """Return the host KnowledgeBase SQLite connection."""
        return cast(_KnowledgeBaseSearchOwner, self)._conn

    # ── Compression ───────────────────────────────────────────────────

    @staticmethod
    def _compress_results(
        docs: list[KBDocument],
        max_chars: int,
    ) -> list[KBDocument]:
        """Apply prompt compression to retrieved documents when over budget.

        Uses PerplexityCompressor to remove low-information content while
        preserving structural elements like headers and code blocks.

        Args:
            docs: Retrieved documents.
            max_chars: Maximum character budget.

        Returns:
            Documents with compressed content if over budget.
        """
        total = sum(len(d.content) for d in docs)
        if total <= max_chars or not docs:
            return docs

        try:
            from vetinari.optimization.prompt_compressor import PerplexityCompressor

            compressor = PerplexityCompressor()
            target_ratio = max_chars / total if total > 0 else 1.0
            compressed_docs = list(docs)
            for index, doc in enumerate(compressed_docs):
                if len(doc.content) > 200:
                    compressed_docs[index] = replace(doc, content=compressor.compress(doc.content, target_ratio))
            return compressed_docs
        except Exception:
            logger.warning("Prompt compressor unavailable for RAG compression")

        return docs

    # ── Vector KNN ────────────────────────────────────────────────────

    def _query_vec_knn(
        self,
        query_vec: list[float],
        k: int,
        category: str | None,
        *,
        fallback_query: str,
    ) -> list[KBDocument]:
        """Search using sqlite-vec KNN (O(log n)).

        Args:
            query_vec: Query embedding vector.
            k: Number of results.
            category: Optional category filter.
            fallback_query: Original text query used when vectors are unavailable.

        Returns:
            Ranked list of KBDocument.
        """
        cursor = self._connection().cursor()
        query_blob = pack_embedding(query_vec)
        fetch_limit = k * 3

        try:
            cursor.execute(
                "SELECT doc_id, distance FROM doc_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (query_blob, fetch_limit),
            )
            knn_results = cursor.fetchall()
        except sqlite3.Error as exc:
            logger.warning("KB vec KNN search failed: %s -- falling back to cosine", exc)
            return self._query_cosine(query_vec, k, category, fallback_query=fallback_query)

        if not knn_results:
            return self._query_fts(fallback_query, k, category)

        candidates: list[tuple[str, float]] = [
            (str(row["doc_id"]), 1.0 / (1.0 + float(row["distance"]))) for row in knn_results
        ]
        return self._fetch_documents(candidates, k, category)

    # ── Cosine fallback ───────────────────────────────────────────────

    def _query_cosine(
        self,
        query_vec: list[float],
        k: int,
        category: str | None,
        *,
        fallback_query: str,
    ) -> list[KBDocument]:
        """Search using manual O(n) cosine similarity (fallback).

        Args:
            query_vec: Query embedding vector.
            k: Number of results.
            category: Optional category filter.
            fallback_query: Original text query used when no embeddings are available.

        Returns:
            Ranked list of KBDocument.
        """
        cursor = self._connection().cursor()

        conditions = []
        params: list[Any] = []
        if category:
            conditions.append("d.category = ?")
            params.append(category)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor.execute(
            f"SELECT e.doc_id, e.embedding_blob FROM doc_embeddings e JOIN documents d ON e.doc_id = d.doc_id {where}",
            params,
        )

        scored: list[tuple[str, float]] = []
        for row in cursor.fetchall():
            mem_vec = unpack_embedding(row["embedding_blob"])
            sim = cosine_similarity(query_vec, mem_vec)
            scored.append((row["doc_id"], sim))

        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:k]

        if not top:
            return self._query_fts(fallback_query, k, category)

        return self._fetch_documents(top, k, category=None)

    # ── FTS5 ─────────────────────────────────────────────────────────

    def _query_fts(
        self,
        query: str,
        k: int,
        category: str | None,
    ) -> list[KBDocument]:
        """Search using FTS5 with BM25 ranking (keyword fallback).

        Args:
            query: Search query string.
            k: Number of results.
            category: Optional category filter.

        Returns:
            Ranked list of KBDocument.
        """
        cursor = self._connection().cursor()

        if not query or not query.strip():
            # An empty query has no semantic meaning — returning recency hits
            # would silently mislead callers into thinking they got real results.
            # Return an empty list so callers can decide what to do next.
            return []

        try:
            fts_query = " OR ".join(query.split()[:10])
            fts_conditions = ["doc_fts MATCH ?"]
            fts_params: list[Any] = [fts_query]
            if category:
                fts_conditions.append("d.category = ?")
                fts_params.append(category)
            fts_params.append(k)
            fts_where = " AND ".join(fts_conditions)
            cursor.execute(
                f"SELECT d.*, rank FROM doc_fts f "
                f"JOIN documents d ON f.doc_id = d.doc_id "
                f"WHERE {fts_where} "
                "ORDER BY rank "
                "LIMIT ?",
                fts_params,
            )
            results = cursor.fetchall()
            if results:
                return [self._row_to_doc(row, score=abs(row["rank"])) for row in results]
        except sqlite3.Error:
            logger.warning("FTS5 query failed, falling back to keyword overlap", exc_info=True)

        return self._query_keyword_overlap(query, k, category)

    # ── Keyword overlap (last resort) ─────────────────────────────────

    def _query_keyword_overlap(
        self,
        query: str,
        k: int,
        category: str | None,
    ) -> list[KBDocument]:
        """Simple keyword overlap search (last resort).

        Args:
            query: Search query string.
            k: Number of results.
            category: Optional category filter.

        Returns:
            Ranked list of KBDocument.
        """
        cursor = self._connection().cursor()
        conditions = []
        params: list[Any] = []
        if category:
            conditions.append("category = ?")
            params.append(category)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor.execute(f"SELECT * FROM documents {where}", params)

        query_words = set(query.lower().split())
        scored: list[tuple[float, sqlite3.Row]] = []
        for row in cursor.fetchall():
            content_words = set(row["content"].lower().split())
            overlap = len(query_words & content_words)
            if overlap > 0:
                scored.append((overlap / max(len(query_words), 1), row))

        scored.sort(key=lambda x: -x[0])
        return [self._row_to_doc(row, score=score) for score, row in scored[:k]]

    # ── Document fetch ────────────────────────────────────────────────

    def _fetch_documents(
        self,
        doc_ids: Sequence[str | tuple[str, float]],
        k: int,
        category: str | None,
    ) -> list[KBDocument]:
        """Fetch full documents by ID list, preserving order.

        Args:
            doc_ids: Ordered list of document IDs.
            k: Maximum results to return.
            category: Optional category filter.

        Returns:
            List of KBDocument in the original ID order.
        """
        if not doc_ids:
            return []

        ordered_ids: list[str] = []
        score_by_id: dict[str, float] = {}
        for item in doc_ids:
            if isinstance(item, tuple):
                doc_id, score = item
            else:
                doc_id, score = item, 0.0
            ordered_ids.append(doc_id)
            score_by_id[doc_id] = float(score)

        cursor = self._connection().cursor()
        placeholders = ",".join("?" for _ in ordered_ids)
        conditions = [f"doc_id IN ({placeholders})"]
        params: list[Any] = list(ordered_ids)

        if category:
            conditions.append("category = ?")
            params.append(category)

        where = " AND ".join(conditions)
        cursor.execute(f"SELECT * FROM documents WHERE {where}", params)
        docs_by_id = {
            row["doc_id"]: self._row_to_doc(row, score=score_by_id.get(row["doc_id"], 0.0)) for row in cursor.fetchall()
        }

        return [docs_by_id[did] for did in ordered_ids if did in docs_by_id][:k]

    # ── Static helpers ────────────────────────────────────────────────

    @staticmethod
    def _row_to_doc(row: sqlite3.Row, score: float = 0.0) -> KBDocument:
        """Convert a database row to a KBDocument.

        Args:
            row: SQLite row with doc_id, content, source, category columns.
            score: Relevance score to assign.

        Returns:
            KBDocument instance.
        """
        return KBDocument(
            doc_id=row["doc_id"],
            content=row["content"],
            source=row["source"],
            category=row["category"],
            score=score,
        )

    @staticmethod
    def _chunk(text: str, size: int, overlap: int) -> list[str]:
        """Split text into overlapping chunks.

        Args:
            text: Text to split.
            size: Maximum chunk size in characters.
            overlap: Overlap between consecutive chunks.  Must be strictly less
                than ``size``; if equal or greater the step would be zero or
                negative and the loop would never advance.

        Returns:
            List of text chunks.

        Raises:
            ValueError: If ``size`` <= 0 or ``overlap`` >= ``size``.
        """
        if size <= 0:
            raise ValueError(f"chunk size must be > 0, got {size}")
        if overlap >= size:
            raise ValueError(
                f"overlap ({overlap}) must be strictly less than size ({size}); "
                "otherwise the chunking loop cannot advance"
            )
        if len(text) <= size:
            return [text]
        chunks = []
        step = size - overlap  # guaranteed > 0 by the guard above
        i = 0
        while i < len(text):
            chunks.append(text[i : i + size])
            i += step
        return chunks
