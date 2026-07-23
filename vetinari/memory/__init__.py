"""Vetinari Memory Module — unified storage with three access patterns.

Which getter to use:

- ``get_unified_memory_store()`` — core storage (memories, episodes, embeddings).
  Use for agent recall, episode recording, and direct memory CRUD.
- ``get_memory_store()`` — plan execution tracking (plan history, subtask logs).
  Use when recording or querying plan/subtask lifecycle events.
- ``get_shared_memory()`` — facade over all subsystems (store + plan tracking
  + blackboard). Use when you need access to multiple subsystems from one object.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import vetinari.constants as _constants

from .intent_parser import (
    IntentParser,
    ParsedQuery,
    QueryIntent,
    get_intent_parser,
)
from .interfaces import (
    DUAL_MEMORY_AVAILABLE,
    ApprovalDetails,
    MemoryEntry,
    MemoryStats,
    MemoryType,
    content_hash,
)
from .plan_tracking import (
    MemoryStore,
    get_memory_store,
    init_memory_store,
)
from .shared import SharedMemory, get_shared_memory
from .unified import (
    RecordedEpisode,
    SessionContext,
    UnifiedMemoryStore,
    get_unified_memory_store,
    init_unified_memory_store,
)

logger = logging.getLogger(__name__)


PLAN_MEMORY_DB_PATH = "vetinari_memory.db"
PLAN_RETENTION_DAYS = 90
PLAN_ADMIN_TOKEN = ""


def get_plan_memory_db_path() -> str:
    """Return the plan-memory database path from the runtime environment."""
    return os.environ.get("PLAN_MEMORY_DB_PATH", str(_constants._PROJECT_ROOT / "vetinari_memory.db"))


def get_plan_retention_days() -> int:
    """Return the runtime plan retention window, failing closed for bad values.

    Returns:
        Retention window in days from PLAN_RETENTION_DAYS, or the package
        default when the environment variable is unset or empty.

    Raises:
        ValueError: If PLAN_RETENTION_DAYS is set to a non-integer value.
    """
    raw = os.environ.get("PLAN_RETENTION_DAYS")
    if raw is None or raw == "":
        return PLAN_RETENTION_DAYS
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError("PLAN_RETENTION_DAYS must be an integer") from exc


def get_plan_admin_token() -> str:
    """Return the legacy plan admin token from the runtime environment."""
    return os.environ.get("PLAN_ADMIN_TOKEN", "")


def get_plan_memory_db_file() -> Path:
    """Return the runtime plan-memory database path as a Path."""
    return Path(get_plan_memory_db_path())


Episode = RecordedEpisode

__all__ = [
    "DUAL_MEMORY_AVAILABLE",
    "PLAN_ADMIN_TOKEN",
    "PLAN_MEMORY_DB_PATH",
    "PLAN_RETENTION_DAYS",
    "ApprovalDetails",
    "Episode",
    "IntentParser",
    "MemoryEntry",
    "MemoryStats",
    "MemoryStore",
    "MemoryType",
    "ParsedQuery",
    "QueryIntent",
    "RecordedEpisode",
    "SessionContext",
    "SharedMemory",
    "UnifiedMemoryStore",
    "content_hash",
    "get_intent_parser",
    "get_memory_store",
    "get_plan_admin_token",
    "get_plan_memory_db_file",
    "get_plan_memory_db_path",
    "get_plan_retention_days",
    "get_shared_memory",
    "get_unified_memory_store",
    "init_memory_store",
    "init_unified_memory_store",
]
