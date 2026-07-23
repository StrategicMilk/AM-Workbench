"""Memory storage operations — write side of the unified memory system.

Extracted from ``unified.py`` (P0 split).  Provides write-path helpers:
content deduplication, provenance tracking, hash-chain tamper detection,
importance scoring, and SQLite write wrappers.

Decision: layered memory with scope inheritance and hash-chain tamper
detection (ADR-0077).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import struct
import threading
import time
from datetime import datetime, timezone
from typing import Any

from vetinari.database import get_connection
from vetinari.memory.interfaces import MemoryEntry, MemoryType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEMANTIC_DEDUP_THRESHOLD = 0.92  # cosine similarity above which we consider entries duplicates
_DEDUP_SCAN_LIMIT = 2000  # max embeddings to scan during dedup check


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pack_embedding(vec: list[float]) -> bytes:
    """Pack a float list into a compact binary blob."""
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_embedding(blob: bytes) -> list[float]:
    """Unpack a binary embedding blob into a float list."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _record_dedup_hit_best_effort(agent: str) -> None:
    """Record a deduplication hit in the telemetry system."""
    try:
        from vetinari.telemetry import get_telemetry_collector

        get_telemetry_collector().record_dedup_hit(agent)
    except Exception:
        logger.warning(
            "Telemetry record_dedup_hit failed for %s — skipping best-effort telemetry",
            agent,
            exc_info=True,
        )


def _record_dedup_miss_best_effort(agent: str) -> None:
    """Record a deduplication miss in the telemetry system."""
    try:
        from vetinari.telemetry import get_telemetry_collector

        get_telemetry_collector().record_dedup_miss(agent)
    except Exception:
        logger.warning(
            "Telemetry record_dedup_miss failed for %s — skipping best-effort telemetry",
            agent,
            exc_info=True,
        )


def _compute_chain_hash(entry_id: str, content: str, prev_hash: str) -> str:
    """Compute a tamper-detection chain hash linking this entry to its predecessor.

    Args:
        entry_id: The memory entry's unique ID.
        content: Entry content to include in the hash.
        prev_hash: Hash of the previous entry in the chain (empty string for first).

    Returns:
        SHA-256 hex digest of (entry_id + content + prev_hash).
    """
    payload = f"{entry_id}:{content}:{prev_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# -- Ebbinghaus decay constants (ADR-0071) --
DECAY_RATE = 0.16  # Base decay rate in the exponential
IMPORTANCE_FACTOR = 0.8  # How much importance slows decay (0 = no effect, 1 = full)
RECALL_BOOST = 0.2  # Strength increase per recall event
PRUNE_THRESHOLD = 0.1  # Below this strength, memory is eligible for pruning
MS_PER_DAY = 86_400_000  # Milliseconds in a day


def ebbinghaus_strength(
    importance: float,
    created_ts_ms: int,
    recall_count: int = 0,
    *,
    now_ms: int | None = None,
) -> float:
    """Compute memory retention strength using the Ebbinghaus forgetting curve.

    Higher importance slows decay; each recall boosts strength. Memories with
    strength below PRUNE_THRESHOLD are candidates for garbage collection.

    Formula: importance * exp(-DECAY_RATE * (1 - importance * IMPORTANCE_FACTOR) * days)
             * (1 + recall_count * RECALL_BOOST)

    Args:
        importance: Original importance score (0.0-1.0).
        created_ts_ms: Creation timestamp in milliseconds since epoch.
        recall_count: Number of times this memory has been actively retrieved.
        now_ms: Current time in ms (defaults to wall clock; injectable for tests).

    Returns:
        Retention strength, clamped to [0.0, 1.0].
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    days = max(0.0, (now_ms - created_ts_ms) / MS_PER_DAY)
    decay = math.exp(-DECAY_RATE * (1.0 - importance * IMPORTANCE_FACTOR) * days)
    recall_factor = 1.0 + recall_count * RECALL_BOOST
    return max(0.0, min(1.0, importance * decay * recall_factor))


# ---------------------------------------------------------------------------
# Telemetry helpers (best-effort — never block the write path)
# ---------------------------------------------------------------------------


def _record_dedup_hit(backend: str) -> None:
    """Record a deduplication hit in telemetry (best-effort, never raises).

    Args:
        backend: Memory backend identifier, passed through to TelemetryCollector.
    """
    try:
        from vetinari.telemetry import get_telemetry_collector

        get_telemetry_collector().record_dedup_hit(backend)
    except Exception:
        logger.warning("Telemetry dedup_hit recording failed for '%s' — collector unavailable or raised", backend)


def _record_dedup_miss(backend: str) -> None:
    """Record a deduplication miss in telemetry (best-effort, never raises).

    Args:
        backend: Memory backend identifier, passed through to TelemetryCollector.
    """
    try:
        from vetinari.telemetry import get_telemetry_collector

        get_telemetry_collector().record_dedup_miss(backend)
    except Exception:
        logger.warning("Telemetry dedup_miss recording failed for '%s' — collector unavailable or raised", backend)


def _record_sync_failure(backend: str) -> None:
    """Record a memory commit failure in telemetry (best-effort, never raises).

    Args:
        backend: Memory backend identifier, passed through to TelemetryCollector.
    """
    try:
        from vetinari.telemetry import get_telemetry_collector

        get_telemetry_collector().record_sync_failure(backend)
    except Exception:
        logger.warning("Telemetry sync_failure recording failed for '%s' — collector unavailable or raised", backend)


# ---------------------------------------------------------------------------
# Storage class
# ---------------------------------------------------------------------------

_storage_lock = threading.Lock()


def _exact_duplicate_id(conn: Any, content_hash: str, agent: str) -> str | None:
    """Return an exact duplicate memory id when content hash matches."""
    row = conn.execute("SELECT id FROM memories WHERE content_hash = ? AND forgotten = 0", (content_hash,)).fetchone()
    if not row:
        return None
    logger.debug("Memory dedup: exact match found (%s)", row[0])
    _record_dedup_hit(agent)
    return row[0]


def _semantic_duplicate_id(conn: Any, vec: list[float], agent: str) -> str | None:
    """Return a semantically duplicate memory id from recent embeddings."""
    rows = conn.execute(
        "SELECT memory_id, embedding_blob FROM embeddings ORDER BY rowid DESC LIMIT ?",
        (_DEDUP_SCAN_LIMIT,),
    ).fetchall()
    for emb_row in rows:
        sim = _cosine_similarity(vec, _unpack_embedding(emb_row[1]))
        if sim >= SEMANTIC_DEDUP_THRESHOLD:
            logger.debug(
                "Memory semantic dedup: similarity %.3f >= %.2f; skipping",
                sim,
                SEMANTIC_DEDUP_THRESHOLD,
            )
            _record_dedup_hit(agent)
            return emb_row[0]
    _record_dedup_miss(agent)
    return None


def _previous_chain_hash(conn: Any) -> str:
    """Return the latest persisted chain hash for tamper detection."""
    last_row = conn.execute("SELECT id, metadata_json FROM memories ORDER BY rowid DESC LIMIT 1").fetchone()
    if not last_row or not last_row[1]:
        return ""
    meta = json.loads(last_row[1]) if last_row[1] else {}
    return str(meta.get("chain_hash", ""))


def _memory_metadata_with_chain(entry: MemoryEntry, chain_hash: str) -> dict[str, Any]:
    """Return persisted metadata with chain and provenance fields."""
    meta = dict(entry.metadata or {})
    meta["chain_hash"] = chain_hash
    if entry.provenance:
        meta["provenance"] = entry.provenance
    return meta


def _insert_memory_entry(
    conn: Any,
    entry: MemoryEntry,
    *,
    ts_ms: int,
    now_str: str,
    content_hash: str,
    chain_hash: str,
    vec: list[float],
) -> None:
    """Insert or replace the memory row and matching embedding."""
    meta = _memory_metadata_with_chain(entry, chain_hash)
    conn.execute(
        """INSERT OR REPLACE INTO memories
           (id, agent, entry_type, content, summary, timestamp, provenance,
            content_hash, quality_score, importance, created_at, updated_at,
            metadata_json, scope)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry.id,
            entry.agent,
            entry.entry_type.value if hasattr(entry.entry_type, "value") else str(entry.entry_type),
            entry.content,
            entry.summary,
            ts_ms,
            entry.provenance,
            content_hash,
            0.5,
            entry.metadata.get("importance", 0.5) if entry.metadata else 0.5,
            now_str,
            now_str,
            json.dumps(meta),
            entry.scope,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO embeddings (memory_id, embedding_blob, model, created_at) VALUES (?, ?, ?, ?)",
        (entry.id, _pack_embedding(vec), "all-MiniLM-L6-v2", now_str),
    )


class MemoryStorage:
    """Handles write-side memory operations: store, forget, deduplicate, chain.

    Uses the unified SQLite database via ``get_connection()``.  Thread-safe
    through per-method locking.
    """

    def store(
        self,
        entry: MemoryEntry,
        *,
        check_duplicate: bool = True,
    ) -> str:
        """Persist a memory entry, skipping near-duplicates when requested.

        Computes a content hash for fast exact deduplication and optionally
        checks semantic similarity against recent embeddings.  Appends the
        entry to the tamper-detection hash chain.

        Args:
            entry: The memory entry to store.
            check_duplicate: When True, reject entries with cosine similarity
                ≥ SEMANTIC_DEDUP_THRESHOLD against any recent embedding.

        Returns:
            The ``entry.id`` that was stored, or the ID of the duplicate
            that was found (when deduplication skips the write).

        Raises:
            sqlite3.DatabaseError: If the underlying SQLite write fails.
        """
        conn = get_connection()
        now_str = datetime.now(timezone.utc).isoformat()
        ts_ms = entry.timestamp or int(time.time() * 1000)
        content_hash = hashlib.sha256(entry.content.encode("utf-8")).hexdigest()
        agent = entry.agent or "unknown"

        duplicate_id = _exact_duplicate_id(conn, content_hash, agent)
        if duplicate_id is not None:
            return duplicate_id

        from vetinari.embeddings import get_embedder

        vec = get_embedder().embed(entry.content)
        if check_duplicate:
            duplicate_id = _semantic_duplicate_id(conn, vec, agent)
            if duplicate_id is not None:
                return duplicate_id

        chain_hash = _compute_chain_hash(entry.id, entry.content, _previous_chain_hash(conn))
        with _storage_lock:
            _insert_memory_entry(
                conn,
                entry,
                ts_ms=ts_ms,
                now_str=now_str,
                content_hash=content_hash,
                chain_hash=chain_hash,
                vec=vec,
            )
            try:
                conn.commit()
            except Exception as _commit_exc:
                _record_sync_failure(agent)
                raise RuntimeError(
                    f"Memory commit failed for entry {entry.id} - storage may be inconsistent"
                ) from _commit_exc

        logger.debug("Stored memory %s (scope=%s, agent=%s)", entry.id, entry.scope, entry.agent)
        return entry.id

    def forget(self, entry_id: str) -> bool:
        """Soft-delete a memory entry by marking it forgotten.

        Args:
            entry_id: The ID of the entry to forget.

        Returns:
            True when the entry was found and marked forgotten.
        """
        conn = get_connection()
        cursor = conn.execute("UPDATE memories SET forgotten = 1 WHERE id = ?", (entry_id,))
        conn.commit()
        return cursor.rowcount > 0

    def update_content(self, entry_id: str, new_content: str) -> bool:
        """Replace the content of an existing memory entry.

        Also recomputes the content hash and clears the old embedding so
        the next search re-embeds the updated content.

        Args:
            entry_id: Target memory entry ID.
            new_content: Replacement content string.

        Returns:
            True when the entry was found and updated.
        """
        conn = get_connection()
        new_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
        now_str = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            "UPDATE memories SET content = ?, content_hash = ?, updated_at = ? WHERE id = ?",
            (new_content, new_hash, now_str, entry_id),
        )
        if cursor.rowcount:
            # Remove stale embedding so it gets re-computed on next search
            conn.execute("DELETE FROM embeddings WHERE memory_id = ?", (entry_id,))
        conn.commit()
        return cursor.rowcount > 0

    def get_entry(self, entry_id: str) -> MemoryEntry | None:
        """Retrieve a single memory entry by ID.

        Args:
            entry_id: The entry ID to fetch.

        Returns:
            The :class:`~vetinari.memory.interfaces.MemoryEntry`, or None if
            not found or marked forgotten.
        """
        conn = get_connection()
        row = conn.execute("SELECT * FROM memories WHERE id = ? AND forgotten = 0", (entry_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_entry(row)

    def export(
        self,
        path: str,
        limit: int = 10_000,
        *,
        subject_id: str | None = None,
        allow_unscoped_export: bool = False,
    ) -> bool:
        """Export filtered memories to a JSONL file.

        Args:
            path: Destination file path (UTF-8 encoded JSONL).
            limit: Maximum number of entries to export (guards against OOM).
            subject_id: Optional subject identifier for subject-scoped export.
            allow_unscoped_export: Explicitly permit exporting all non-forgotten
                memories when no subject scope is supplied.

        Returns:
            True on success, False when an error occurs.
        """
        import json
        from pathlib import Path

        try:
            if not subject_id and not allow_unscoped_export:
                raise ValueError("subject_id is required for memory export")
            conn = get_connection()
            rows = conn.execute(
                "SELECT * FROM memories WHERE forgotten = 0 ORDER BY timestamp LIMIT ?", (limit,)
            ).fetchall()
            entries = [self._row_to_entry(row) for row in rows]
            if subject_id:
                from vetinari.privacy.erasure_registry import filter_subject_export

                serialized = filter_subject_export(entries, subject_id=subject_id)
            else:
                serialized = [entry.to_dict() for entry in entries]
            with Path(path).open("w", encoding="utf-8") as f:
                f.writelines(json.dumps(entry) + "\n" for entry in serialized)
            return True
        except Exception as exc:
            logger.error("Memory export failed: %s", exc)
            return False

    def _row_to_entry(self, row: Any) -> MemoryEntry:
        """Convert a SQLite row to a MemoryEntry.

        Args:
            row: sqlite3.Row from the memories table.

        Returns:
            Populated :class:`~vetinari.memory.interfaces.MemoryEntry`.
        """
        import json

        meta: dict[str, Any] = {}
        if row["metadata_json"]:
            try:
                meta = json.loads(row["metadata_json"])
            except Exception:
                meta = {}
        try:
            entry_type = MemoryType(row["entry_type"])
        except ValueError:
            entry_type = MemoryType.DISCOVERY
        return MemoryEntry(
            id=row["id"],
            agent=row["agent"],
            entry_type=entry_type,
            content=row["content"],
            summary=row["summary"],
            timestamp=row["timestamp"],
            provenance=row["provenance"],
            metadata=meta or None,
            scope=dict(row).get("scope", "global"),
        )
