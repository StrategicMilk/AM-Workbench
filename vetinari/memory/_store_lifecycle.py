"""Memory store compaction, eviction, and stats helpers."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
from typing import Any

from ._lifecycle_receipts import record_lifecycle_receipt

logger = logging.getLogger(__name__)


def compact_memories(conn: sqlite3.Connection, max_age_days: int | None) -> int:
    """Remove forgotten entries and optionally prune old data.

    Args:
        conn: Active SQLite connection.
        max_age_days: Remove non-frequently-accessed entries older than this.

    Returns:
        Number of entries removed.
    """
    import time as _time

    deleted = 0
    forgotten_rows = conn.execute(
        """
        SELECT id, content_hash, timestamp, entry_type, agent, quality_score, importance
        FROM memories
        WHERE forgotten = 1
        """
    ).fetchall()
    record_lifecycle_receipt(
        conn,
        store="memories",
        action="compact_forgotten",
        rows=forgotten_rows,
        id_field="id",
        hash_fields=("content", "summary"),
        metadata_fields=("timestamp", "entry_type", "agent", "quality_score", "importance"),
    )
    forgotten_ids = [row["id"] for row in forgotten_rows]
    if forgotten_ids:
        placeholders = ",".join("?" for _ in forgotten_ids)
        conn.execute(f"DELETE FROM embeddings WHERE memory_id IN ({placeholders})", forgotten_ids)  # noqa: S608
        with contextlib.suppress(sqlite3.Error):
            conn.execute(f"DELETE FROM memory_vec WHERE memory_id IN ({placeholders})", forgotten_ids)  # noqa: S608
    conn.execute("DELETE FROM memories WHERE forgotten = 1")
    deleted += int(conn.execute("SELECT changes()").fetchone()[0])

    if max_age_days is not None:
        cutoff = int((_time.time() - max_age_days * 86400) * 1000)
        old_rows = conn.execute(
            """
            SELECT id, content_hash, timestamp, entry_type, agent, quality_score, importance
            FROM memories
            WHERE timestamp < ? AND access_count < 3
            """,
            (cutoff,),
        ).fetchall()
        record_lifecycle_receipt(
            conn,
            store="memories",
            action="compact_old",
            rows=old_rows,
            id_field="id",
            hash_fields=("content", "summary"),
            metadata_fields=("timestamp", "entry_type", "agent", "quality_score", "importance"),
        )
        conn.execute(
            "DELETE FROM memories WHERE timestamp < ? AND access_count < 3",
            (cutoff,),
        )
        deleted += int(conn.execute("SELECT changes()").fetchone()[0])

    conn.commit()
    logger.info("Compacted memory store: removed %d entries", deleted)
    return deleted


def evict_low_importance_memories(conn: sqlite3.Connection, max_entries: int) -> None:
    """Evict the least-important long-term memory entries when over capacity.

    Importance is computed as quality_score * (access_count + 1) * recency_decay.
    Removes entries until the count is ``max_entries`` minus a 5% buffer.

    Args:
        conn: Active SQLite connection.
        max_entries: Maximum allowed memory count.
    """
    total = conn.execute("SELECT COUNT(*) as cnt FROM memories WHERE forgotten = 0").fetchone()["cnt"]
    if total <= max_entries:
        return
    evict_count = total - max_entries + (max_entries // 20)  # 5% buffer
    # Ebbinghaus-based eviction: rank by retention strength (ADR-0071).
    # Uses SQL approximation of the Ebbinghaus formula for in-DB sorting.
    # importance * exp(-0.16 * (1 - importance*0.8) * days) * (1 + recall_count*0.2)
    try:
        candidates = conn.execute(
            """SELECT id, content_hash, timestamp, entry_type, agent, quality_score, importance
               FROM memories
               WHERE forgotten = 0
               ORDER BY (
                   importance *
                   EXP(-0.16 * (1.0 - importance * 0.8) *
                       MAX(0, (julianday('now') - julianday(created_at)))) *
                   (1.0 + COALESCE(recall_count, 0) * 0.2)
               ) ASC LIMIT ?""",
            (evict_count,),
        ).fetchall()
        record_lifecycle_receipt(
            conn,
            store="memories",
            action="capacity_evict_low_importance",
            rows=candidates,
            id_field="id",
            hash_fields=("content", "summary"),
            metadata_fields=("timestamp", "entry_type", "agent", "quality_score", "importance"),
        )
        conn.execute(
            """DELETE FROM memories WHERE id IN (
                 SELECT id FROM memories
                 WHERE forgotten = 0
                 ORDER BY (
                     importance *
                     EXP(-0.16 * (1.0 - importance * 0.8) *
                         MAX(0, (julianday('now') - julianday(created_at)))) *
                     (1.0 + COALESCE(recall_count, 0) * 0.2)
                 ) ASC LIMIT ?
               )""",
            (evict_count,),
        )
        conn.execute("DELETE FROM embeddings WHERE memory_id NOT IN (SELECT id FROM memories WHERE forgotten = 0)")
        with contextlib.suppress(sqlite3.Error):
            conn.execute("DELETE FROM memory_vec WHERE memory_id NOT IN (SELECT id FROM memories WHERE forgotten = 0)")
        conn.commit()
        logger.info("Evicted low-importance memories (count=%d)", evict_count)
    except sqlite3.Error as exc:
        logger.warning("Memory eviction failed: %s", exc)


# ---------------------------------------------------------------------------
# Stats and single-entry fetch
# ---------------------------------------------------------------------------


def get_memory_stats(conn: sqlite3.Connection, db_path_fn: Any) -> dict[str, Any]:
    """Compute aggregate statistics from the memories table.

    Args:
        conn: Active SQLite connection.
        db_path_fn: Callable that returns the database Path (for file size).

    Returns:
        Dictionary with total_entries, file_size_bytes, oldest/newest timestamps,
        and per-agent/per-type counts.
    """
    total = conn.execute("SELECT COUNT(*) as total FROM memories WHERE forgotten = 0").fetchone()["total"]
    row = conn.execute(
        "SELECT MIN(timestamp) as oldest, MAX(timestamp) as newest FROM memories WHERE forgotten = 0"
    ).fetchone()
    oldest = row["oldest"] or 0
    newest = row["newest"] or 0
    by_agent = {
        r["agent"]: r["cnt"]
        for r in conn.execute(
            "SELECT agent, COUNT(*) as cnt FROM memories WHERE forgotten = 0 GROUP BY agent"
        ).fetchall()
    }
    by_type = {
        r["entry_type"]: r["cnt"]
        for r in conn.execute(
            "SELECT entry_type, COUNT(*) as cnt FROM memories WHERE forgotten = 0 GROUP BY entry_type"
        ).fetchall()
    }
    try:
        file_size = db_path_fn().stat().st_size
    except OSError:
        file_size = 0
    return {
        "total_entries": total,
        "file_size_bytes": file_size,
        "oldest_entry": oldest,
        "newest_entry": newest,
        "entries_by_agent": by_agent,
        "entries_by_type": by_type,
    }
