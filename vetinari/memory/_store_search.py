"""Search helper functions for UnifiedMemoryStore.

Contains the four search backends (KNN, cosine, FTS5, LIKE) plus timeline
retrieval and semantic deduplication checks. All functions accept an explicit
SQLite connection so they work with both the shared thread-local connection
(production) and private connections (test isolation).

Not part of the public API — import only from ``vetinari.memory.unified``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from vetinari.utils.math_helpers import cosine_similarity

from .interfaces import MemoryEntry
from .memory_embeddings import unpack_embedding

logger = logging.getLogger(__name__)


def vec_knn_search(
    conn: sqlite3.Connection,
    query_vec: list[float],
    agent: str | None,
    entry_types: list[str] | None,
    limit: int,
    *,
    fallback_query: str,
    embedding_model: str,
    embedding_dimensions: int,
    fts_fallback: Any,  # callable(conn, query, agent, entry_types, limit) -> list[MemoryEntry]
    manual_fallback: Any,  # callable(conn, query_vec, agent, entry_types, limit) -> list[MemoryEntry]
) -> list[MemoryEntry]:
    """Search using sqlite-vec KNN — O(log n) when the extension is loaded.

    Falls back to manual cosine scan when the KNN query fails, and to FTS5
    when the KNN result set is empty.

    Args:
        conn: Active SQLite connection with sqlite-vec loaded.
        query_vec: Query embedding vector.
        agent: Optional agent-name filter.
        entry_types: Optional entry-type filter list.
        limit: Maximum results.
        fallback_query: Original text query used for lexical fallback.
        embedding_model: Embedding model identifier that stored rows must match.
        embedding_dimensions: Embedding vector dimensionality that stored rows must match.
        fts_fallback: Callable used when the KNN result set is empty.
        manual_fallback: Callable used when the KNN query itself fails.

    Returns:
        Ranked list of :class:`~vetinari.memory.interfaces.MemoryEntry`.
    """
    from .memory_embeddings import pack_embedding

    cursor = conn.cursor()
    query_blob = pack_embedding(query_vec)
    fetch_limit = limit * 3  # over-fetch to account for post-filtering

    try:
        cursor.execute(
            "SELECT memory_id, distance FROM memory_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (query_blob, fetch_limit),
        )
        knn_results = cursor.fetchall()
    except sqlite3.Error as exc:
        logger.warning("sqlite-vec KNN search failed: %s — falling back to manual cosine scan", exc)
        return manual_fallback(conn, query_vec, agent, entry_types, limit)

    if not knn_results:
        return fts_fallback(conn, fallback_query, agent, entry_types, limit)

    candidate_ids = [row["memory_id"] for row in knn_results]
    results = _hydrate_knn_entries(
        conn,
        candidate_ids,
        agent,
        entry_types,
        embedding_model,
        embedding_dimensions,
    )
    if not results:
        return fts_fallback(conn, fallback_query, agent, entry_types, limit)
    return results[:limit]


def _hydrate_knn_entries(
    conn: sqlite3.Connection,
    candidate_ids: list[str],
    agent: str | None,
    entry_types: list[str] | None,
    embedding_model: str,
    embedding_dimensions: int,
) -> list[MemoryEntry]:
    from ._store_ops import row_to_entry

    cursor = conn.cursor()
    placeholders = ",".join("?" for _ in candidate_ids)
    conditions = [
        f"m.id IN ({placeholders})",
        "m.forgotten = 0",
        "e.model = ?",
        "LENGTH(e.embedding_blob) = ?",
    ]
    params: list[Any] = [*candidate_ids, embedding_model, embedding_dimensions * 4]

    if agent:
        conditions.append("m.agent = ?")
        params.append(agent)
    if entry_types:
        type_ph = ",".join("?" for _ in entry_types)
        conditions.append(f"m.entry_type IN ({type_ph})")
        params.extend(entry_types)

    where = " AND ".join(conditions)
    cursor.execute(
        f"SELECT m.* FROM memories m JOIN embeddings e ON e.memory_id = m.id WHERE {where}",
        params,
    )
    entries_by_id = {row["id"]: row_to_entry(row) for row in cursor.fetchall()}
    return [entries_by_id[mid] for mid in candidate_ids if mid in entries_by_id]


def manual_cosine_search(
    conn: sqlite3.Connection,
    query_vec: list[float],
    agent: str | None,
    entry_types: list[str] | None,
    limit: int,
    *,
    fallback_query: str,
    embedding_model: str,
    embedding_dimensions: int,
    fts_fallback: Any,
) -> list[MemoryEntry]:
    """O(n) cosine similarity scan over stored embeddings.

    Used when sqlite-vec KNN is unavailable.  Falls back to FTS5 when
    no embeddings are present.

    Args:
        conn: Active SQLite connection.
        query_vec: Query embedding vector.
        agent: Optional agent-name filter.
        entry_types: Optional entry-type filter list.
        limit: Maximum results.
        fallback_query: Original text query used for lexical fallback.
        embedding_model: Embedding model identifier that stored rows must match.
        embedding_dimensions: Embedding vector dimensionality that stored rows must match.
        fts_fallback: Callable used when the embedding set is empty.

    Returns:
        Ranked list of :class:`~vetinari.memory.interfaces.MemoryEntry`.
    """
    from ._store_ops import row_to_entry

    cursor = conn.cursor()
    conditions = ["m.forgotten = 0", "e.model = ?", "LENGTH(e.embedding_blob) = ?"]
    params: list[Any] = [embedding_model, embedding_dimensions * 4]

    if agent:
        conditions.append("m.agent = ?")
        params.append(agent)
    if entry_types:
        placeholders = ",".join("?" for _ in entry_types)
        conditions.append(f"m.entry_type IN ({placeholders})")
        params.extend(entry_types)

    where = " AND ".join(conditions)
    cursor.execute(
        f"SELECT e.memory_id, e.embedding_blob FROM embeddings e JOIN memories m ON e.memory_id = m.id WHERE {where}",
        params,
    )

    scored: list[tuple[str, float]] = []
    for row in cursor.fetchall():
        mem_vec = unpack_embedding(row["embedding_blob"])
        sim = cosine_similarity(query_vec, mem_vec)
        scored.append((row["memory_id"], sim))

    scored.sort(key=lambda x: x[1], reverse=True)
    top_ids = [mid for mid, _ in scored[:limit]]

    if not top_ids:
        return fts_fallback(conn, fallback_query, agent, entry_types, limit)

    placeholders = ",".join("?" for _ in top_ids)
    cursor.execute(f"SELECT * FROM memories WHERE id IN ({placeholders})", top_ids)
    entries_by_id = {row["id"]: row_to_entry(row) for row in cursor.fetchall()}

    return [entries_by_id[mid] for mid in top_ids if mid in entries_by_id]


def fts_search(
    conn: sqlite3.Connection,
    query: str,
    agent: str | None,
    entry_types: list[str] | None,
    limit: int,
    *,
    like_fallback: Any,
) -> list[MemoryEntry]:
    """FTS5 full-text search with BM25 ranking.

    Falls back to a LIKE scan when the FTS5 query syntax is invalid.

    Args:
        conn: Active SQLite connection.
        query: Free-text search query.
        agent: Optional agent-name filter.
        entry_types: Optional entry-type filter list.
        limit: Maximum results.
        like_fallback: Callable used when FTS5 raises OperationalError.

    Returns:
        Ranked list of :class:`~vetinari.memory.interfaces.MemoryEntry`.
    """
    from ._store_ops import row_to_entry

    if not query or not query.strip():
        return []

    terms = re.findall(r"\w+", query, flags=re.UNICODE)[:10]
    if not terms:
        return []

    cursor = conn.cursor()
    conditions = ["m.forgotten = 0"]
    params: list[Any] = []

    fts_query = " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)
    conditions.append("memory_fts MATCH ?")
    params.append(fts_query)

    if agent:
        conditions.append("m.agent = ?")
        params.append(agent)
    if entry_types:
        placeholders = ",".join("?" for _ in entry_types)
        conditions.append(f"m.entry_type IN ({placeholders})")
        params.extend(entry_types)

    where = " AND ".join(conditions)
    sql = (
        "SELECT m.* FROM memory_fts f "
        "JOIN memories m ON f.rowid = m.rowid "
        f"WHERE {where} "
        "ORDER BY bm25(memory_fts), m.timestamp DESC LIMIT ?"
    )
    params.append(limit)

    try:
        cursor.execute(sql, params)
        return [row_to_entry(row) for row in cursor.fetchall()]
    except sqlite3.OperationalError as exc:
        logger.warning("FTS5 search failed, falling back to LIKE: %s", exc)
        return like_fallback(conn, query, agent, entry_types, limit)


def like_search(
    conn: sqlite3.Connection,
    query: str,
    agent: str | None,
    entry_types: list[str] | None,
    limit: int,
) -> list[MemoryEntry]:
    """LIKE-based substring search — last-resort fallback for FTS5 failures.

    Args:
        conn: Active SQLite connection.
        query: Substring to search for.
        agent: Optional agent-name filter.
        entry_types: Optional entry-type filter list.
        limit: Maximum results.

    Returns:
        List of matching :class:`~vetinari.memory.interfaces.MemoryEntry`.
    """
    from ._store_ops import row_to_entry

    cursor = conn.cursor()
    conditions = ["forgotten = 0", "content LIKE ?"]
    params: list[Any] = [f"%{query}%"]

    if agent:
        conditions.append("agent = ?")
        params.append(agent)
    if entry_types:
        placeholders = ",".join("?" for _ in entry_types)
        conditions.append(f"entry_type IN ({placeholders})")
        params.extend(entry_types)

    where = " AND ".join(conditions)
    sql = f"SELECT * FROM memories WHERE {where} ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    cursor.execute(sql, params)
    return [row_to_entry(row) for row in cursor.fetchall()]


def build_timeline(
    conn: sqlite3.Connection,
    agent: str | None,
    start_time: int | None,
    end_time: int | None,
    limit: int,
) -> list[MemoryEntry]:
    """Fetch memories ordered by timestamp descending with optional filters.

    Args:
        conn: Active SQLite connection.
        agent: Optional agent-name filter.
        start_time: Optional start timestamp (milliseconds, inclusive).
        end_time: Optional end timestamp (milliseconds, inclusive).
        limit: Maximum entries to return.

    Returns:
        List of :class:`~vetinari.memory.interfaces.MemoryEntry` ordered by
        timestamp descending.
    """
    from ._store_ops import row_to_entry

    conditions = ["forgotten = 0"]
    params: list[Any] = []
    if agent:
        conditions.append("agent = ?")
        params.append(agent)
    if start_time is not None:
        conditions.append("timestamp >= ?")
        params.append(start_time)
    if end_time is not None:
        conditions.append("timestamp <= ?")
        params.append(end_time)

    where = " AND ".join(conditions)
    sql = f"SELECT * FROM memories WHERE {where} ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    cursor = conn.cursor()
    cursor.execute(sql, params)
    return [row_to_entry(row) for row in cursor.fetchall()]


def is_semantic_duplicate(
    conn: sqlite3.Connection,
    query_vec: list[float],
    dedup_threshold: float,
) -> bool:
    """Check whether a query vector is near-duplicate of any stored embedding.

    Scans up to 500 recent embeddings and returns True if any cosine
    similarity exceeds ``dedup_threshold``.

    Args:
        conn: Active SQLite connection.
        query_vec: Embedding of the candidate content.
        dedup_threshold: Cosine similarity threshold for declaring a duplicate.

    Returns:
        True if a near-duplicate exists in the embeddings table.
    """
    rows = conn.execute(
        "SELECT e.embedding_blob FROM embeddings e "
        "JOIN memories m ON e.memory_id = m.id "
        "WHERE m.forgotten = 0 "
        "ORDER BY m.timestamp DESC, m.created_at DESC "
        "LIMIT 500",
    ).fetchall()
    for row in rows:
        stored_vec = unpack_embedding(row["embedding_blob"])
        if cosine_similarity(query_vec, stored_vec) > dedup_threshold:
            return True
    return False
