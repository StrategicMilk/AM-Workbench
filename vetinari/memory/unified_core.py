"""Core memory CRUD helpers for the unified long-term memory store.

These methods cover durable memory entries, fact-graph relationships, access
timestamps, and lifecycle statistics. Episode-specific behavior lives in
``unified_episodes.py``.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.database import _get_db_path

from ._store_ops import (
    compact_memories,
    evict_low_importance_memories,
    export_memories,
    filter_entry_secrets,
    forget_memory,
    get_entry_by_id,
    get_fact_chain,
    get_memory_stats,
    row_to_entry,
    set_relationship,
    store_memory_entry,
    update_memory_content,
)
from .interfaces import MemoryEntry, MemoryStats

logger = logging.getLogger(__name__)


class _CoreStoreMixin:
    """Persistent CRUD, fact-graph, and statistics methods for UnifiedMemoryStore."""

    if TYPE_CHECKING:
        _conn: Any
        _lock: Any
        _max_entries: Any
        _store_embedding_for: Any

    def remember(self, entry: MemoryEntry) -> str:
        """Scan for secrets, dedup by content hash, then persist.

        Returns:
            The unique ID assigned to the stored entry.
        """
        entry = filter_entry_secrets(entry)
        with self._lock:
            stored_id = store_memory_entry(self._conn, entry, self._store_embedding_for)
            evict_low_importance_memories(self._conn, self._max_entries)
            return stored_id

    def export(
        self,
        path: str,
        *,
        subject_id: str | None = None,
        allow_unscoped_export: bool = False,
    ) -> bool:
        """Dump filtered memories to *path* as JSON.

        Returns:
            True when the file was written successfully, False on any I/O error.
        """
        with self._lock:
            return export_memories(
                self._conn,
                path,
                subject_id=subject_id,
                allow_unscoped_export=allow_unscoped_export,
            )

    def forget(self, entry_id: str, reason: str) -> bool:
        """Soft-delete an entry by marking it as a tombstone so it is excluded from all future queries.

        Args:
            entry_id: The unique ID of the memory entry to tombstone.
            reason: Human-readable explanation for why the entry is being forgotten
                (stored for audit purposes).

        Returns:
            True when the entry existed and was tombstoned, False when the ID was not found.
        """
        with self._lock:
            return forget_memory(self._conn, entry_id, reason)

    def update_content(self, entry_id: str, new_content: str) -> bool:
        """Replace the stored text of an existing memory entry in place.

        Args:
            entry_id: The unique ID of the memory entry to modify.
            new_content: The replacement content string; must be non-empty.

        Returns:
            True when the entry existed and was updated, False when the ID was not found.
        """
        with self._lock:
            return update_memory_content(self._conn, entry_id, new_content)

    def fact_graph(self, entry_id: str) -> list[MemoryEntry]:
        """Walk the supersession chain from *entry_id* back to its origin.

        Follows ``supersedes_id`` links through non-forgotten entries,
        returning the full lineage from newest to oldest. Useful for
        understanding how a fact evolved over time.

        Args:
            entry_id: Starting entry ID (typically the newest revision).

        Returns:
            Ordered list of MemoryEntry from newest to oldest in the chain.
        """
        with self._lock:
            return get_fact_chain(self._conn, entry_id)

    def create_relationship(
        self,
        source_id: str,
        target_id: str,
        relationship_type: str,
    ) -> bool:
        """Link two memory entries with a typed relationship.

        Sets ``supersedes_id`` and ``relationship_type`` on the source entry
        so that chain-aware search can filter or walk the resulting graph.

        Args:
            source_id: The newer entry that references *target_id*.
            target_id: The older entry being referenced.
            relationship_type: One of :class:`~vetinari.types.RelationshipType` values
                (``supersedes``, ``contradicts``, ``caused_by``, ``elaborates``).

        Returns:
            True when the source entry was found and updated, False otherwise.
        """
        with self._lock:
            return set_relationship(self._conn, source_id, target_id, relationship_type)

    def compact(self, max_age_days: int | None = None) -> int:
        """Delete forgotten entries and optionally prune by age.

        Returns:
            Number of entries permanently removed from the store.
        """
        with self._lock:
            return compact_memories(self._conn, max_age_days)

    def stats(self) -> MemoryStats:
        """Return aggregate counts, timestamps, and file size for the store.

        Returns:
            MemoryStats snapshot covering total entries, type breakdown, and disk usage.
        """
        with self._lock:
            return MemoryStats(**get_memory_stats(self._conn, _get_db_path))

    def get_entry(self, entry_id: str) -> MemoryEntry | None:
        """Fetch entry by ID (increments access count).

        Returns:
            The matching MemoryEntry, or None when the ID does not exist or is tombstoned.
        """
        with self._lock:
            row = get_entry_by_id(self._conn, entry_id)
        return row_to_entry(row) if row is not None else None

    def _touch_accessed(self, entry_ids: list[str]) -> None:
        """Update last_accessed timestamp for retrieved memory entries."""
        if not entry_ids:
            return
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        with self._lock:
            try:
                self._conn.executemany(
                    "UPDATE memories SET last_accessed = ? WHERE id = ?",
                    [(now_ms, entry_id) for entry_id in entry_ids],
                )
                self._conn.commit()
            except sqlite3.Error:
                logger.warning("Could not update last_accessed for retrieved entries - timestamps may be stale")

    @staticmethod
    def _filter_secrets(entry: MemoryEntry) -> MemoryEntry:
        """Delegate to ``filter_entry_secrets``; kept for backward compatibility."""
        return filter_entry_secrets(entry)

    def _row_to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        """Convert a memories table row to a MemoryEntry."""
        return row_to_entry(row)
