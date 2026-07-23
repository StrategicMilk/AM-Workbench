"""UnifiedMemoryStore — the single long-term memory backend for all agents.

Owns the SQLite database, FTS5 index, and vector embeddings.  Every
memory operation (store, search, forget, consolidate) flows through this
module.  It is the "warehouse" in the factory metaphor: raw materials
(episodic observations) arrive, get consolidated into knowledge (semantic
patterns and skills), and are served back to agents on demand.

**Memory ontology — who owns what:**

- ``UnifiedMemoryStore`` (this module): Long-term storage, search, fact
  graph, episode recording, Ebbinghaus decay eviction, and episodic →
  semantic promotion.  Single SQLite connection behind an RLock.
- ``SharedMemory`` (``shared.py``): Facade that unifies access to
  UnifiedMemoryStore, PlanTracking, and Blackboard behind one API so
  agent code never imports storage internals directly.
- ``EpisodeMemory`` (``_store_episode.py``): Insert/recall/evict helpers
  for the ``memory_episodes`` table — episodic records of task executions.
- ``Blackboard`` (``blackboard.py``): Pub/sub message board for inter-agent
  coordination.  Short-lived messages, not persistent knowledge.
- ``SessionContext`` (``session_context.py``): LRU cache of recent entries
  for the current session, promoted to long-term on consolidation.

Sub-modules: ``unified_core`` (CRUD), ``unified_search`` (query routing),
``unified_embeddings`` (semantic search), ``unified_episodes`` (episode APIs),
``unified_consolidation`` (promotion), ``_schema`` (DDL), and lower-level
``_store_*`` SQL helpers.

Decision: Ebbinghaus decay over simple temporal decay (ADR-0092).
Decision: Structured fact relationships with chain-aware search (ADR-0092).
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from vetinari.database import get_connection
from vetinari.types import MemoryType

from ._schema import create_schema, create_vec_tables
from .episode_recorder import RecordedEpisode
from .interfaces import MemoryEntry, MemoryStats
from .memory_embeddings import embed_via_local_inference as _embed_via_local_inference
from .memory_embeddings import load_sqlite_vec as _load_sqlite_vec
from .memory_embeddings import pack_embedding as _pack_embedding
from .memory_embeddings import sqlite_vec_available as _sqlite_vec_available
from .memory_embeddings import unpack_embedding as _memory_unpack_embedding
from .session_context import SessionContext
from .unified_config import (
    CONSOLIDATION_QUALITY_THRESHOLD,
    EMBEDDING_API_URL,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EPISODE_PROMOTION_THRESHOLD,
    MAX_LONG_TERM_ENTRIES,
    SEMANTIC_DEDUP_THRESHOLD,
    SESSION_MAX_ENTRIES,
)
from .unified_consolidation import _ConsolidationMixin
from .unified_core import _CoreStoreMixin
from .unified_embeddings import _EmbeddingMixin
from .unified_episodes import _EpisodeMixin
from .unified_search import _SearchMixin

logger = logging.getLogger(__name__)


BOUNDARY_ADR = "ADR-0132"
CANONICAL_BOUNDARY = "memory.long_term"
_unpack_embedding = _memory_unpack_embedding

__all__ = [
    "BOUNDARY_ADR",
    "CANONICAL_BOUNDARY",
    "CONSOLIDATION_QUALITY_THRESHOLD",
    "EMBEDDING_API_URL",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_MODEL",
    "EPISODE_PROMOTION_THRESHOLD",
    "MAX_LONG_TERM_ENTRIES",
    "SEMANTIC_DEDUP_THRESHOLD",
    "SESSION_MAX_ENTRIES",
    "MemoryEntry",
    "MemoryStats",
    "MemoryType",
    "RecordedEpisode",
    "SessionContext",
    "UnifiedMemoryStore",
    "_embed_via_local_inference",
    "_pack_embedding",
    "_unpack_embedding",
    "get_unified_memory_store",
    "get_unified_store",
    "init_unified_memory_store",
]


class UnifiedMemoryStore(
    _CoreStoreMixin,
    _SearchMixin,
    _EmbeddingMixin,
    _EpisodeMixin,
    _ConsolidationMixin,
):
    """Single SQLite + FTS5 memory backend.

    Owns the DB connection, RLock, and config; all SQL/business logic is
    in the split sub-modules.  The public API is unchanged.
    """

    _ask_deprecation_warned: bool = False

    def __init__(
        self,
        db_path: str | None = None,
        embedding_api_url: str = EMBEDDING_API_URL,
        embedding_model: str = EMBEDDING_MODEL,
        max_entries: int = MAX_LONG_TERM_ENTRIES,
        dedup_threshold: float = SEMANTIC_DEDUP_THRESHOLD,
        session_max: int = SESSION_MAX_ENTRIES,
    ) -> None:
        """Open the store, initialise the schema, and probe for sqlite-vec."""
        self._embedding_api_url = embedding_api_url
        self._embedding_model = embedding_model
        self._max_entries = max_entries
        self._dedup_threshold = dedup_threshold
        self._lock = threading.RLock()
        self.session = SessionContext(max_entries=session_max)
        self._has_vec = False

        if db_path is not None:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
            self._private_conn: sqlite3.Connection | None = sqlite3.connect(db_path, check_same_thread=False)
            self._private_conn.row_factory = sqlite3.Row
            self._private_conn.execute("PRAGMA journal_mode=WAL")
            self._owns_connection = True
            create_schema(self._private_conn)
        else:
            self._private_conn = None
            self._owns_connection = False

        self._try_load_sqlite_vec()
        logger.info("UnifiedMemoryStore initialized (sqlite_vec=%s)", self._has_vec)

    @property
    def _conn(self) -> sqlite3.Connection | None:
        """Active connection: private when store owns one, shared thread-local otherwise."""
        if self._owns_connection:
            return self._private_conn
        return get_connection()

    def _try_load_sqlite_vec(self) -> None:
        """Load sqlite-vec and create KNN virtual tables; sets ``_has_vec``."""
        if not (_sqlite_vec_available() and _load_sqlite_vec(self._conn)):
            return
        self._has_vec = create_vec_tables(self._conn, EMBEDDING_DIMENSIONS)

    def ask(self, question: str, agent: str | None = None) -> list[MemoryEntry]:
        """Deprecated — use :meth:`query` instead.

        Delegates to ``query()`` and emits a one-time deprecation warning.

        Args:
            question: Natural language question.
            agent: Optional agent-name filter.

        Returns:
            Results from :meth:`query`.
        """
        if not UnifiedMemoryStore._ask_deprecation_warned:
            logger.warning(
                "ask() is deprecated — use query() for intent-aware dispatch. Falling back to query() automatically."
            )
            UnifiedMemoryStore._ask_deprecation_warned = True
        return self.query(question, agent=agent)

    def close(self) -> None:
        """Close the private connection (no-op when using the shared connection)."""
        if getattr(self, "_private_conn", None) is not None and getattr(self, "_owns_connection", False):
            with contextlib.suppress(sqlite3.Error):
                self._private_conn.close()
            self._private_conn = None

    def check_health(self) -> dict[str, Any]:
        """Return a fail-closed health payload for the memory store.

        Returns:
            Mapping with health status, reason on failure, and sqlite-vec
            availability on success.
        """
        try:
            conn = self._conn
            if conn is None:
                return {"ok": False, "status": "unavailable", "reason": "database connection is not available"}
            conn.execute("SELECT 1").fetchone()
        except Exception as exc:
            logger.warning("Memory health check failed: %s", exc)
            return {"ok": False, "status": "unavailable", "reason": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "status": "healthy", "sqlite_vec": bool(self._has_vec)}

    def __enter__(self) -> UnifiedMemoryStore:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        self.close()
        return False

    def __del__(self) -> None:
        self.close()


_unified_store: UnifiedMemoryStore | None = None
_store_lock = threading.Lock()


def get_unified_memory_store() -> UnifiedMemoryStore:
    """Get or create the global UnifiedMemoryStore singleton (double-checked locking).

    Returns:
        The process-wide UnifiedMemoryStore instance, initialised on first call.
    """
    global _unified_store
    if _unified_store is None:
        with _store_lock:
            if _unified_store is None:
                _unified_store = UnifiedMemoryStore()
    return _unified_store


# Alias used by vetinari.training.idle_scheduler
get_unified_store = get_unified_memory_store


def init_unified_memory_store(**kwargs: Any) -> UnifiedMemoryStore:
    """Replace the global singleton with a freshly constructed store.

    Returns:
        The newly created UnifiedMemoryStore that is now the active singleton.
    """
    global _unified_store
    with _store_lock:
        if _unified_store is not None:
            _unified_store.close()
        _unified_store = UnifiedMemoryStore(**kwargs)
    return _unified_store
