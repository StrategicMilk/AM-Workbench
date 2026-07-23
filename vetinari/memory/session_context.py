"""Short-term session memory with bounded LRU eviction.

Provides an in-memory key-value store for plan-scoped state, replacing
Blackboard's SharedExecutionContext for within-session data sharing.
Entries are evicted least-recently-used when the capacity bound is exceeded.

This is a supporting subsystem for UnifiedMemoryStore — it manages only
the volatile, within-session tier; durable storage lives in SQLite.
"""

from __future__ import annotations

import heapq
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any

from vetinari.exceptions import StorageError

logger = logging.getLogger(__name__)


# Maximum entries before LRU eviction kicks in (overridable per-instance).
_DEFAULT_SESSION_MAX_ENTRIES = 100  # matches VETINARI_SESSION_MAX_ENTRIES default


# ── SessionEntry ───────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionEntry:
    """A single entry in short-term session memory."""

    key: str
    value: Any
    timestamp: float = field(default_factory=time.time)
    quality_score: float = 0.0
    access_count: int = 0

    def __repr__(self) -> str:
        return (
            f"SessionEntry(key={self.key!r}, quality_score={self.quality_score!r}, access_count={self.access_count!r})"
        )


# ── SessionContext ─────────────────────────────────────────────────────────


class SessionContext:
    """Short-term in-memory session store with bounded LRU eviction.

    Replaces Blackboard's SharedExecutionContext for plan-scoped state.
    Entries are evicted LRU when the bound is exceeded.  All operations
    are thread-safe via a reentrant lock.

    Injection markers and oversized string values are rejected before
    storage to prevent memory poisoning attacks.
    """

    # Injection markers that should never appear in stored memory values.
    _INJECTION_MARKERS: tuple[str, ...] = (
        "{{",
        "}}",
        "<%",
        "%>",
        "<script",
        "</script",
        "javascript:",
        "data:text/html",
    )
    # Maximum allowed byte size for a single string value (100 KB).
    _MAX_VALUE_BYTES: int = 100 * 1024

    def __init__(self, max_entries: int = _DEFAULT_SESSION_MAX_ENTRIES) -> None:
        """Initialise the session store with an entry capacity bound.

        Args:
            max_entries: Maximum number of entries before LRU eviction begins.
        """
        self._max_entries = max_entries
        self._store: OrderedDict[str, SessionEntry] = OrderedDict()
        self._lock = threading.RLock()

    def _sanitize_value(self, value: Any) -> Any:
        """Strip injection markers and enforce a size limit on string values.

        Non-string values are returned unchanged.  If the string value
        exceeds ``_MAX_VALUE_BYTES`` it is rejected.  Any injection
        markers found are removed and a warning is logged.

        Args:
            value: The value to sanitize.

        Returns:
            The sanitized value.

        Raises:
            StorageError: If a string value exceeds the maximum allowed size.
        """
        if not isinstance(value, str):
            return value

        if len(value.encode("utf-8", errors="replace")) > self._MAX_VALUE_BYTES:
            raise StorageError(
                f"Memory value exceeds maximum size ({self._MAX_VALUE_BYTES} bytes); "
                "rejected to prevent memory poisoning"
            )

        sanitized = value
        for marker in self._INJECTION_MARKERS:
            if marker in sanitized:
                sanitized = sanitized.replace(marker, "")
                logger.warning(
                    "Memory poisoning attempt detected: stripped injection marker %r from key store",
                    marker,
                )

        return sanitized

    def set_context(self, key: str, value: Any, quality_score: float = 0.0) -> None:
        """Set a key-value pair in the session context.

        Sanitizes string values before storage: strips known injection
        markers and rejects values that exceed the 100 KB size limit.
        Moves the key to the most-recently-used position on update.

        Args:
            key: The context key.
            value: The context value.
            quality_score: Quality score for consolidation eligibility.
        """
        value = self._sanitize_value(value)
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                entry = self._store[key]
                self._store[key] = replace(entry, value=value, timestamp=time.time(), quality_score=quality_score)
            else:
                self._store[key] = SessionEntry(key=key, value=value, quality_score=quality_score)
            # Evict oldest entry if over limit
            while len(self._store) > self._max_entries:
                self._store.popitem(last=False)

    def get_context(self, key: str, default: Any = None) -> Any:
        """Get a context value by key, promoting it to most-recently-used.

        Args:
            key: The context key.
            default: Default value if key not found.

        Returns:
            The stored value or default.
        """
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return default
            self._store[key] = replace(entry, access_count=entry.access_count + 1)
            self._store.move_to_end(key)
            return entry.value

    def add(self, entry: Any, quality_score: float = 0.0) -> None:
        """Add a MemoryEntry to session context (keyed by entry.id).

        Args:
            entry: The memory entry to add (must have ``id`` and ``to_dict()``).
            quality_score: Quality score for consolidation eligibility.
        """
        self.set_context(entry.id, entry.to_dict(), quality_score=quality_score)

    def get_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Get the most recent session entries.

        Uses heapq.nlargest for O(n log k) performance where k=limit,
        significantly faster than sorting the entire store when k << n.

        Args:
            limit: Maximum entries to return.

        Returns:
            List of entry dicts, most recent first.
        """
        with self._lock:
            entries = list(self._store.values())
        # heapq.nlargest is O(n log k) vs O(n log n) for full sort
        recent = heapq.nlargest(limit, entries, key=lambda e: e.timestamp)
        return [{"key": e.key, "value": e.value, "quality_score": e.quality_score} for e in recent]

    def get_all(self) -> list[SessionEntry]:
        """Get all session entries for consolidation.

        Returns:
            List of all session entries.
        """
        with self._lock:
            return list(self._store.values())

    def clear(self) -> None:
        """Clear all session entries."""
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
