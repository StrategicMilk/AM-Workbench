"""SharedMemory facade — single entry point for all memory subsystems.

Provides a unified API that delegates to :class:`UnifiedMemoryStore`,
:class:`PlanTracking`, and :class:`Blackboard` via lazy properties so that
importing this module never triggers heavy I/O or circular imports.

Decision: SharedMemory replaces direct subsystem calls scattered across
agent code (ADR-0077).

Usage::

    from vetinari.memory.shared import get_shared_memory

    mem = get_shared_memory()
    mem.remember(entry)            # delegates to UnifiedMemoryStore
    results = mem.search(query)    # delegates to UnifiedMemoryStore
    mem.write_plan_history(...)    # delegates to PlanTracking
    board = mem.blackboard          # the Blackboard singleton
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.memory.interfaces import MemoryEntry


class SharedMemory:
    """Lazy-loading facade for Vetinari's memory subsystems.

    Each subsystem is loaded on first access via ``@property`` to avoid
    circular imports and startup I/O.
    """

    def __init__(self) -> None:
        self._unified_store: Any = None
        self._memory_search: Any = None
        self._plan_tracking: Any = None
        self._blackboard: Any = None

    # ── UnifiedMemoryStore ────────────────────────────────────────────────

    @property
    def store(self) -> Any:
        """Lazy-load the UnifiedMemoryStore singleton."""
        if self._unified_store is None:
            from vetinari.memory.unified import get_unified_memory_store

            self._unified_store = get_unified_memory_store()
        return self._unified_store

    @property
    def searcher(self) -> Any:
        """Lazy-load the MemorySearch instance."""
        if self._memory_search is None:
            from vetinari.memory.memory_search import MemorySearch

            self._memory_search = MemorySearch()
        return self._memory_search

    def remember(self, entry: MemoryEntry) -> str:
        """Store a memory entry via UnifiedMemoryStore.

        Delegates to :meth:`UnifiedMemoryStore.remember` so that provenance
        tracking, deduplication, and hash-chain tamper detection are applied.

        Args:
            entry: The :class:`~vetinari.memory.interfaces.MemoryEntry` to store.

        Returns:
            The stored entry's ID.
        """
        return self.store.remember(entry)

    def search(
        self,
        query: str,
        *,
        scope: str = "global",
        mode: str = "hybrid",
        agent_type: str | None = None,
        limit: int = 10,
        use_semantic: bool = True,
    ) -> list[Any]:
        """Search memories using scope-aware hybrid retrieval.

        Args:
            query: Natural language query string.
            scope: Scope for inheritance-aware filtering (e.g. ``"task:abc"``).
            mode: Search mode — ``"hybrid"``, ``"semantic"``, or ``"fts"``.
            agent_type: Optional agent-type filter.
            limit: Maximum results to return.
            use_semantic: When False, forces FTS-only search regardless of
                the *mode* argument (for backward compatibility).

        Returns:
            List of :class:`~vetinari.memory.interfaces.MemoryEntry` objects.
        """
        effective_mode = "fts" if not use_semantic else mode
        return self.searcher.search(
            query,
            scope=scope,
            mode=effective_mode,
            agent_type=agent_type,
            limit=limit,
        )

    # ── Fact graph ────────────────────────────────────────────────────────

    def fact_graph(self, entry_id: str) -> list[Any]:
        """Walk the supersession chain for *entry_id* via UnifiedMemoryStore.

        Args:
            entry_id: Starting entry ID (typically the newest revision).

        Returns:
            Ordered list of MemoryEntry from newest to oldest in the chain.
        """
        return self.store.fact_graph(entry_id)

    # ── PlanTracking ─────────────────────────────────────────────────────

    @property
    def plan_tracking(self) -> Any:
        """Lazy-load the PlanTracking MemoryStore."""
        if self._plan_tracking is None:
            from vetinari.memory.plan_tracking import get_memory_store

            self._plan_tracking = get_memory_store()
        return self._plan_tracking

    def write_plan_history(self, plan_id: str, plan_data: dict[str, Any]) -> None:
        """Record a completed plan in plan history.

        Args:
            plan_id: Unique plan identifier.
            plan_data: Plan data dict (status, tasks, timestamps etc.).
        """
        try:
            store = self.plan_tracking
            if hasattr(store, "write_plan_history"):
                # MemoryStore.write_plan_history takes a single plan_data dict;
                # plan_id is embedded inside that dict under the "plan_id" key.
                data = {**plan_data, "plan_id": plan_id}
                store.write_plan_history(data)
        except Exception as exc:
            logger.warning("write_plan_history failed for plan %s (non-fatal): %s", plan_id, exc)

    def write_subtask_memory(self, subtask_id: str, subtask_data: dict[str, Any]) -> None:
        """Record a completed subtask in plan history.

        Args:
            subtask_id: Unique subtask identifier.
            subtask_data: Subtask data dict (result, agent, timestamps etc.).
        """
        try:
            store = self.plan_tracking
            if hasattr(store, "write_subtask_memory"):
                # MemoryStore.write_subtask_memory takes a single subtask_data dict;
                # subtask_id is embedded inside that dict under the "subtask_id" key.
                data = {**subtask_data, "subtask_id": subtask_id}
                store.write_subtask_memory(data)
        except Exception as exc:
            logger.warning("write_subtask_memory failed for subtask %s (non-fatal): %s", subtask_id, exc)

    # ── Blackboard ────────────────────────────────────────────────────────

    @property
    def blackboard(self) -> Any:
        """Lazy-load the Blackboard singleton."""
        if self._blackboard is None:
            from vetinari.memory.blackboard import get_blackboard

            self._blackboard = get_blackboard()
        return self._blackboard


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_shared_memory: SharedMemory | None = None
_shared_memory_lock = threading.Lock()


def get_shared_memory() -> SharedMemory:
    """Return the process-wide SharedMemory singleton (thread-safe).

    Uses double-checked locking to ensure the facade is initialised at
    most once across concurrent agent threads.

    Returns:
        The shared :class:`SharedMemory` facade instance.
    """
    global _shared_memory
    if _shared_memory is None:
        with _shared_memory_lock:
            if _shared_memory is None:
                _shared_memory = SharedMemory()
    return _shared_memory
