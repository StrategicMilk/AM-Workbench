"""SQLite index helpers for the dataset revision store."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from vetinari.workbench.dataset_revision_records import (
    DatasetBranch,
    DatasetRevision,
    DatasetRevisionError,
    DatasetRevisionSchemaMismatch,
    DatasetTag,
    _to_jsonable,
)
from vetinari.workbench.spine_consumers import record_asset_written


def require_conn(store: Any) -> sqlite3.Connection:
    """Return the open SQLite connection for a dataset revision store.

    Returns:
        Open SQLite connection owned by the store.

    Raises:
        DatasetRevisionError: If the store connection has been closed.
    """
    if store._conn is None:
        raise DatasetRevisionError("dataset revision SQLite connection is closed")
    return store._conn


def check_sqlite_integrity(store: Any) -> None:
    """Fail if the rebuildable SQLite index is corrupt.

    Raises:
        DatasetRevisionSchemaMismatch: If SQLite integrity checks fail.
    """
    try:
        conn = sqlite3.connect(store._sqlite_path)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise DatasetRevisionSchemaMismatch("SQLite integrity_check failed") from exc
    if rows != [("ok",)]:
        raise DatasetRevisionSchemaMismatch("SQLite integrity_check failed")


def create_schema(store: Any) -> None:
    """Create the dataset revision index schema if it is absent."""
    conn = require_conn(store)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS revisions (
            revision_id TEXT PRIMARY KEY,
            branch TEXT NOT NULL,
            parent_revision_id TEXT,
            payload TEXT NOT NULL,
            created_order INTEGER NOT NULL
        )
        """
    )
    conn.execute("CREATE TABLE IF NOT EXISTS branches (name TEXT PRIMARY KEY, payload TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS tags (name TEXT PRIMARY KEY, payload TEXT NOT NULL)")


def rebuild_sqlite(store: Any) -> None:
    """Rebuild the SQLite index from JSONL source-of-truth records.

    Raises:
        DatasetRevisionSchemaMismatch: If the index cannot be rebuilt atomically.
    """
    conn = require_conn(store)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM revisions")
        conn.execute("DELETE FROM branches")
        conn.execute("DELETE FROM tags")
        for order, revision_id in enumerate(store._revision_order):
            revision = store._revisions[revision_id]
            conn.execute(
                """
                INSERT INTO revisions(revision_id, branch, parent_revision_id, payload, created_order)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    revision.revision_id,
                    revision.branch,
                    revision.parent_revision_id,
                    _record_payload(revision),
                    order,
                ),
            )
        for branch in store._branches.values():
            conn.execute("INSERT INTO branches(name, payload) VALUES (?, ?)", (branch.name, _record_payload(branch)))
        for tag in store._tags.values():
            conn.execute("INSERT INTO tags(name, payload) VALUES (?, ?)", (tag.name, _record_payload(tag)))
        conn.execute("COMMIT")
    except sqlite3.DatabaseError as exc:
        conn.execute("ROLLBACK")
        raise DatasetRevisionSchemaMismatch("SQLite rebuild failed") from exc


def insert_revision_sqlite(store: Any, revision: DatasetRevision) -> None:
    """Insert one revision row into the SQLite index.

    Args:
        store: DatasetRevisionStore-compatible owner of the SQLite connection.
        revision: Revision record to index.

    Raises:
        DatasetRevisionSchemaMismatch: If SQLite rejects the revision row.
    """
    try:
        require_conn(store).execute(
            """
            INSERT INTO revisions(revision_id, branch, parent_revision_id, payload, created_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                revision.revision_id,
                revision.branch,
                revision.parent_revision_id,
                _record_payload(revision),
                len(store._revision_order) - 1,
            ),
        )
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_asset_written(
            asset_id=revision.revision_id,
            kind="dataset",
            project_id=str(getattr(store, "_project_id", "default")),
        )
    except sqlite3.DatabaseError as exc:
        raise DatasetRevisionSchemaMismatch("SQLite revision insert failed") from exc


def upsert_branch_sqlite(store: Any, branch: DatasetBranch) -> None:
    """Insert or replace a branch row in the SQLite index.

    Args:
        store: DatasetRevisionStore-compatible owner of the SQLite connection.
        branch: Branch record to index.

    Raises:
        DatasetRevisionSchemaMismatch: If SQLite rejects the branch row.
    """
    try:
        require_conn(store).execute(
            "INSERT OR REPLACE INTO branches(name, payload) VALUES (?, ?)",
            (branch.name, _record_payload(branch)),
        )
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_asset_written(
            asset_id=f"dataset-branch-{branch.name}",
            kind="dataset",
            project_id=str(getattr(store, "_project_id", "default")),
        )
    except sqlite3.DatabaseError as exc:
        raise DatasetRevisionSchemaMismatch("SQLite branch insert failed") from exc


def insert_tag_sqlite(store: Any, tag: DatasetTag) -> None:
    """Insert a tag row into the SQLite index.

    Args:
        store: DatasetRevisionStore-compatible owner of the SQLite connection.
        tag: Tag record to index.

    Raises:
        DatasetRevisionSchemaMismatch: If SQLite rejects the tag row.
    """
    try:
        require_conn(store).execute(
            "INSERT INTO tags(name, payload) VALUES (?, ?)",
            (tag.name, _record_payload(tag)),
        )
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_asset_written(
            asset_id=f"dataset-tag-{tag.name}",
            kind="dataset",
            project_id=str(getattr(store, "_project_id", "default")),
        )
    except sqlite3.DatabaseError as exc:
        raise DatasetRevisionSchemaMismatch("SQLite tag insert failed") from exc


def _record_payload(record: Any) -> str:
    return json.dumps(_to_jsonable(record), separators=(",", ":"), sort_keys=True)


__all__ = [
    "check_sqlite_integrity",
    "create_schema",
    "insert_revision_sqlite",
    "insert_tag_sqlite",
    "rebuild_sqlite",
    "require_conn",
    "upsert_branch_sqlite",
]
