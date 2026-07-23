"""Insert helpers for UnifiedMemoryStore rows."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .interfaces import MemoryType


def insert_memory_entry_row(
    conn: sqlite3.Connection,
    entry: Any,
    content_digest: str,
    previous_hash: str,
    chain_hash: str,
) -> None:
    """Insert one memory entry row with integrity-chain fields.

    Args:
        conn: Conn value consumed by insert_memory_entry_row().
        entry: Entry value consumed by insert_memory_entry_row().
        content_digest: Content digest value consumed by insert_memory_entry_row().
        previous_hash: Previous hash value consumed by insert_memory_entry_row().
        chain_hash: Chain hash value consumed by insert_memory_entry_row().
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.cursor().execute(
        """INSERT INTO memories
           (id, agent, entry_type, content, summary, timestamp,
            provenance, content_hash, previous_content_hash, chain_hash, forgotten, access_count,
            quality_score, importance, created_at, updated_at, metadata_json,
            scope, recall_count, supersedes_id, relationship_type, last_accessed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0.0, 0.5, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry.id,
            entry.agent,
            entry.entry_type.value if isinstance(entry.entry_type, MemoryType) else str(entry.entry_type),
            entry.content,
            entry.summary,
            entry.timestamp,
            entry.provenance,
            content_digest,
            previous_hash,
            chain_hash,
            now,
            now,
            json.dumps(entry.metadata) if entry.metadata else None,
            entry.scope,
            entry.recall_count,
            entry.supersedes_id,
            entry.relationship_type,
            entry.last_accessed,
        ),
    )
