"""Memory search operations — read side of the unified memory system.

Extracted from ``unified.py`` (P0 split).  Provides read-path helpers:
BM25 full-text search, semantic (embedding) search, hybrid RRF fusion,
scope-aware queries, and Corrective RAG (CRAG) relevance filtering.

Decision: layered memory with scope inheritance and hybrid search (ADR-0077).
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.boundary_guards import account_evidence_drop, require_nonempty
from vetinari.database import get_connection
from vetinari.memory.interfaces import MemoryEntry
from vetinari.memory.memory_storage import _cosine_similarity, _unpack_embedding

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RRF_K = 60  # Reciprocal Rank Fusion constant
_CRAG_RELEVANCE_THRESHOLD = 0.3  # minimum cosine similarity to keep a result
_DEFAULT_LIMIT = 10


# ---------------------------------------------------------------------------
# Search helpers
# ---------------------------------------------------------------------------


def _fts_search(
    query: str,
    scope_filter: str | None,
    limit: int,
    agent_type_filter: str | None = None,
) -> list[tuple[str, float]]:
    """BM25 full-text search via FTS5 memory_fts table.

    Args:
        query: FTS5 query string (special chars are escaped).
        scope_filter: SQL scope expression (e.g. ``"global"`` or ``"project:abc"``).
            When provided, only memories with scope = scope_filter OR scope = 'global'
            are returned (scope inheritance).
        limit: Maximum result count.
        agent_type_filter: When provided, restrict results to memories whose
            ``agent`` column matches this value exactly.

    Returns:
        List of (memory_id, bm25_rank) pairs ordered by relevance.
    """
    conn = get_connection()
    # Build FTS5 query: each word becomes a separate OR term for broad matching.
    # Escape FTS5 special characters first, then join words with spaces (implicit AND).
    checked_query = require_nonempty(query, field_name="query")
    words = [w for w in checked_query.split() if w]
    if not words:
        return []
    # Use individual word matching (AND semantics): "word1 word2" in FTS5 = AND
    safe_words = " ".join('"' + w.replace('"', '""') + '"' if len(w) > 1 else w for w in words)
    try:
        has_scope = bool(scope_filter and scope_filter != "global")
        has_agent = bool(agent_type_filter)
        if has_scope and has_agent:
            rows = conn.execute(
                """SELECT m.id, bm25(memory_fts) AS rank
                   FROM memory_fts f
                   JOIN memories m ON m.id = f.id
                   WHERE memory_fts MATCH ? AND m.forgotten = 0
                     AND m.scope IN (?, 'global')
                     AND m.agent = ?
                   ORDER BY rank LIMIT ?""",
                (safe_words, scope_filter, agent_type_filter, limit),
            ).fetchall()
        elif has_scope:
            rows = conn.execute(
                """SELECT m.id, bm25(memory_fts) AS rank
                   FROM memory_fts f
                   JOIN memories m ON m.id = f.id
                   WHERE memory_fts MATCH ? AND m.forgotten = 0
                     AND m.scope IN (?, 'global')
                   ORDER BY rank LIMIT ?""",
                (safe_words, scope_filter, limit),
            ).fetchall()
        elif has_agent:
            rows = conn.execute(
                """SELECT m.id, bm25(memory_fts) AS rank
                   FROM memory_fts f
                   JOIN memories m ON m.id = f.id
                   WHERE memory_fts MATCH ? AND m.forgotten = 0
                     AND m.agent = ?
                   ORDER BY rank LIMIT ?""",
                (safe_words, agent_type_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT m.id, bm25(memory_fts) AS rank
                   FROM memory_fts f
                   JOIN memories m ON m.id = f.id
                   WHERE memory_fts MATCH ? AND m.forgotten = 0
                   ORDER BY rank LIMIT ?""",
                (safe_words, limit),
            ).fetchall()
        return [(row[0], float(row[1] or 0.0)) for row in rows]
    except Exception:
        account_evidence_drop(
            logger=logger,
            evidence_ref="fts_search",
            reason="memory_search_failure",
        )
        raise


def _semantic_search(
    query: str,
    scope_filter: str | None,
    limit: int,
    agent_type_filter: str | None = None,
) -> list[tuple[str, float]]:
    """Cosine similarity search over stored embeddings.

    Args:
        query: Natural language query to embed.
        scope_filter: Optional scope for inheritance filtering.
        limit: Maximum result count.
        agent_type_filter: When provided, restrict search to a specific agent type.

    Returns:
        List of (memory_id, similarity) pairs ordered by decreasing similarity.
    """
    from vetinari.embeddings import get_embedder

    conn = get_connection()
    try:
        q_vec = get_embedder().embed(require_nonempty(query, field_name="query"))
    except Exception:
        account_evidence_drop(
            logger=logger,
            evidence_ref="semantic_search_embed",
            reason="memory_search_failure",
        )
        raise

    if scope_filter and scope_filter != "global":
        if agent_type_filter:
            rows = conn.execute(
                """SELECT e.memory_id, e.embedding_blob
                   FROM embeddings e
                   JOIN memories m ON m.id = e.memory_id
                   WHERE m.forgotten = 0 AND m.scope IN (?, 'global') AND m.agent = ?""",
                (scope_filter, agent_type_filter),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT e.memory_id, e.embedding_blob
                   FROM embeddings e
                   JOIN memories m ON m.id = e.memory_id
                   WHERE m.forgotten = 0 AND m.scope IN (?, 'global')""",
                (scope_filter,),
            ).fetchall()
    elif agent_type_filter:
        rows = conn.execute(
            """SELECT e.memory_id, e.embedding_blob
               FROM embeddings e
               JOIN memories m ON m.id = e.memory_id
               WHERE m.forgotten = 0 AND m.agent = ?""",
            (agent_type_filter,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT e.memory_id, e.embedding_blob
               FROM embeddings e
               JOIN memories m ON m.id = e.memory_id
               WHERE m.forgotten = 0"""
        ).fetchall()

    scored: list[tuple[str, float]] = []
    for row in rows:
        try:
            stored_vec = _unpack_embedding(row[1])
            sim = _cosine_similarity(q_vec, stored_vec)
            scored.append((row[0], sim))
        except Exception:
            account_evidence_drop(
                logger=logger,
                evidence_ref=f"memory_embedding:{row[0]}",
                reason="memory_search_failure",
            )
            raise
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:limit]


def _reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[str, float]]],
    k: int = _RRF_K,
) -> list[tuple[str, float]]:
    """Merge multiple ranked lists via Reciprocal Rank Fusion.

    RRF score for item i in list L: sum over L of 1 / (k + rank(i in L))

    Args:
        ranked_lists: Each inner list is a ranked result list of (id, score).
        k: Smoothing constant (default 60, empirically robust).

    Returns:
        Merged list of (id, rrf_score) sorted by decreasing score.
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (item_id, _) in enumerate(ranked, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def _fetch_entries(ids: list[str]) -> list[MemoryEntry]:
    """Bulk-fetch memory entries by ID, preserving order.

    Args:
        ids: List of memory entry IDs to retrieve.

    Returns:
        List of :class:`~vetinari.memory.interfaces.MemoryEntry` in the
        same order as *ids*, skipping any that are not found.
    """
    if not ids:
        return []
    conn = get_connection()
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM memories WHERE id IN ({placeholders}) AND forgotten = 0",
        ids,
    ).fetchall()
    by_id: dict[str, Any] = {row["id"]: row for row in rows}
    from vetinari.memory.memory_storage import MemoryStorage

    storage = MemoryStorage()
    return [storage._row_to_entry(by_id[i]) for i in ids if i in by_id]


# ---------------------------------------------------------------------------
# Public search API
# ---------------------------------------------------------------------------


class MemorySearch:
    """Provides scope-aware hybrid search and CRAG post-filtering.

    All methods delegate to the unified SQLite database via
    ``get_connection()`` and share the ``get_embedder()`` singleton.
    """

    def search(
        self,
        query: str,
        *,
        scope: str = "global",
        mode: str = "hybrid",
        agent_type: str | None = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> list[MemoryEntry]:
        """Search memories, merging BM25 and semantic results via RRF.

        Args:
            query: Natural language query string.
            scope: Scope filter for inheritance (e.g. ``"task:abc"``).
                Scope inheritance means a task-scope query also returns
                global memories.
            mode: Search mode — ``"hybrid"`` (default), ``"semantic"``,
                or ``"fts"``.
            agent_type: Optional agent-type filter restricting results.
            limit: Maximum number of results to return.

        Returns:
            List of matching :class:`~vetinari.memory.interfaces.MemoryEntry`
            objects ordered by relevance.
        """
        if mode == "fts":
            results = _fts_search(query, scope, limit, agent_type)
            ids = [r[0] for r in results]
        elif mode == "semantic":
            results = _semantic_search(query, scope, limit, agent_type)
            ids = [r[0] for r in results]
        else:
            # Hybrid: parallel BM25 + semantic, merged via RRF
            fts_results = _fts_search(query, scope, limit * 2, agent_type)
            sem_results = _semantic_search(query, scope, limit * 2, agent_type)
            merged = _reciprocal_rank_fusion([fts_results, sem_results])
            ids = [item_id for item_id, _ in merged[:limit]]

        return _fetch_entries(ids)

    def search_with_relevance_check(
        self,
        query: str,
        *,
        scope: str = "global",
        agent_type: str | None = None,
        limit: int = _DEFAULT_LIMIT,
        relevance_threshold: float = _CRAG_RELEVANCE_THRESHOLD,
    ) -> list[MemoryEntry]:
        """CRAG: search then filter low-relevance results, reformulating if needed.

        Corrective Retrieval Augmented Generation (CRAG) post-filters
        results by cosine similarity to the query embedding.  When no
        results pass the threshold, the query is reformulated by stripping
        stop words and retried once.

        Args:
            query: Initial search query.
            scope: Scope filter for inheritance.
            agent_type: Optional agent-type filter.
            limit: Maximum number of results to return.
            relevance_threshold: Minimum cosine similarity to keep a result.

        Returns:
            List of sufficiently relevant
            :class:`~vetinari.memory.interfaces.MemoryEntry` objects.

        Raises:
            Exception: Re-raises embedding failures after evidence-drop
                accounting.
        """
        from vetinari.embeddings import get_embedder

        entries = self.search(query, scope=scope, mode="hybrid", agent_type=agent_type, limit=limit * 2)
        if not entries:
            return []

        # Score each result against the query embedding
        try:
            q_vec = get_embedder().embed(require_nonempty(query, field_name="query"))
        except Exception:
            account_evidence_drop(
                logger=logger,
                evidence_ref="relevance_check_embed",
                reason="memory_search_failure",
            )
            raise

        conn = get_connection()
        filtered: list[MemoryEntry] = []
        for entry in entries:
            row = conn.execute("SELECT embedding_blob FROM embeddings WHERE memory_id = ?", (entry.id,)).fetchone()
            if row is None:
                logger.debug("CRAG: excluding unembedded memory %s from relevance-filtered results", entry.id)
                continue
            try:
                sim = _cosine_similarity(q_vec, _unpack_embedding(row[0]))
                if sim >= relevance_threshold:
                    filtered.append(entry)
            except Exception:
                account_evidence_drop(
                    logger=logger,
                    evidence_ref=f"crag_embedding:{entry.id}",
                    reason="memory_search_failure",
                )
                raise

        if not filtered:
            # Reformulate: strip common stop words and retry once
            stop_words = {"the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "for", "of"}
            reformulated = " ".join(w for w in query.split() if w.lower() not in stop_words)
            if reformulated and reformulated != query:
                logger.debug("CRAG: no relevant results for %r; reformulating to %r", query, reformulated)
                return self.search(reformulated, scope=scope, mode="hybrid", agent_type=agent_type, limit=limit)
            return []

        return filtered[:limit]
