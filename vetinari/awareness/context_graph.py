"""Context graph — live situational model for pipeline decisions.

Maintains four quadrants of awareness that inform every significant
pipeline decision: SELF (system capabilities and performance), ENVIRONMENT
(hardware, project, time patterns), USER (preferences learned from
implicit feedback), and RELATIONSHIPS (cross-domain pattern detection).

This is step 1 of the awareness layer: Confidence → **Context Graph** → Decision Journal.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.types import ContextQuadrant
from vetinari.utils.bounded_collections import BoundedList

logger = logging.getLogger(__name__)

_MAX_CONTEXT_ENTRIES_PER_QUADRANT = 1000


# -- Data types ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ContextEntry:
    """A single entry in a context quadrant.

    Args:
        key: Unique identifier within the quadrant (e.g. "vram_utilization").
        value: The current value (numeric, string, or structured).
        updated_at: ISO timestamp of the last update.
        source: What produced this entry (e.g. "model_pool", "implicit_feedback").
        confidence: How reliable this data point is (0.0-1.0).
    """

    key: str
    value: Any
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = ""
    confidence: float = 1.0

    def __repr__(self) -> str:
        return f"ContextEntry(key={self.key!r}, value={self.value!r}, confidence={self.confidence:.2f})"


@dataclass
class ContextSnapshot:
    """Point-in-time snapshot of all four quadrants.

    Returned by ``get_context()`` for read-only consumption by pipeline
    decision points. Immutable after creation.

    Args:
        self_context: SELF quadrant entries.
        environment: ENVIRONMENT quadrant entries.
        user: USER quadrant entries.
        relationships: RELATIONSHIPS quadrant entries.
        timestamp: When the snapshot was taken.
    """

    self_context: dict[str, ContextEntry] = field(default_factory=dict)
    environment: dict[str, ContextEntry] = field(default_factory=dict)
    user: dict[str, ContextEntry] = field(default_factory=dict)
    relationships: dict[str, ContextEntry] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def get(self, quadrant: ContextQuadrant, key: str) -> Any | None:
        """Look up a value from a specific quadrant.

        Args:
            quadrant: Which quadrant to query.
            key: The entry key within that quadrant.

        Returns:
            The entry value, or None if not found.
        """
        store = self._quadrant_store(quadrant)
        entry = store.get(key)
        return entry.value if entry is not None else None

    def _quadrant_store(self, quadrant: ContextQuadrant) -> dict[str, ContextEntry]:
        """Map a quadrant enum to the corresponding dict.

        Args:
            quadrant: The quadrant to look up.

        Returns:
            The dict for that quadrant.
        """
        mapping: dict[ContextQuadrant, dict[str, ContextEntry]] = {
            ContextQuadrant.SELF: self.self_context,
            ContextQuadrant.ENVIRONMENT: self.environment,
            ContextQuadrant.USER: self.user,
            ContextQuadrant.RELATIONSHIPS: self.relationships,
        }
        return mapping[quadrant]

    def __repr__(self) -> str:
        counts = (
            f"self={len(self.self_context)}, env={len(self.environment)}, "
            f"user={len(self.user)}, rel={len(self.relationships)}"
        )
        return f"ContextSnapshot({counts})"


# -- Context graph ------------------------------------------------------------


class ContextGraph:
    """Live situational model tracking what the system knows about itself.

    Covers its environment, the user, and cross-domain relationships.
    Thread-safe: all mutations go through a single lock. Reads return
    immutable snapshots so consumers never see partial updates.

    Side effects:
        - None on construction. Quadrants are populated by callers
          (pipeline stages, feedback collectors, hardware monitors).
    """

    _STALE_THRESHOLD_SECONDS = 3600  # Entries older than 1h are flagged stale

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._quadrants: dict[ContextQuadrant, dict[str, ContextEntry]] = {
            ContextQuadrant.SELF: {},
            ContextQuadrant.ENVIRONMENT: {},
            ContextQuadrant.USER: {},
            ContextQuadrant.RELATIONSHIPS: {},
        }

    def get_context(self, quadrants: list[ContextQuadrant] | None = None) -> ContextSnapshot:
        """Return a read-only snapshot of the context graph.

        Args:
            quadrants: Which quadrants to include. None means all four.

        Returns:
            Immutable ContextSnapshot with current data.
        """
        with self._lock:
            target = quadrants or list(ContextQuadrant)
            return ContextSnapshot(
                self_context=dict(self._quadrants[ContextQuadrant.SELF]) if ContextQuadrant.SELF in target else {},
                environment=dict(self._quadrants[ContextQuadrant.ENVIRONMENT])
                if ContextQuadrant.ENVIRONMENT in target
                else {},
                user=dict(self._quadrants[ContextQuadrant.USER]) if ContextQuadrant.USER in target else {},
                relationships=dict(self._quadrants[ContextQuadrant.RELATIONSHIPS])
                if ContextQuadrant.RELATIONSHIPS in target
                else {},
            )

    def update_self(self, key: str, value: Any, source: str = "", confidence: float = 1.0) -> None:
        """Update a SELF quadrant entry (models, VRAM, quality trends, weaknesses).

        Args:
            key: Entry identifier (e.g. "loaded_models", "vram_utilization").
            value: Current value.
            source: What produced this data point.
            confidence: Reliability of this data (0.0-1.0).
        """
        self._set(ContextQuadrant.SELF, key, value, source, confidence)

    def update_environment(self, key: str, value: Any, source: str = "", confidence: float = 1.0) -> None:
        """Update an ENVIRONMENT quadrant entry (hardware, project, time patterns).

        Args:
            key: Entry identifier (e.g. "gpu_model", "project_tech_stack").
            value: Current value.
            source: What produced this data point.
            confidence: Reliability of this data (0.0-1.0).
        """
        self._set(ContextQuadrant.ENVIRONMENT, key, value, source, confidence)

    def record_user_signal(self, key: str, value: Any, source: str = "", confidence: float = 1.0) -> None:
        """Record a USER quadrant signal (preferences, expertise, corrections).

        Args:
            key: Entry identifier (e.g. "prefers_verbose_docs", "expertise_level").
            value: Current value.
            source: What produced this signal (e.g. "implicit_feedback").
            confidence: Reliability of this data (0.0-1.0).
        """
        self._set(ContextQuadrant.USER, key, value, source, confidence)

    def record_relationship(self, key: str, value: Any, source: str = "", confidence: float = 1.0) -> None:
        """Record a RELATIONSHIPS quadrant entry (cross-domain patterns, causal chains).

        Args:
            key: Pattern identifier (e.g. "quality_drop_after_model_switch").
            value: Pattern description or structured data.
            source: What detected this pattern.
            confidence: Reliability of this observation (0.0-1.0).
        """
        self._set(ContextQuadrant.RELATIONSHIPS, key, value, source, confidence)

    def get_stale_entries(self) -> list[tuple[ContextQuadrant, str, float]]:
        """Find entries that haven't been updated within the staleness threshold.

        Returns:
            List of (quadrant, key, age_seconds) tuples for stale entries.
        """
        now = time.time()
        stale = BoundedList[tuple[ContextQuadrant, str, float]](
            len(self._quadrants) * _MAX_CONTEXT_ENTRIES_PER_QUADRANT
        )
        with self._lock:
            for quadrant, entries in self._quadrants.items():
                for key, entry in entries.items():
                    try:
                        entry_dt = datetime.fromisoformat(entry.updated_at)
                        if entry_dt.tzinfo is None:
                            entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                        age = now - entry_dt.timestamp()
                        if age > self._STALE_THRESHOLD_SECONDS:
                            stale.append((quadrant, key, age))
                    except (ValueError, TypeError):
                        stale.append((quadrant, key, float("inf")))
        return list(stale)

    def _set(self, quadrant: ContextQuadrant, key: str, value: Any, source: str, confidence: float) -> None:
        """Internal: set or update an entry in the specified quadrant.

        Args:
            quadrant: Target quadrant.
            key: Entry key.
            value: Entry value.
            source: Data source identifier.
            confidence: Reliability score (0.0-1.0).
        """
        entry = ContextEntry(
            key=key,
            value=value,
            source=source,
            confidence=max(0.0, min(1.0, confidence)),
        )
        with self._lock:
            entries = self._quadrants[quadrant]
            entries[key] = entry
            while len(entries) > _MAX_CONTEXT_ENTRIES_PER_QUADRANT:
                oldest_key = min(entries, key=lambda item: entries[item].updated_at)
                del entries[oldest_key]
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Context graph updated: %s.%s = %r (confidence=%.2f, source=%s)",
                quadrant.value,
                key,
                value,
                confidence,
                source,
            )


# -- Singleton ----------------------------------------------------------------

_instance: ContextGraph | None = None
_lock = threading.Lock()


def get_context_graph() -> ContextGraph:
    """Get or create the global ContextGraph singleton.

    Returns:
        The singleton ContextGraph instance.
    """
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = ContextGraph()
    return _instance


def reset_context_graph() -> None:
    """Reset the singleton for testing.

    Returns:
        None.
    """
    global _instance
    with _lock:
        _instance = None
