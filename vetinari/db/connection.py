"""SQLite connection helpers."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from vetinari.constants import get_user_dir


def _default_db_path() -> str:
    explicit = os.environ.get("VETINARI_DB_PATH")
    if explicit:
        return explicit
    data_root = (
        Path(os.environ["VETINARI_DATA_ROOT"]).expanduser() if "VETINARI_DATA_ROOT" in os.environ else get_user_dir()
    )
    path = data_root / "vetinari.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def get_connection(db_path: str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection.

    Args:
        db_path: Optional database path. Defaults to ``VETINARI_DB_PATH``.

    Returns:
        SQLite connection.
    """
    return sqlite3.connect(db_path or _default_db_path())


__all__ = ["get_connection"]
