"""Compatibility schema surface for memory-store column checks."""

from __future__ import annotations

from vetinari.memory._schema import _SCHEMA_SQL

CREATE_SQL = _SCHEMA_SQL
COLUMNS = (
    "id",
    "agent",
    "entry_type",
    "content",
    "summary",
    "timestamp",
    "provenance",
    "content_hash",
    "created_at",
    "updated_at",
)

__all__ = ["COLUMNS", "CREATE_SQL"]
