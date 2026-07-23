"""Lifecycle receipt helpers for unified memory pruning.

The receipt table records non-restorable pruning events without storing raw
memory or episode text.  It is created on demand so older private stores and
the shared production schema both gain the audit trail before the first prune.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

_RECEIPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_lifecycle_receipts (
    receipt_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    store TEXT NOT NULL,
    action TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    items_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memory_lifecycle_receipts_store
ON memory_lifecycle_receipts(store, action, created_at);
"""


def ensure_lifecycle_receipts_table(conn: sqlite3.Connection) -> None:
    """Create the pruning receipt table if this store does not have it yet."""
    conn.executescript(_RECEIPT_SCHEMA)


def _hash_fields(row: sqlite3.Row, fields: Iterable[str]) -> str:
    row_keys = set(row.keys())
    payload = {field: row[field] for field in fields if field in row_keys and row[field] is not None}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_lifecycle_receipt(
    conn: sqlite3.Connection,
    *,
    store: str,
    action: str,
    rows: list[sqlite3.Row],
    id_field: str,
    hash_fields: tuple[str, ...],
    metadata_fields: tuple[str, ...] = (),
) -> None:
    """Record a compact receipt for rows that are about to be pruned.

    Raw content fields are not copied into the receipt; only an irreversible
    hash over caller-selected fields plus non-content metadata is persisted.
    """
    if not rows:
        return
    ensure_lifecycle_receipts_table(conn)
    items: list[dict[str, Any]] = []
    for row in rows:
        row_keys = set(row.keys())
        item: dict[str, Any] = {
            "id": row[id_field],
            "content_hash": row["content_hash"] if "content_hash" in row_keys else _hash_fields(row, hash_fields),
        }
        for field in metadata_fields:
            if field in row_keys:
                item[field] = row[field]
        items.append(item)

    conn.execute(
        """
        INSERT INTO memory_lifecycle_receipts
            (receipt_id, created_at, store, action, item_count, items_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            f"mlr_{uuid.uuid4().hex[:12]}",
            datetime.now(timezone.utc).isoformat(),
            store,
            action,
            len(items),
            json.dumps(items, sort_keys=True, separators=(",", ":")),
        ),
    )
