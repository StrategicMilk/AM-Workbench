"""SQLite schema bootstrap helpers."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _resolve_default_db_path() -> Path:
    """Resolve the default Vetinari DB path from env, with data-root fallback.

    Priority order:
      1. ``VETINARI_DB_PATH`` — explicit override (canonical).
      2. ``VETINARI_DATA_ROOT`` — operator-configured data root; DB lives at
         ``<root>/vetinari.db`` to match get_connection()'s default.
      3. Built-in fallback at ``.vetinari/vetinari.db`` (legacy single-folder
         install layout).
    """
    explicit = os.environ.get("VETINARI_DB_PATH")
    if explicit:
        return Path(explicit)
    data_root = os.environ.get("VETINARI_DATA_ROOT")
    if data_root:
        return Path(data_root) / "vetinari.db"
    return Path(".vetinari/vetinari.db")


def bootstrap_schema(db_path: str | None = None) -> str:
    """Initialize the SQLite schema if needed.

    Args:
        db_path: Optional database path. Defaults to the same resolution rule
            as ``vetinari.db.connection.get_connection``: ``VETINARI_DB_PATH``
            wins, then ``VETINARI_DATA_ROOT`` joined with ``vetinari.db``,
            then the legacy ``.vetinari/vetinari.db`` fallback.

    Returns:
        Database path initialized. Always runs the packaged numbered
        migrations under ``vetinari/migrations/`` so callers see the same
        schema regardless of how the DB was created.
    """
    path = Path(db_path) if db_path else _resolve_default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Initializing Vetinari database schema at %s", path)
    # Apply packaged numbered migrations (creates _migration_history and
    # records the 001_record_schema_versions.sql application).
    from vetinari.migrations.runner import run_migrations as _runner

    _runner(path)
    return str(path)


__all__ = ["bootstrap_schema"]
