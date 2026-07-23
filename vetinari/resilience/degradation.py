"""Graceful degradation matrix — per-subsystem fallback chains.

When a subsystem fails, the degradation manager determines the next
available fallback level and provides a user-facing message explaining
what changed.  The fallback stack for inference is:

  primary model -> smaller quant -> tiny model -> cached responses -> unavailable

Other subsystems (model selection, learning, persistence) have their own
fallback chains appropriate to their failure modes.

Pipeline role: consulted by the resilience layer when a circuit breaker
trips or a subsystem reports a failure.  Provides the fallback action
and user-facing degradation message.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class DegradationLevel(Enum):
    """Ordered degradation levels from best to worst service quality."""

    PRIMARY = "primary"  # Full capability — preferred model/subsystem
    REDUCED = "reduced"  # Smaller or slower model — still functional
    MINIMAL = "minimal"  # Tiny model or minimal functionality
    CACHED = "cached"  # Serving from cache — no new inference
    UNAVAILABLE = "unavailable"  # Cannot serve — honest "can't help right now"


# Ordered from best to worst for comparison
_LEVEL_ORDER: list[DegradationLevel] = [
    DegradationLevel.PRIMARY,
    DegradationLevel.REDUCED,
    DegradationLevel.MINIMAL,
    DegradationLevel.CACHED,
    DegradationLevel.UNAVAILABLE,
]


@dataclass(frozen=True, slots=True)
class FallbackEntry:
    """A single fallback level within a subsystem's degradation chain.

    Attributes:
        level: The degradation level this entry represents.
        description: Internal description of what this fallback does.
        user_message: Human-readable message shown to the user when this level activates.
        is_available: Whether this fallback is currently usable (e.g., a smaller model is loaded).
    """

    level: DegradationLevel
    description: str
    user_message: str
    is_available: bool = True

    def __repr__(self) -> str:
        return (
            f"FallbackEntry(level={self.level.value!r}, is_available={self.is_available}, "
            f"description={self.description!r})"
        )


@dataclass
class SubsystemFallback:
    """Degradation chain for a single subsystem.

    Attributes:
        subsystem: Name of the subsystem (e.g., ``"inference"``, ``"model_selection"``).
        current_level: The level the subsystem is currently operating at.
        chain: Ordered list of fallback entries from PRIMARY to UNAVAILABLE.
    """

    subsystem: str
    current_level: DegradationLevel = DegradationLevel.PRIMARY
    chain: list[FallbackEntry] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"SubsystemFallback(subsystem={self.subsystem!r},"
            f" current_level={self.current_level!r},"
            f" chain_len={len(self.chain)})"
        )


# ── Default degradation matrices ─────────────────────────────────────────────


def _build_inference_chain() -> list[FallbackEntry]:
    """Build the default degradation chain for the inference subsystem."""
    return [
        FallbackEntry(
            level=DegradationLevel.PRIMARY,
            description="Use the preferred model at full capability",
            user_message="",  # No message needed at primary level
        ),
        FallbackEntry(
            level=DegradationLevel.REDUCED,
            description="Switch to a smaller quantization or secondary model",
            user_message="Using a smaller model — responses may be less detailed",
        ),
        FallbackEntry(
            level=DegradationLevel.MINIMAL,
            description="Fall back to the smallest available model",
            user_message="Using a minimal model — quality will be reduced but basic requests still work",
        ),
        FallbackEntry(
            level=DegradationLevel.CACHED,
            description="Serve from response cache when available",
            user_message="Serving from cache — this response may not reflect your exact request",
        ),
        FallbackEntry(
            level=DegradationLevel.UNAVAILABLE,
            description="No model available for inference",
            user_message="No model is available right now — please try again later",
        ),
    ]


def _build_model_selection_chain() -> list[FallbackEntry]:
    """Build the default degradation chain for model selection."""
    return [
        FallbackEntry(
            level=DegradationLevel.PRIMARY,
            description="Full Thompson sampling + Ponder scoring",
            user_message="",
        ),
        FallbackEntry(
            level=DegradationLevel.REDUCED,
            description="Rule-based selection without Thompson data",
            user_message="Model selection is running in simplified mode",
        ),
        FallbackEntry(
            level=DegradationLevel.MINIMAL,
            description="Use the last successfully loaded model",
            user_message="Using the default model — smart selection is temporarily unavailable",
        ),
        FallbackEntry(
            level=DegradationLevel.UNAVAILABLE,
            description="No models discovered or loadable",
            user_message="No models available — please check your model directory",
        ),
    ]


def _build_learning_chain() -> list[FallbackEntry]:
    """Build the default degradation chain for the learning subsystem."""
    return [
        FallbackEntry(
            level=DegradationLevel.PRIMARY,
            description="Full feedback loop with Thompson updates and training data collection",
            user_message="",
        ),
        FallbackEntry(
            level=DegradationLevel.REDUCED,
            description="Collect training data but skip Thompson updates",
            user_message="",  # Invisible to user — learning degrades silently
        ),
        FallbackEntry(
            level=DegradationLevel.MINIMAL,
            description="Skip all learning — operate on existing data only",
            user_message="",  # Invisible to user
        ),
        FallbackEntry(
            level=DegradationLevel.UNAVAILABLE,
            description="Learning subsystem is offline",
            user_message="",  # Invisible to user — inference continues without learning
        ),
    ]


def _build_persistence_chain() -> list[FallbackEntry]:
    """Build the default degradation chain for the persistence layer."""
    return [
        FallbackEntry(
            level=DegradationLevel.PRIMARY,
            description="Full persistence to disk (JSONL, SQLite, JSON)",
            user_message="",
        ),
        FallbackEntry(
            level=DegradationLevel.REDUCED,
            description="Write to in-memory buffer, flush when disk available",
            user_message="Data may not be saved permanently until disk space is freed",
        ),
        FallbackEntry(
            level=DegradationLevel.MINIMAL,
            description="Log critical data only, skip training and analytics",
            user_message="Running with limited data persistence — some history may be lost",
        ),
        FallbackEntry(
            level=DegradationLevel.UNAVAILABLE,
            description="Cannot write to any persistent store",
            user_message="Unable to save data — results will be lost when the session ends",
        ),
    ]


def _build_engine_chain() -> list[FallbackEntry]:
    """Build the fail-closed AM Engine lifecycle degradation chain."""
    return [
        FallbackEntry(
            level=DegradationLevel.PRIMARY,
            description="Pinned AM Engine is healthy and ready",
            user_message="",
        ),
        FallbackEntry(
            level=DegradationLevel.REDUCED,
            description="AM Engine is restarting within the bounded retry policy",
            user_message="The AM Engine is restarting; inference is temporarily unavailable",
        ),
        FallbackEntry(
            level=DegradationLevel.UNAVAILABLE,
            description="AM Engine is unavailable or its version does not match the trusted pin",
            user_message=(
                "The AM Engine is unavailable; verify or reinstall the pinned engine release before retrying"
            ),
        ),
    ]


# ── DegradationManager ──────────────────────────────────────────────────────


class DegradationManager:
    """Manages per-subsystem degradation chains and level transitions.

    Provides ``get_fallback()`` to determine the next available fallback
    when a subsystem fails, and ``report_recovery()`` to restore a
    subsystem to a higher level when conditions improve.

    Thread-safe: all state mutations are protected by a lock.

    Side effects:
        - Initializes default degradation chains for known subsystems on creation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subsystems: dict[str, SubsystemFallback] = {}
        self._initialize_defaults()

    def _initialize_defaults(self) -> None:
        """Register default degradation chains for all known subsystems."""
        defaults: dict[str, list[FallbackEntry]] = {
            "inference": _build_inference_chain(),
            "model_selection": _build_model_selection_chain(),
            "learning": _build_learning_chain(),
            "persistence": _build_persistence_chain(),
            "engine": _build_engine_chain(),
        }
        for name, chain in defaults.items():
            self._subsystems[name] = SubsystemFallback(
                subsystem=name,
                current_level=DegradationLevel.PRIMARY,
                chain=chain,
            )

    def get_fallback(self, subsystem: str) -> FallbackEntry | None:
        """Get the next available fallback for a subsystem.

        Moves the subsystem one level down from its current level and
        returns the new fallback entry.  If already at UNAVAILABLE,
        returns the UNAVAILABLE entry.

        Args:
            subsystem: The subsystem name (e.g., ``"inference"``).

        Returns:
            The next FallbackEntry, or None if the subsystem is unknown.
        """
        with self._lock:
            sub = self._subsystems.get(subsystem)
            if sub is None:
                logger.warning(
                    "Unknown subsystem %s — no degradation chain registered",
                    subsystem,
                )
                return None

            current_idx = _LEVEL_ORDER.index(sub.current_level)

            # Find the next available level below current
            for level in _LEVEL_ORDER[current_idx + 1 :]:
                entry = self._find_entry(sub, level)
                if entry and entry.is_available:
                    sub.current_level = level
                    logger.warning(
                        "Subsystem %s degraded to %s: %s",
                        subsystem,
                        level.value,
                        entry.description,
                    )
                    return entry

            # Already at or past lowest — return UNAVAILABLE
            unavailable = self._find_entry(sub, DegradationLevel.UNAVAILABLE)
            if unavailable:
                sub.current_level = DegradationLevel.UNAVAILABLE
                return unavailable

            return None

    def get_current_level(self, subsystem: str) -> DegradationLevel | None:
        """Return the current degradation level for a subsystem.

        Args:
            subsystem: The subsystem name.

        Returns:
            Current DegradationLevel, or None if subsystem is unknown.
        """
        with self._lock:
            sub = self._subsystems.get(subsystem)
            return sub.current_level if sub else None

    def get_current_user_message(self, subsystem: str) -> str:
        """Return the user-facing message for the subsystem's current degradation level.

        Args:
            subsystem: The subsystem name.

        Returns:
            User message string, or empty string if at PRIMARY or subsystem unknown.
        """
        with self._lock:
            sub = self._subsystems.get(subsystem)
            if sub is None:
                return ""
            entry = self._find_entry(sub, sub.current_level)
            return entry.user_message if entry else ""

    def report_recovery(self, subsystem: str) -> DegradationLevel | None:
        """Signal that a subsystem has recovered — move back to PRIMARY if it is available.

        Checks that the PRIMARY FallbackEntry is marked ``is_available`` before
        restoring.  If PRIMARY is still unavailable (e.g. the model has not
        finished loading), the subsystem stays at its current level so callers
        continue to use the active fallback rather than silently reverting to a
        broken primary.

        Args:
            subsystem: The subsystem that recovered.

        Returns:
            The new level after the recovery attempt, or None if subsystem is unknown.
        """
        with self._lock:
            sub = self._subsystems.get(subsystem)
            if sub is None:
                return None

            primary_entry = self._find_entry(sub, DegradationLevel.PRIMARY)
            if primary_entry is not None and not primary_entry.is_available:
                logger.warning(
                    "Cannot restore %s to PRIMARY — primary is still unavailable; staying at %s",
                    subsystem,
                    sub.current_level.value,
                )
                return sub.current_level

            old_level = sub.current_level
            sub.current_level = DegradationLevel.PRIMARY
            if old_level != DegradationLevel.PRIMARY:
                logger.info(
                    "Subsystem %s recovered from %s to PRIMARY",
                    subsystem,
                    old_level.value,
                )
            return sub.current_level

    def set_availability(
        self,
        subsystem: str,
        level: DegradationLevel,
        *,
        is_available: bool,
    ) -> None:
        """Mark a fallback level as available or unavailable.

        Use this when a fallback option becomes viable (e.g., a smaller
        model was loaded) or is no longer viable (e.g., cache expired).

        Args:
            subsystem: The subsystem name.
            level: The degradation level to update.
            is_available: Whether this level is currently usable.
        """
        with self._lock:
            sub = self._subsystems.get(subsystem)
            if sub is None:
                return
            for index, entry in enumerate(sub.chain):
                if entry.level != level:
                    continue
                sub.chain[index] = FallbackEntry(
                    level=entry.level,
                    description=entry.description,
                    user_message=entry.user_message,
                    is_available=is_available,
                )
                logger.debug(
                    "Subsystem %s level %s availability set to %s",
                    subsystem,
                    level.value,
                    is_available,
                )
                break

    def get_status(self) -> dict[str, Any]:
        """Return a dashboard-friendly summary of all subsystem degradation states.

        Returns:
            Dict mapping subsystem name to its current level and available fallbacks.
        """
        with self._lock:
            return {
                name: {
                    "current_level": sub.current_level.value,
                    "available_levels": [e.level.value for e in sub.chain if e.is_available],
                    "user_message": (
                        current_entry.user_message
                        if (current_entry := self._find_entry(sub, sub.current_level)) is not None
                        else ""
                    ),
                }
                for name, sub in self._subsystems.items()
            }

    @staticmethod
    def _find_entry(
        sub: SubsystemFallback,
        level: DegradationLevel,
    ) -> FallbackEntry | None:
        """Find the FallbackEntry for a given level in a subsystem's chain."""
        for entry in sub.chain:
            if entry.level == level:
                return entry
        return None


# ── Singleton ────────────────────────────────────────────────────────────────

_manager: DegradationManager | None = None
_manager_lock = threading.Lock()


def get_degradation_manager() -> DegradationManager:
    """Return the process-wide DegradationManager singleton.

    Uses double-checked locking so the common read-path never acquires the lock.

    Returns:
        The singleton DegradationManager instance.
    """
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = DegradationManager()
    return _manager


def reset_degradation_manager() -> None:
    """Reset the singleton for test isolation."""
    global _manager
    with _manager_lock:
        _manager = None
