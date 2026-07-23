"""Episode storage and recall helpers for UnifiedMemoryStore.

Contains all operations on the ``memory_episodes`` and ``episode_embeddings``
tables: row conversion, stats, failure-pattern extraction, eviction,
insertion, and similarity-based recall.

Not part of the public API — import only from ``vetinari.memory.unified``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from ._lifecycle_receipts import record_lifecycle_receipt
from .memory_embeddings import unpack_embedding

logger = logging.getLogger(__name__)


_SEARCH_TERM_MIN_LEN = 3


def _redact_episode_value(value: Any) -> Any:
    """Apply memory-boundary PII and secret redaction to episode payloads."""
    from vetinari.safety.guardrails import redact_pii_payload
    from vetinari.security import get_secret_scanner

    redacted = redact_pii_payload(value)
    scanner = get_secret_scanner()
    if isinstance(redacted, str):
        return scanner.sanitize(redacted)
    if isinstance(redacted, dict):
        return scanner.sanitize_dict(redacted)
    return redacted


# ---------------------------------------------------------------------------
# Row conversion
# ---------------------------------------------------------------------------


def row_to_episode_dict(row: Any) -> dict[str, Any]:
    """Convert a memory_episodes row to a plain dict for Episode construction.

    Args:
        row: sqlite3.Row from the memory_episodes table.

    Returns:
        Dictionary with all Episode fields populated.
    """
    metadata: dict[str, Any] = {}
    if row["metadata_json"]:
        try:
            decoded = json.loads(row["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            decoded = {}
        if isinstance(decoded, dict):
            metadata = decoded
    return {
        "episode_id": row["episode_id"],
        "timestamp": row["timestamp"],
        "task_summary": row["task_summary"],
        "agent_type": row["agent_type"],
        "task_type": row["task_type"],
        "output_summary": row["output_summary"],
        "quality_score": float(row["quality_score"]),
        "success": bool(row["success"]),
        "model_id": row["model_id"] or "",
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Stats and pattern helpers
# ---------------------------------------------------------------------------


def get_episode_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """Compute episode statistics from the memory_episodes table.

    Args:
        conn: Active SQLite connection.

    Returns:
        Dictionary with ``total_episodes``, ``successful``, and
        ``avg_quality_score`` keys.
    """
    total = conn.execute("SELECT COUNT(*) as total FROM memory_episodes").fetchone()["total"]
    successful = conn.execute("SELECT COUNT(*) as cnt FROM memory_episodes WHERE success = 1").fetchone()["cnt"]
    avg_score = conn.execute("SELECT AVG(quality_score) as avg FROM memory_episodes").fetchone()["avg"] or 0.0
    return {
        "total_episodes": total,
        "successful": successful,
        "avg_quality_score": round(avg_score, 3),
    }


def get_failure_patterns(conn: sqlite3.Connection, agent_type: str, task_type: str) -> list[str]:
    """Return output summaries from failed episodes for error pattern analysis.

    Args:
        conn: Active SQLite connection.
        agent_type: Agent type filter.
        task_type: Task type filter.

    Returns:
        List of output summary strings from the 10 most recent failed episodes.
    """
    rows = conn.execute(
        """SELECT output_summary FROM memory_episodes
           WHERE agent_type = ? AND task_type = ? AND success = 0
           ORDER BY created_at DESC LIMIT 10""",
        (agent_type, task_type),
    ).fetchall()
    return [row["output_summary"] for row in rows]


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


def evict_old_episodes(conn: sqlite3.Connection, max_entries: int) -> None:
    """Remove the lowest-importance episodes when the table exceeds the limit.

    Evicts 10% of ``max_entries`` when the count exceeds the threshold.

    Args:
        conn: Active SQLite connection.
        max_entries: Maximum allowed episode count before eviction.
    """
    row = conn.execute("SELECT COUNT(*) as cnt FROM memory_episodes").fetchone()
    if not row or row["cnt"] <= max_entries:
        return
    evict_count = max_entries // 10
    try:
        candidates = conn.execute(
            """
            SELECT episode_id, timestamp, agent_type, task_type, quality_score,
                   success, model_id, importance, task_summary, output_summary
            FROM memory_episodes
            ORDER BY importance ASC
            LIMIT ?
            """,
            (evict_count,),
        ).fetchall()
        record_lifecycle_receipt(
            conn,
            store="memory_episodes",
            action="capacity_evict_low_importance",
            rows=candidates,
            id_field="episode_id",
            hash_fields=("task_summary", "output_summary"),
            metadata_fields=(
                "timestamp",
                "agent_type",
                "task_type",
                "quality_score",
                "success",
                "model_id",
                "importance",
            ),
        )
        conn.execute(
            "DELETE FROM memory_episodes WHERE episode_id IN "
            "(SELECT episode_id FROM memory_episodes ORDER BY importance ASC LIMIT ?)",
            (evict_count,),
        )
        conn.commit()
        logger.debug("Evicted %d low-importance episodes", evict_count)
    except sqlite3.Error as exc:
        logger.warning("Episode eviction failed: %s", exc)


# ---------------------------------------------------------------------------
# Insertion
# ---------------------------------------------------------------------------


def insert_episode(
    conn: sqlite3.Connection,
    *,
    task_summary: str,
    agent_type: str,
    task_type: str,
    output_summary: str,
    quality_score: float,
    success: bool,
    model_id: str,
    importance: float,
    metadata: dict[str, Any],
) -> str:
    """Insert a new episode row and return its generated ID.

    Args:
        conn: Active SQLite connection.
        task_summary: Human-readable task description (max 300 chars).
        agent_type: Agent type that executed the task.
        task_type: Task category (e.g. ``"coding"``).
        output_summary: Brief summary of the output (max 500 chars).
        quality_score: Quality score in [0.0, 1.0].
        success: Whether the task completed without errors.
        model_id: Identifier of the model used.
        importance: Pre-computed importance score.
        metadata: Additional key-value metadata.

    Returns:
        The generated ``episode_id``.
    """
    episode_id = f"ep_{uuid.uuid4().hex[:8]}"
    ts = datetime.now(timezone.utc).isoformat()
    task_summary = str(_redact_episode_value(task_summary))
    output_summary = str(_redact_episode_value(output_summary))
    metadata = _redact_episode_value(metadata)
    if not isinstance(metadata, dict):
        metadata = {}

    conn.execute(
        """INSERT INTO memory_episodes
           (episode_id, timestamp, task_summary, agent_type, task_type,
            output_summary, quality_score, success, model_id, importance,
            metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            episode_id,
            ts,
            task_summary,
            agent_type,
            task_type,
            output_summary,
            quality_score,
            int(success),
            model_id,
            importance,
            json.dumps(metadata),
        ),
    )
    conn.commit()
    return episode_id


def record_episode_full(
    conn: sqlite3.Connection,
    *,
    task_description: str,
    agent_type: str,
    task_type: str,
    output_summary: str,
    quality_score: float,
    success: bool,
    model_id: str,
    metadata: dict[str, Any],
    max_entries: int,
    api_url: str,
    model: str,
) -> str:
    """Record an episode, store its embedding, and evict if over limit.

    Truncates ``task_description`` to 300 chars and ``output_summary`` to
    500 chars.  Computes importance from ``quality_score`` and ``success``.
    Stores an episode embedding for similarity search when the embedding
    endpoint is available.

    Args:
        conn: Active SQLite connection.
        task_description: Human-readable task description.
        agent_type: Agent type that executed the task.
        task_type: Task category (e.g. ``"coding"``).
        output_summary: Brief output summary.
        quality_score: Quality score in [0.0, 1.0].
        success: Whether the task completed without errors.
        model_id: Identifier of the model used.
        metadata: Additional key-value metadata.
        max_entries: Episode table size limit; triggers eviction when exceeded.
        api_url: Embedding endpoint base URL.
        model: Embedding model identifier.

    Returns:
        The generated ``episode_id``.
    """
    from vetinari.inference.embedder import _validate_pinned_embedding_model

    from .memory_embeddings import embed_via_local_inference, pack_embedding

    _validate_pinned_embedding_model(model)
    task_summary = str(_redact_episode_value(task_description))[:300]
    out_summary = str(_redact_episode_value(output_summary))[:500]
    metadata = _redact_episode_value(metadata)
    if not isinstance(metadata, dict):
        metadata = {}
    importance = round(quality_score * (1.0 if success else 0.5), 3)

    episode_id = insert_episode(
        conn,
        task_summary=task_summary,
        agent_type=agent_type,
        task_type=task_type,
        output_summary=out_summary,
        quality_score=quality_score,
        success=success,
        model_id=model_id,
        importance=importance,
        metadata=metadata,
    )

    embed_text = f"{task_type}: {task_summary}"
    vec = embed_via_local_inference(embed_text, api_url, model)
    if vec is not None:
        blob = pack_embedding(vec)
        conn.execute(
            "INSERT OR REPLACE INTO episode_embeddings (episode_id, embedding_blob, model, dimensions) VALUES (?, ?, ?, ?)",
            (episode_id, blob, model, len(vec)),
        )
        conn.commit()

    evict_old_episodes(conn, max_entries)
    return episode_id


# ---------------------------------------------------------------------------
# Recall
# ---------------------------------------------------------------------------


def recall_episodes_from_db(
    conn: sqlite3.Connection,
    query_vec: list[float] | None,
    query_text: str,
    k: int,
    min_score: float,
    task_type: str | None,
    successful_only: bool,
    row_to_episode_fn: Any,
    embedding_model: str = "",
) -> list[Any]:
    """Retrieve the k most relevant past episodes using embedding or keyword search.

    When ``query_vec`` is provided, ranks episodes by cosine similarity.
    Falls back to keyword matching on ``task_summary`` otherwise.

    Args:
        conn: Active SQLite connection.
        query_vec: Pre-computed query embedding, or None for keyword fallback.
        query_text: Raw query string used for keyword fallback.
        k: Maximum episodes to return.
        min_score: Minimum cosine-similarity threshold when semantic recall is available.
        task_type: Optional task type filter.
        successful_only: When True, only return successful episodes.
        row_to_episode_fn: Callable that converts a sqlite3.Row to an Episode.
        embedding_model: Embedding model identifier expected for compatible stored vectors.

    Returns:
        List of Episode objects ordered by relevance.
    """
    cursor = conn.cursor()
    scored: list[tuple[str, float]] = []
    if query_vec is not None:
        embedding_dimensions = len(query_vec)
        top_ids, scored = _semantic_recall_ids(
            cursor,
            query_vec,
            min_score,
            k,
            task_type,
            successful_only,
            embedding_model,
            embedding_dimensions,
        )
    else:
        top_ids = _keyword_recall_ids(cursor, query_text, task_type, successful_only, k)

    if not top_ids:
        return []

    episodes = _load_recalled_episodes(
        cursor,
        top_ids,
        task_type=task_type if query_vec is not None else None,
        successful_only=successful_only if query_vec is not None else False,
        row_to_episode_fn=row_to_episode_fn,
    )
    if query_vec is not None:
        sim_map = dict(scored[: k * 3])
        episodes.sort(key=lambda ep: sim_map.get(ep.episode_id, 0.0), reverse=True)

    return episodes[:k]


def _semantic_recall_ids(
    cursor: sqlite3.Cursor,
    query_vec: list[float],
    min_score: float,
    k: int,
    task_type: str | None,
    successful_only: bool,
    embedding_model: str,
    embedding_dimensions: int,
) -> tuple[list[str], list[tuple[str, float]]]:
    from vetinari.utils.math_helpers import cosine_similarity

    embedding_columns = {str(row["name"]) for row in cursor.execute("PRAGMA table_info(episode_embeddings)").fetchall()}
    clauses = []
    params: list[Any] = []
    if task_type:
        clauses.append("m.task_type = ?")
        params.append(task_type)
    if successful_only:
        clauses.append("m.success = 1")
    if "model" in embedding_columns:
        clauses.append("(e.model = ? OR e.model = '')")
        params.append(embedding_model)
    if "dimensions" in embedding_columns:
        clauses.append("(e.dimensions = ? OR e.dimensions = 0)")
        params.append(embedding_dimensions)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cursor.execute(
        "SELECT e.episode_id, e.embedding_blob "
        "FROM episode_embeddings e "
        "JOIN memory_episodes m ON m.episode_id = e.episode_id "
        f"{where}",
        params,
    )
    scored = []
    for row in cursor.fetchall():
        ep_vec = unpack_embedding(row["embedding_blob"])
        sim = cosine_similarity(query_vec, ep_vec)
        scored.append((row["episode_id"], sim))
    if min_score > 0:
        scored = [(episode_id, sim) for episode_id, sim in scored if sim >= min_score]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [eid for eid, _ in scored[: k * 3]], scored


def _keyword_recall_ids(
    cursor: sqlite3.Cursor,
    query_text: str,
    task_type: str | None,
    successful_only: bool,
    k: int,
) -> list[str]:
    sql = "SELECT episode_id FROM memory_episodes"
    params: list[Any] = []
    clauses: list[str] = []
    terms = _search_terms(query_text)
    if terms:
        clauses.append("(" + " OR ".join("task_summary LIKE ?" for _ in terms) + ")")
        params.extend(f"%{term}%" for term in terms)
    else:
        clauses.append("task_summary LIKE ?")
        params.append(f"%{query_text[:100]}%")
    if task_type:
        clauses.append("task_type = ?")
        params.append(task_type)
    if successful_only:
        clauses.append("success = 1")
    sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY quality_score DESC LIMIT ?"
    params.append(k * 3)
    cursor.execute(sql, params)
    return [row["episode_id"] for row in cursor.fetchall()]


def _load_recalled_episodes(
    cursor: sqlite3.Cursor,
    top_ids: list[str],
    *,
    task_type: str | None,
    successful_only: bool,
    row_to_episode_fn: Any,
) -> list[Any]:
    placeholders = ",".join("?" for _ in top_ids)
    sql = f"SELECT * FROM memory_episodes WHERE episode_id IN ({placeholders})"
    params: list[Any] = list(top_ids)
    if task_type:
        sql += " AND task_type = ?"
        params.append(task_type)
    if successful_only:
        sql += " AND success = 1"
    cursor.execute(sql, params)
    return [row_to_episode_fn(row) for row in cursor.fetchall()]


def _search_terms(query_text: str) -> list[str]:
    terms: list[str] = []
    current: list[str] = []
    for char in query_text.lower():
        if char.isalnum():
            current.append(char)
            continue
        if current:
            term = "".join(current)
            if len(term) >= _SEARCH_TERM_MIN_LEN:
                terms.append(term)
            current = []
    if current:
        term = "".join(current)
        if len(term) >= _SEARCH_TERM_MIN_LEN:
            terms.append(term)
    return list(dict.fromkeys(terms))
