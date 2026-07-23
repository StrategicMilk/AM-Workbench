"""Storage health helpers."""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


def check_sqlite_health(db_path: str) -> dict[str, bool | str]:
    """Check SQLite writability.

    Args:
        db_path: SQLite database path.

    Returns:
        SQLite health mapping.
    """
    if db_path == ":memory:":
        logger.warning("SQLite health check received non-durable in-memory database path")
        return {"writable": False, "db_path": db_path, "error": "in-memory database is not durable"}

    try:
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS _vetinari_healthcheck (id INTEGER PRIMARY KEY)")
            conn.execute("INSERT INTO _vetinari_healthcheck DEFAULT VALUES")
            conn.execute("DELETE FROM _vetinari_healthcheck")
            conn.commit()
    except Exception as exc:
        logger.warning("SQLite health check failed for %s: %s", db_path, type(exc).__name__)
        return {"writable": False, "db_path": db_path, "error": str(exc)}
    return {"writable": True, "db_path": db_path}


__all__ = ["check_sqlite_health"]
