"""Andon System — stop-the-line alerts for critical failures.

Provides :class:`AndonSignal` for individual alerts, :class:`NelsonViolation`
for SPC rule violations, and :class:`AndonSystem` which manages pausing and
resuming execution when critical/emergency conditions are detected.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class AndonSignal:
    """Stop-the-line alert for critical failures."""

    source: str  # Which gate / stage triggered it
    severity: str  # "warning", "critical", "emergency"
    message: str
    affected_tasks: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    acknowledged: bool = False
    scope: str | None = None  # None = global signal; set to scope name for scoped signals

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"AndonSignal(source={self.source!r}, severity={self.severity!r}, "
            f"acknowledged={self.acknowledged!r}, scope={self.scope!r})"
        )


@dataclass(frozen=True, slots=True)
class NelsonViolation:
    """A violation of one of the eight Nelson SPC rules.

    Attributes:
        rule: The Nelson rule number (1-8).
        severity: One of "critical", "warning", or "info".
        description: Human-readable description of the violation.
    """

    rule: int  # 1-8
    severity: str  # "critical", "warning", "info"
    description: str


class AndonSystem:
    """Manages stop-the-line alerts.

    When a *critical* or *emergency* signal is raised the system enters
    a **paused** state.  Execution should not proceed until every
    critical/emergency signal has been explicitly acknowledged.
    """

    PAUSE_SEVERITIES = frozenset({"critical", "emergency"})

    def __init__(self) -> None:
        # Side effects: none — no callbacks, threads, or global state touched here.
        self._signals: deque[AndonSignal] = deque(maxlen=200)
        self._paused: bool = False
        self._callbacks: list[Callable[[AndonSignal], None]] = []

        # Protects _callbacks list for concurrent register/deregister/raise_signal.
        self._callback_lock = threading.Lock()

        # Scoped pause state: scope_name -> AndonSignal that caused the pause.
        # Protected by _scope_lock for thread-safe reads and writes.
        self._paused_scopes: dict[str, AndonSignal] = {}
        self._scope_lock = threading.Lock()

    # -- public API ---------------------------------------------------------

    def register_callback(self, callback: Callable[[AndonSignal], None]) -> None:
        """Register a callback to be invoked whenever a signal is raised.

        The callback receives the newly created :class:`AndonSignal` as its
        only argument.  If the callback raises an exception it is logged and
        suppressed so the Andon system continues operating normally.

        Caller owns the callback lifecycle — use :meth:`deregister_callback`
        to remove a callback when the owning object is being torn down.

        Args:
            callback: A callable that accepts a single :class:`AndonSignal`.
        """
        with self._callback_lock:
            self._callbacks.append(callback)

    def deregister_callback(self, callback: Callable[[AndonSignal], None]) -> bool:
        """Remove a previously registered callback.

        Returns ``True`` if the callback was found and removed, ``False`` if
        it was not registered (does not raise in either case).  If the same
        callback was registered multiple times, only the first occurrence is
        removed per call.

        Args:
            callback: The callable to remove.

        Returns:
            True if the callback was present and removed; False otherwise.
        """
        with self._callback_lock:
            try:
                self._callbacks.remove(callback)
                return True
            except ValueError:
                logger.debug("Callback not found in removal request — may have already been unregistered")
                return False

    def raise_signal(
        self,
        source: str,
        severity: str,
        message: str,
        affected_tasks: list[str] | None = None,
    ) -> AndonSignal:
        """Raise an Andon signal.

        Signals with severity ``critical`` or ``emergency`` automatically
        pause execution.

        Args:
            source: The source.
            severity: The severity.
            message: The message.
            affected_tasks: The affected tasks.

        Returns:
            The AndonSignal result.
        """
        signal = AndonSignal(
            source=source,
            severity=severity,
            message=message,
            affected_tasks=affected_tasks or [],
        )
        self._signals.append(signal)

        # Snapshot callbacks under the lock so concurrent register/deregister
        # during iteration cannot cause IndexError or skip a callback.
        with self._callback_lock:
            callbacks_snapshot = list(self._callbacks)
        for _cb in callbacks_snapshot:
            try:
                _cb(signal)
            except Exception:
                logger.exception("Andon callback raised an exception; continuing")

        if severity in self.PAUSE_SEVERITIES:
            self._paused = True
            logger.critical(
                "ANDON %s from %s: %s (tasks: %s)",
                severity.upper(),
                source,
                message,
                affected_tasks or [],
            )
        else:
            logger.warning(
                "ANDON %s from %s: %s",
                severity.upper(),
                source,
                message,
            )

        return signal

    def acknowledge(self, signal_index: int) -> bool:
        """Acknowledge a signal by its index.

        After acknowledgment, if no unacknowledged critical/emergency
        signals remain the system resumes (unpauses).

        Returns ``True`` if the signal was found and acknowledged.

        Returns:
            True if successful, False otherwise.
        """
        if signal_index < 0 or signal_index >= len(self._signals):
            return False

        self._signals[signal_index].acknowledged = True

        # Check whether we can unpause
        still_critical = any(not s.acknowledged and s.severity in self.PAUSE_SEVERITIES for s in self._signals)
        if not still_critical:
            self._paused = False
            logger.info("Andon system resumed -- all critical signals acknowledged")

        return True

    def is_paused(self) -> bool:
        """Return ``True`` while unacknowledged critical/emergency signals exist."""
        return self._paused

    def get_active_signals(self) -> list[AndonSignal]:
        """Return all signals that have **not** been acknowledged."""
        return [s for s in self._signals if not s.acknowledged]

    def get_all_signals(self) -> list[AndonSignal]:
        """Return every signal (acknowledged or not)."""
        return list(self._signals)

    # -- scoped pause API ---------------------------------------------------

    def pause_scope(self, scope: str, signal: AndonSignal) -> None:
        """Pause a named scope, recording the signal that caused it.

        Scoped pauses are independent of the global pause state.  A scope
        remains paused until :meth:`resume_scope` is called.

        Args:
            scope: Name of the department or pipeline stage to pause
                (e.g. ``"dept-planning"``).
            signal: The :class:`AndonSignal` that triggered the pause.
                The signal's ``scope`` field is updated to this scope name.
        """
        signal.scope = scope
        with self._scope_lock:
            self._paused_scopes[scope] = signal

        self._signals.append(signal)
        with self._callback_lock:
            callbacks_snapshot = list(self._callbacks)
        for _cb in callbacks_snapshot:
            try:
                _cb(signal)
            except Exception:
                logger.exception("Andon scope callback raised an exception for scope %r; continuing", scope)

        logger.warning(
            "ANDON scope %r paused by %s from %s: %s",
            scope,
            signal.severity.upper(),
            signal.source,
            signal.message,
        )

    def resume_scope(self, scope: str) -> bool:
        """Resume a previously paused scope.

        The signal that caused the pause is automatically acknowledged.

        Args:
            scope: The scope name to resume.

        Returns:
            ``True`` if the scope was paused and has been resumed,
            ``False`` if the scope was not paused.
        """
        with self._scope_lock:
            signal = self._paused_scopes.pop(scope, None)

        if signal is None:
            return False

        signal.acknowledged = True
        logger.info("ANDON scope %r resumed", scope)
        return True

    def get_paused_scopes(self) -> list[str]:
        """Return the names of all currently paused scopes.

        Returns:
            Sorted list of scope names that are currently paused.
        """
        with self._scope_lock:
            return sorted(self._paused_scopes.keys())

    def acknowledge_scope(self, scope: str, parent_scope: str | None = None) -> bool:
        """Acknowledge a paused scope, optionally propagating to a parent.

        Hierarchical acknowledgment: acknowledging a child scope also
        resumes it.  If *parent_scope* is provided and is itself paused,
        it is also resumed when the child is acknowledged.

        Args:
            scope: The scope to acknowledge and resume.
            parent_scope: Optional parent scope to also acknowledge.

        Returns:
            ``True`` if the scope was found and acknowledged.
            ``False`` if the scope was not paused.
        """
        resumed = self.resume_scope(scope)
        if not resumed:
            return False

        if parent_scope is not None:
            parent_resumed = self.resume_scope(parent_scope)
            if parent_resumed:
                logger.info("ANDON parent scope %r also resumed after child %r was acknowledged", parent_scope, scope)
            else:
                logger.debug(
                    "ANDON parent scope %r was not paused when child %r was acknowledged",
                    parent_scope,
                    scope,
                )

        return True

    def is_scope_paused(self, scope: str) -> bool:
        """Return ``True`` if the named scope is currently paused.

        Args:
            scope: The scope name to check.

        Returns:
            True if the scope is currently paused.
        """
        with self._scope_lock:
            return scope in self._paused_scopes


# ── Singleton ──

_andon_system_instance: AndonSystem | None = None
_andon_lock = threading.Lock()


def get_andon_system() -> AndonSystem:
    """Return the process-global AndonSystem instance.

    Returns:
        The singleton AndonSystem.
    """
    global _andon_system_instance
    if _andon_system_instance is None:
        with _andon_lock:
            if _andon_system_instance is None:
                _andon_system_instance = AndonSystem()
    return _andon_system_instance


def reset_andon_system() -> None:
    """Reset the singleton (intended for testing)."""
    global _andon_system_instance
    _andon_system_instance = None
