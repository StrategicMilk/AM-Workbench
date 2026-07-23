"""Migration 001: add composite indexes for workbench query performance."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "001_composite_indexes"


class MigrationError(Exception):
    """Raised when the metadata spine schema is not ready for migration."""


def run(conn: sqlite3.Connection) -> None:
    """Apply idempotent indexes to the Workbench metadata spine.

    Args:
        conn: Open SQLite connection for the metadata spine.

    Raises:
        MigrationError: If the expected spine table is missing.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'records'")
    if cursor.fetchone() is None:
        raise MigrationError(
            "Migration 001 cannot run: table 'records' does not exist. "
            "Ensure the metadata spine schema exists before applying indexes."
        )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_spine_kind_project_created
        ON records (
            kind,
            json_extract(payload, '$.project_id'),
            COALESCE(
                json_extract(payload, '$.created_at_utc'),
                json_extract(payload, '$.captured_at_utc'),
                json_extract(payload, '$.started_at_utc'),
                json_extract(payload, '$.opened_at_utc'),
                ''
            ),
            created_order
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_spine_rag_revision_query_hash
        ON records (
            COALESCE(
                json_extract(payload, '$.rag_revision_id'),
                json_extract(payload, '$.revision_id'),
                record_id
            ),
            json_extract(payload, '$.query_hash')
        )
        """
    )
    conn.commit()
