"""Internal CRUD and lifecycle helpers for UnifiedMemoryStore.

Covers row conversion, entry storage, forgetting, updating, compaction,
stats, and eviction for the ``memories`` table.  Episode and search
operations live in ``_store_episode.py`` and ``_store_search.py``
respectively.

Not part of the public API — import only from ``vetinari.memory.unified``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Public re-exports from sub-modules (keeps existing callers working)
# ---------------------------------------------------------------------------
from ._store_episode import (
    evict_old_episodes,
    get_episode_stats,
    get_failure_patterns,
    insert_episode,
    recall_episodes_from_db,
    record_episode_full,
    row_to_episode_dict,
)
from ._store_insert import insert_memory_entry_row
from ._store_lifecycle import compact_memories, evict_low_importance_memories, get_memory_stats
from ._store_search import (
    build_timeline,
    fts_search,
    is_semantic_duplicate,
    like_search,
    manual_cosine_search,
    vec_knn_search,
)
from .interfaces import MemoryEntry, MemoryType

logger = logging.getLogger(__name__)


__all__ = [
    "build_timeline",
    "compact_memories",
    "evict_low_importance_memories",
    "evict_old_episodes",
    "export_memories",
    "filter_entry_secrets",
    "forget_memory",
    "fts_search",
    "get_entry_by_id",
    "get_episode_stats",
    "get_fact_chain",
    "get_failure_patterns",
    "get_memory_stats",
    "get_superseded_ids",
    "insert_episode",
    "is_semantic_duplicate",
    "like_search",
    "manual_cosine_search",
    "recall_episodes_from_db",
    "record_episode_full",
    "row_to_entry",
    "row_to_episode_dict",
    "set_relationship",
    "store_memory_entry",
    "update_memory_content",
    "vec_knn_search",
]


# ---------------------------------------------------------------------------
# Row conversion helper
# ---------------------------------------------------------------------------


def row_to_entry(row: sqlite3.Row) -> MemoryEntry:
    """Convert a memories table row to a MemoryEntry.

    Args:
        row: sqlite3.Row from the memories table.

    Returns:
        Populated :class:`~vetinari.memory.interfaces.MemoryEntry`.
    """
    try:
        entry_type = MemoryType(row["entry_type"])
    except ValueError:
        entry_type = MemoryType.DISCOVERY

    metadata = None
    if row["metadata_json"]:
        try:
            metadata = json.loads(row["metadata_json"])
        except (json.JSONDecodeError, TypeError):
            metadata = None

    # Safely read columns that may not exist in pre-migration databases
    def _col(name: str, default: Any = None) -> Any:
        try:
            return row[name]
        except (IndexError, KeyError):
            logger.warning(
                "Column %r not found in memory row — database may predate this schema migration, using default",
                name,
            )
            return default

    return MemoryEntry(
        id=row["id"],
        agent=row["agent"],
        entry_type=entry_type,
        content=row["content"],
        summary=row["summary"],
        timestamp=row["timestamp"],
        provenance=row["provenance"],
        source_backends=["unified"],
        metadata=metadata,
        recall_count=_col("recall_count", 0) or 0,
        supersedes_id=_col("supersedes_id"),
        relationship_type=_col("relationship_type"),
        scope=_col("scope", "global") or "global",
        last_accessed=_col("last_accessed", 0) or 0,
    )


# ---------------------------------------------------------------------------
# Security sanitisation
# ---------------------------------------------------------------------------


def filter_entry_secrets(entry: Any) -> Any:
    """Sanitize a MemoryEntry's content and metadata to remove secrets.

    Uses the global secret scanner singleton.  Mutates and returns the entry
    in place (MemoryEntry is not frozen).

    Args:
        entry: :class:`~vetinari.memory.interfaces.MemoryEntry` to sanitize.

    Returns:
        The same entry with secrets scrubbed from content and metadata.
    """
    from vetinari.security import get_secret_scanner

    scanner = get_secret_scanner()
    if entry.content:
        sanitized = scanner.sanitize(entry.content)
        if sanitized != entry.content:
            logger.debug("Entry content contained secrets — sanitized")
            entry.content = sanitized
    if entry.metadata and isinstance(entry.metadata, dict):
        sanitized_meta = scanner.sanitize_dict(entry.metadata)
        if sanitized_meta != entry.metadata:
            logger.debug("Entry metadata contained secrets — sanitized")
            entry.metadata = sanitized_meta
    return entry


def _redact_pii_value(value: Any) -> Any:
    """Return a JSON-like value with PII removed from every string."""
    from vetinari.safety.guardrails import redact_pii_payload

    return redact_pii_payload(value)


def _redact_memory_entry(entry: Any) -> Any:
    """Redact user content before it crosses the SQLite storage boundary."""
    entry.content = _redact_pii_value(entry.content)
    entry.summary = _redact_pii_value(entry.summary)
    if entry.metadata is not None:
        entry.metadata = _redact_pii_value(entry.metadata)
    return entry


# ---------------------------------------------------------------------------
# Entry storage
# ---------------------------------------------------------------------------


def _ensure_hash_chain_columns(conn: sqlite3.Connection) -> None:
    """Ensure the append-only memory integrity columns exist."""
    cursor = conn.execute("PRAGMA table_info(memories)")
    existing = {row[1] for row in cursor.fetchall()}
    if "previous_content_hash" not in existing:
        conn.execute("ALTER TABLE memories ADD COLUMN previous_content_hash TEXT NOT NULL DEFAULT ''")
    if "chain_hash" not in existing:
        conn.execute("ALTER TABLE memories ADD COLUMN chain_hash TEXT NOT NULL DEFAULT ''")


def _latest_memory_chain_hash(conn: sqlite3.Connection) -> str:
    """Return the newest chain hash, falling back to content_hash for old rows."""
    row = conn.execute(
        """
        SELECT chain_hash, content_hash
        FROM memories
        WHERE forgotten = 0
        ORDER BY rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return ""
    return row["chain_hash"] or row["content_hash"] or ""


def _memory_chain_hash(previous_hash: str, entry_id: str, content_digest: str) -> str:
    """Bind one memory append to the previous append and current content."""
    payload = f"{previous_hash}\n{entry_id}\n{content_digest}".encode()
    return hashlib.sha256(payload).hexdigest()


def _entry_type_value(entry_type: Any) -> str:
    return entry_type.value if hasattr(entry_type, "value") else str(entry_type)


def _existing_memory_id_for_hash(conn: sqlite3.Connection, c_hash: str, entry: Any) -> str | None:
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id FROM memories
        WHERE content_hash = ?
          AND forgotten = 0
          AND COALESCE(scope, 'global') = ?
          AND (COALESCE(provenance, '') = ? OR COALESCE(provenance, '') = '' OR ? = '')
          AND COALESCE(agent, '') = ?
          AND entry_type = ?
        ORDER BY created_at DESC, rowid DESC
        LIMIT 1
        """,
        (
            c_hash,
            getattr(entry, "scope", "global") or "global",
            getattr(entry, "provenance", "") or "",
            getattr(entry, "provenance", "") or "",
            getattr(entry, "agent", "") or "",
            _entry_type_value(getattr(entry, "entry_type", "")),
        ),
    )
    existing = cursor.fetchone()
    if existing:
        logger.debug("Skipping duplicate memory within same scope/provenance/type boundary: %s", existing["id"])
        return str(existing["id"])
    return None


def store_memory_entry(
    conn: sqlite3.Connection,
    entry: Any,
    store_embedding_fn: Any,
) -> str:
    """Persist a memory entry with content hash deduplication.

    Skips the write when an identical content hash already exists.
    Calls ``store_embedding_fn(memory_id, text)`` after a successful insert.

    Args:
        conn: Active SQLite connection.
        entry: :class:`~vetinari.memory.interfaces.MemoryEntry` to persist.
        store_embedding_fn: Callable ``(memory_id: str, text: str) -> None``
            invoked after a successful insert for best-effort embedding storage.

    Returns:
        The ``entry.id`` that was stored, or the existing ID when deduplication
        skips the write.

    Raises:
        vetinari.exceptions.StorageError: If the database INSERT fails.
    """
    from vetinari.exceptions import StorageError

    try:
        entry = _redact_memory_entry(entry)
    except Exception as exc:
        logger.exception("Memory PII redaction failed for %s — refusing to store raw content", entry.id)
        raise StorageError("Memory store redaction failed; raw content was not stored") from exc

    from .interfaces import content_hash

    c_hash = content_hash(entry.content)

    _ensure_hash_chain_columns(conn)
    existing = _existing_memory_id_for_hash(conn, c_hash, entry)
    if existing:
        return existing

    previous_hash = _latest_memory_chain_hash(conn)
    chain_hash = _memory_chain_hash(previous_hash, entry.id, c_hash)

    try:
        insert_memory_entry_row(conn, entry, c_hash, previous_hash, chain_hash)
        conn.commit()
        store_embedding_fn(entry.id, entry.content)
        logger.debug("Stored memory %s (agent=%s, type=%s)", entry.id, entry.agent, entry.entry_type)
        return str(entry.id)
    except sqlite3.Error as exc:
        logger.error("Failed to store memory %s: %s", entry.id, exc)
        raise StorageError(f"Memory store write failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_memories(
    conn: sqlite3.Connection,
    path: str,
    *,
    subject_id: str | None = None,
    allow_unscoped_export: bool = False,
) -> bool:
    """Export memories to a JSON file, failing closed for unscoped exports.

    Args:
        conn: Active SQLite connection.
        path: Output file path (UTF-8 encoded JSON).
        subject_id: Required subject filter for normal exports.
        allow_unscoped_export: Explicit administrative override.

    Returns:
        True on success, False when an error occurs.
    """
    from pathlib import Path

    try:
        if not subject_id and not allow_unscoped_export:
            raise ValueError("subject_id is required for memory export")
        rows = conn.execute("SELECT * FROM memories WHERE forgotten = 0 ORDER BY timestamp DESC").fetchall()
        raw_entries = [row_to_entry(row) for row in rows]
        if subject_id:
            from vetinari.privacy.erasure_registry import build_erasure_token, filter_subject_export

            entries = filter_subject_export(raw_entries, subject_id=subject_id)
            export_scope = {
                "subject_id": subject_id,
                "erasure_token": build_erasure_token(source="memory.export", subject_id=subject_id),
                "redaction_applied": True,
            }
        else:
            entries = [entry.to_dict() for entry in raw_entries]
            export_scope = {"administrative_unscoped_export": True, "redaction_applied": False}
        with Path(path).open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "exported_at": datetime.now(timezone.utc).isoformat(),
                    "privacy_export": export_scope,
                    "entries": entries,
                },
                f,
                indent=2,
            )
        return True
    except Exception as exc:
        logger.error("Export failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Soft delete and content update
# ---------------------------------------------------------------------------


def forget_memory(conn: sqlite3.Connection, entry_id: str, reason: str) -> bool:
    """Mark a memory as forgotten (soft delete).

    Args:
        conn: Active SQLite connection.
        entry_id: The entry ID to forget.
        reason: Reason for forgetting (logged at INFO).

    Returns:
        True if the entry was found and marked.
    """
    forgotten_marker = "[forgotten]"
    _ensure_hash_chain_columns(conn)
    row = conn.execute(
        "SELECT content_hash, chain_hash FROM memories WHERE id = ? AND forgotten = 0",
        (entry_id,),
    ).fetchone()
    if row is None:
        return False
    forgotten_digest = f"forgotten:{entry_id}"
    previous_hash = row["chain_hash"] or row["content_hash"] or ""
    chain_hash = _memory_chain_hash(previous_hash, entry_id, forgotten_digest)
    cursor = conn.execute(
        """
        UPDATE memories
        SET forgotten = 1,
            content = ?,
            summary = '',
            metadata_json = NULL,
            content_hash = ?,
            previous_content_hash = ?,
            chain_hash = ?,
            updated_at = ?
        WHERE id = ? AND forgotten = 0
        """,
        (
            forgotten_marker,
            forgotten_digest,
            previous_hash,
            chain_hash,
            datetime.now(timezone.utc).isoformat(),
            entry_id,
        ),
    )
    conn.commit()
    if cursor.rowcount > 0:
        logger.info("Forgot memory %s: %s", entry_id, reason)
        return True
    return False


def update_memory_content(conn: sqlite3.Connection, entry_id: str, new_content: str) -> bool:
    """Update the content of an existing memory entry.

    Args:
        conn: Active SQLite connection.
        entry_id: The entry ID to update.
        new_content: Replacement content string.

    Returns:
        True if the entry was found and updated.

    Raises:
        vetinari.exceptions.StorageError: If redaction fails before the
            replacement content can be persisted.
    """
    from vetinari.exceptions import StorageError

    from .interfaces import content_hash

    try:
        redacted_content = _redact_pii_value(new_content)
        from vetinari.security import get_secret_scanner

        redacted_content = get_secret_scanner().sanitize(redacted_content)
    except Exception as exc:
        logger.exception("Memory update PII redaction failed for %s — refusing to store raw content", entry_id)
        raise StorageError("Memory update redaction failed; raw content was not stored") from exc

    _ensure_hash_chain_columns(conn)
    row = conn.execute(
        "SELECT content_hash, chain_hash FROM memories WHERE id = ? AND forgotten = 0",
        (entry_id,),
    ).fetchone()
    if row is None:
        return False
    new_content_hash = content_hash(redacted_content)
    previous_hash = row["chain_hash"] or row["content_hash"] or ""
    chain_hash = _memory_chain_hash(previous_hash, entry_id, new_content_hash)
    cursor = conn.execute(
        """
        UPDATE memories
        SET content = ?,
            content_hash = ?,
            previous_content_hash = ?,
            chain_hash = ?,
            updated_at = ?
        WHERE id = ? AND forgotten = 0
        """,
        (
            redacted_content,
            new_content_hash,
            previous_hash,
            chain_hash,
            datetime.now(timezone.utc).isoformat(),
            entry_id,
        ),
    )
    if cursor.rowcount > 0:
        conn.execute("DELETE FROM embeddings WHERE memory_id = ?", (entry_id,))
        with contextlib.suppress(sqlite3.Error):
            conn.execute("DELETE FROM memory_vec WHERE memory_id = ?", (entry_id,))
    conn.commit()
    if cursor.rowcount > 0:
        logger.debug("Updated memory %s content", entry_id)
        return True
    return False


# ---------------------------------------------------------------------------
# Compaction and capacity management
# ---------------------------------------------------------------------------


def get_fact_chain(conn: sqlite3.Connection, entry_id: str) -> list[MemoryEntry]:
    """Walk the supersedes_id chain from newest to oldest.

    Starting from *entry_id*, follows supersedes_id links until the chain
    ends (NULL) or a cycle is detected.  Only non-forgotten entries are
    included.

    Args:
        conn: Active SQLite connection.
        entry_id: Starting entry ID (typically the newest in the chain).

    Returns:
        Ordered list of MemoryEntry from newest to oldest.
    """
    chain: list[MemoryEntry] = []
    visited: set[str] = set()
    current_id: str | None = entry_id
    while current_id and current_id not in visited:
        visited.add(current_id)
        row = conn.execute("SELECT * FROM memories WHERE id = ? AND forgotten = 0", (current_id,)).fetchone()
        if row is None:
            break
        chain.append(row_to_entry(row))
        try:
            current_id = row["supersedes_id"]
        except (IndexError, KeyError):
            break
    return chain


def set_relationship(
    conn: sqlite3.Connection,
    source_id: str,
    target_id: str,
    relationship_type: str,
) -> bool:
    """Create a typed relationship from source to target memory.

    Sets ``supersedes_id`` and ``relationship_type`` on the source entry
    to link it to the target.

    Args:
        conn: Active SQLite connection.
        source_id: The newer entry that supersedes/relates to target.
        target_id: The older entry being referenced.
        relationship_type: One of :class:`~vetinari.types.RelationshipType` values.

    Returns:
        True if the source entry was found and updated.
    """
    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        "UPDATE memories SET supersedes_id = ?, relationship_type = ?, updated_at = ? WHERE id = ? AND forgotten = 0",
        (target_id, relationship_type, now, source_id),
    )
    conn.commit()
    if cursor.rowcount > 0:
        logger.debug("Relationship %s -> %s (%s)", source_id, target_id, relationship_type)
        return True
    return False


def get_superseded_ids(conn: sqlite3.Connection) -> set[str]:
    """Return IDs of entries that have been superseded by another live entry.

    An entry is superseded when another non-forgotten entry references it
    via ``supersedes_id``.

    Args:
        conn: Active SQLite connection.

    Returns:
        Set of memory IDs that are superseded.
    """
    rows = conn.execute(
        "SELECT supersedes_id FROM memories WHERE supersedes_id IS NOT NULL AND forgotten = 0"
    ).fetchall()
    return {row[0] for row in rows}


def get_entry_by_id(conn: sqlite3.Connection, entry_id: str) -> Any | None:
    """Fetch a memory entry by ID, incrementing its access count.

    Args:
        conn: Active SQLite connection.
        entry_id: The entry ID to fetch.

    Returns:
        The sqlite3.Row if found and not forgotten, otherwise None.
    """
    row = conn.execute("SELECT * FROM memories WHERE id = ? AND forgotten = 0", (entry_id,)).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE memories SET access_count = access_count + 1, recall_count = COALESCE(recall_count, 0) + 1, updated_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), entry_id),
    )
    conn.commit()
    return row
