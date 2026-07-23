"""Agent-level circuit breaker for per-task failure isolation.

Provides a lightweight CircuitBreaker focused on individual agent task
slots rather than the per-backend breaker in ``vetinari.resilience.circuit_breaker``.
Use this when you need to trip on a *specific agent's task failures* rather
than backend connectivity failures.

Three states:
  CLOSED    — normal operation; failures counted
  OPEN      — tripped; requests rejected until cooldown expires
  HALF_OPEN — recovery probe; one request allowed through
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# Default thresholds — conservative for local model scenarios
DEFAULT_FAILURE_THRESHOLD = 3  # consecutive task failures to trip
DEFAULT_COOLDOWN_SECONDS = 60  # seconds in OPEN before probing


class CircuitState(Enum):
    """States for the agent-level circuit breaker.

    Follows the standard three-state circuit breaker pattern.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitStatus:
    """Snapshot of an AgentCircuitBreaker's current state.

    Args:
        state: Current circuit state.
        consecutive_failures: Failures since last success.
        total_failures: Total failure count since construction.
        total_successes: Total success count since construction.
        total_rejections: Total requests rejected while OPEN.
        opened_at: Epoch time when the circuit was last opened, or 0.0.
        cooldown_remaining: Seconds remaining in cooldown (0.0 when CLOSED).
    """

    state: CircuitState
    consecutive_failures: int
    total_failures: int
    total_successes: int
    total_rejections: int
    opened_at: float
    cooldown_remaining: float

    def __repr__(self) -> str:
        return (
            f"CircuitStatus(state={self.state.value!r}, "
            f"consecutive_failures={self.consecutive_failures!r}, "
            f"cooldown_remaining={self.cooldown_remaining:.1f}s)"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dictionary for dashboard display.

        Returns:
            Dictionary with all status fields.
        """
        return {
            "state": self.state.value,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "total_rejections": self.total_rejections,
            "opened_at": self.opened_at,
            "cooldown_remaining": self.cooldown_remaining,
        }


class AgentCircuitBreaker:
    """Per-agent circuit breaker that trips on consecutive task failures.

    Designed for use inside the AgentGraph execution loop where a given
    agent slot should be quarantined after repeated failures to avoid
    wasting tokens on a stuck agent.

    Usage::

        cb = AgentCircuitBreaker("worker_slot_1")
        if cb.allow_request():
            try:
                result = agent.execute(task)
                cb.record_success()
            except Exception as exc:
                cb.record_failure()
                raise
        else:
            # skip or escalate
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        """Initialise the circuit breaker.

        Args:
            name: Identifying label used in log messages.
            failure_threshold: Consecutive failures before the circuit opens.
            cooldown_seconds: Seconds to remain OPEN before probing.
        """
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be > 0")

        self.name = name
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds

        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._total_failures = 0
        self._total_successes = 0
        self._total_rejections = 0
        self._opened_at: float = 0.0
        self._half_open_probe_sent = False

        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def _maybe_recover(self) -> None:
        """Attempt recovery from OPEN → HALF_OPEN if cooldown has elapsed.

        Must be called with ``self._lock`` held.
        """
        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._cooldown_seconds:
                self._state = CircuitState.HALF_OPEN
                self._half_open_probe_sent = False
                logger.info(
                    "[AgentCircuitBreaker] %s transitioning OPEN → HALF_OPEN after %.1fs",
                    self.name,
                    elapsed,
                )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def allow_request(self) -> bool:
        """Determine whether a new request should be allowed through.

        In CLOSED state all requests are allowed.  In OPEN state all
        requests are rejected until the cooldown expires.  In HALF_OPEN
        exactly one probe request is allowed; subsequent calls return False
        until the probe outcome is recorded via record_success/failure.

        Returns:
            True if the caller may proceed with the task.
        """
        with self._lock:
            self._maybe_recover()

            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                self._total_rejections += 1
                logger.debug(
                    "[AgentCircuitBreaker] %s is OPEN — request rejected (total rejections=%d)",
                    self.name,
                    self._total_rejections,
                )
                return False

            # HALF_OPEN: allow exactly one probe
            if not self._half_open_probe_sent:
                self._half_open_probe_sent = True
                return True

            self._total_rejections += 1
            return False

    def record_success(self) -> None:
        """Record that the most recent request succeeded.

        In HALF_OPEN state a success closes the circuit and resets the
        failure counter.  In CLOSED state the consecutive failure counter
        is reset.
        """
        with self._lock:
            self._total_successes += 1
            self._consecutive_failures = 0

            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._opened_at = 0.0
                logger.info(
                    "[AgentCircuitBreaker] %s recovered: HALF_OPEN → CLOSED",
                    self.name,
                )

    def record_failure(self) -> None:
        """Record that the most recent request failed.

        In CLOSED state, increments the consecutive failure counter and
        trips the circuit if the threshold is reached.  In HALF_OPEN state,
        a failure reopens the circuit and resets the cooldown timer.
        """
        with self._lock:
            self._total_failures += 1
            self._consecutive_failures += 1

            if self._state == CircuitState.HALF_OPEN:
                # Probe failed — back to OPEN
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                self._half_open_probe_sent = False
                logger.warning(
                    "[AgentCircuitBreaker] %s probe failed: HALF_OPEN → OPEN (cooldown restarted)",
                    self.name,
                )
                return

            if self._state == CircuitState.CLOSED and self._consecutive_failures >= self._failure_threshold:
                self._state = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "[AgentCircuitBreaker] %s tripped: CLOSED → OPEN (consecutive_failures=%d, threshold=%d)",
                    self.name,
                    self._consecutive_failures,
                    self._failure_threshold,
                )

    def get_status(self) -> CircuitStatus:
        """Return a point-in-time snapshot of the breaker's state.

        Returns:
            CircuitStatus with all current counters.
        """
        with self._lock:
            self._maybe_recover()
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._opened_at
                cooldown_remaining = max(0.0, self._cooldown_seconds - elapsed)
            else:
                cooldown_remaining = 0.0

            return CircuitStatus(
                state=self._state,
                consecutive_failures=self._consecutive_failures,
                total_failures=self._total_failures,
                total_successes=self._total_successes,
                total_rejections=self._total_rejections,
                opened_at=self._opened_at,
                cooldown_remaining=cooldown_remaining,
            )

    def reset(self) -> None:
        """Forcibly close the circuit and reset all counters.

        Intended for testing and manual operator intervention only.
        """
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._total_failures = 0
            self._total_successes = 0
            self._total_rejections = 0
            self._opened_at = 0.0
            self._half_open_probe_sent = False
        logger.info("[AgentCircuitBreaker] %s manually reset to CLOSED", self.name)
