"""SQLite migration helpers."""

from __future__ import annotations

import sqlite3

from vetinari.migrations.runner import run_migrations as _run_migrations


def upgrade_benchmarks_table(db_path: str) -> bool:
    """Ensure the benchmark table has the production benchmark schema.

    Args:
        db_path: SQLite database path.

    Returns:
        True when the upgrade check completed.

    Raises:
        RuntimeError: If an existing table cannot be migrated to the required
            schema or required columns remain absent after migration.
    """
    required_columns = {
        "id": "TEXT PRIMARY KEY",
        "run_id": "TEXT NOT NULL DEFAULT ''",
        "suite_name": "TEXT",
        "model_id": "TEXT",
        "pass_rate": "REAL",
        "started_at": "TEXT NOT NULL DEFAULT ''",
        "completed_at": "TEXT",
    }
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS benchmarks (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL DEFAULT '',
                suite_name TEXT,
                model_id TEXT,
                pass_rate REAL,
                started_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT
            )
            """
        )
        existing = {row[1] for row in conn.execute("PRAGMA table_info(benchmarks)")}
        for column, definition in required_columns.items():
            if column in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE benchmarks ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError as exc:
                raise RuntimeError(f"could not add benchmarks.{column}: {exc}") from exc
        columns = {row[1] for row in conn.execute("PRAGMA table_info(benchmarks)")}
        missing = set(required_columns) - columns
        if missing:
            raise RuntimeError(f"benchmarks table missing columns after migration: {sorted(missing)}")
        conn.commit()
    finally:
        conn.close()
    return True


def run_migrations(db_path: str) -> dict[str, list[int]]:
    """Run database migrations.

    Args:
        db_path: SQLite database path.

    Returns:
        Migration result summary.

    Raises:
        RuntimeError: When the underlying runner rejects the db_path argument
            (TypeError from a signature mismatch). Re-raises every other
            unhandled exception unchanged so silent migration failures do not
            masquerade as empty success.
    """
    try:
        applied_count = _run_migrations(db_path)
    except TypeError as exc:
        msg = f"Migration runner rejected db path {db_path!r}: {exc}"
        raise RuntimeError(msg) from exc
    return {"applied": list(range(applied_count)), "failed": []}


__all__ = ["run_migrations", "upgrade_benchmarks_table"]
