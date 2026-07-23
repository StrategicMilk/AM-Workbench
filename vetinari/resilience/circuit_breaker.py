"""Circuit Breaker — production-grade, thread-safe, per-agent breakers.

Per-agent circuit breakers that prevent cascading failures when LLM
backends or downstream services become unreliable.  Includes a singleton
registry, exponential backoff, and dashboard-friendly stats.

Three states:
  CLOSED   -- normal operation; failures are counted
  OPEN     -- tripped; calls are rejected immediately
  HALF_OPEN -- recovery probe; one call allowed through

Config per agent:
  failure_threshold  = 5     (consecutive failures to trip)
  recovery_timeout   = 30s   (time in OPEN before moving to HALF_OPEN)
  half_open_max_calls = 1    (probe calls allowed in HALF_OPEN)

Exponential backoff is applied to retries before the breaker trips.

This is the sole circuit breaker implementation — all subsystems
(agent graph, inference mixin, etc.) use this module.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, TypeVar

from vetinari.constants import (
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    CIRCUIT_BREAKER_FAILURE_THRESHOLD_HIGH,
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT,
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT_LONG,
)
from vetinari.types import AgentType
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


T = TypeVar("T")


class ResilienceCircuitState(Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class CircuitBreakerConfig:
    """Configuration for a single circuit breaker."""

    failure_threshold: int = 5  # Local models may fail on large contexts but succeed on smaller ones
    recovery_timeout: float = 30.0  # seconds — local models recover faster than cloud APIs
    half_open_max_calls: int = 1
    # Exponential backoff for retries before tripping
    backoff_base: float = 1.0  # seconds
    backoff_max: float = 30.0  # seconds
    backoff_factor: float = 2.0

    def __post_init__(self) -> None:
        if self.failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if self.recovery_timeout < 0:
            raise ValueError("recovery_timeout must be >= 0")
        if self.half_open_max_calls < 1:
            raise ValueError("half_open_max_calls must be >= 1")
        if self.backoff_base < 0:
            raise ValueError("backoff_base must be >= 0")
        if self.backoff_max < self.backoff_base:
            raise ValueError("backoff_max must be >= backoff_base")
        if self.backoff_factor < 1:
            raise ValueError("backoff_factor must be >= 1")

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"CircuitBreakerConfig(failure_threshold={self.failure_threshold!r},"
            f" recovery_timeout={self.recovery_timeout!r})"
        )


@dataclass(frozen=True, slots=True)
class CircuitBreakerStats:
    """Runtime statistics for a circuit breaker."""

    total_calls: int = 0
    total_successes: int = 0
    total_failures: int = 0
    total_rejections: int = 0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    last_state_change: float = 0.0
    trips: int = 0  # total times breaker has tripped

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"CircuitBreakerStats(total_calls={self.total_calls!r},"
            f" total_failures={self.total_failures!r},"
            f" trips={self.trips!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize breaker statistics to a plain dictionary for dashboard display.

        Returns:
            Dictionary containing call counts, failure streaks, and trip totals.
        """
        return dataclass_to_dict(self)


class CircuitBreakerOpen(Exception):
    """Raised when a call is rejected because the circuit is open."""

    def __init__(self, breaker_name: str, remaining: float):
        self.breaker_name = breaker_name
        self.remaining = remaining
        super().__init__(f"Circuit breaker '{breaker_name}' is OPEN — retry in {remaining:.1f}s")


class CircuitBreaker:
    """Per-agent circuit breaker.

    Usage::

        cb = CircuitBreaker("planner")
        result = cb.call(lambda: llm_infer(prompt))

    Or as a guard::

        if cb.allow_request():
            try:
                result = llm_infer(prompt)
                cb.record_success()
            except Exception:
                cb.record_failure()
        else:
            # fallback logic
    """

    def __init__(
        self,
        name: str,
        config: CircuitBreakerConfig | None = None,
    ):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = ResilienceCircuitState.CLOSED
        self.stats = CircuitBreakerStats()
        self._lock = threading.Lock()
        self._half_open_calls = 0
        self._half_open_successes = 0  # Successful probes in HALF_OPEN — must reach half_open_max_calls to close
        self._opened_at: float = 0.0

    # ── Public properties ─────────────────────────────────────────────

    @property
    def state(self) -> ResilienceCircuitState:
        """Current state (may auto-transition from OPEN → HALF_OPEN)."""
        with self._lock:
            self._maybe_transition()
            return self._state

    @property
    def is_closed(self) -> bool:
        """Return True if the circuit is in the CLOSED (healthy) state."""
        return self.state == ResilienceCircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        """Return True if the circuit is in the OPEN (tripped) state."""
        return self.state == ResilienceCircuitState.OPEN

    # ── Core API ──────────────────────────────────────────────────────

    def allow_request(self) -> bool:
        """Check whether a request should be allowed through.

        Returns:
            True when the circuit is CLOSED or a HALF_OPEN probe slot is
            available; False when the circuit is OPEN or the HALF_OPEN probe
            limit has been reached.
        """
        with self._lock:
            self._maybe_transition()

            if self._state == ResilienceCircuitState.CLOSED:
                return True

            if self._state == ResilienceCircuitState.HALF_OPEN:
                if self._half_open_calls < self.config.half_open_max_calls:
                    self._half_open_calls += 1
                    return True
                return False

            # OPEN
            return False

    def record_success(self) -> None:
        """Record a successful call."""
        with self._lock:
            self.stats = replace(
                self.stats,
                total_calls=self.stats.total_calls + 1,
                total_successes=self.stats.total_successes + 1,
                consecutive_successes=self.stats.consecutive_successes + 1,
                consecutive_failures=0,
                last_success_time=time.monotonic(),
            )

            if self._state == ResilienceCircuitState.HALF_OPEN:
                # Accumulate successful probes — only close when all required probes pass.
                # A single success is not enough when half_open_max_calls > 1.
                self._half_open_successes += 1
                if self._half_open_successes >= self.config.half_open_max_calls:
                    self._transition(ResilienceCircuitState.CLOSED)
                    logger.info(
                        "Circuit breaker '%s' recovered after %d successful probe(s) → CLOSED",
                        self.name,
                        self._half_open_successes,
                    )

    def record_failure(self) -> None:
        """Record a failed call."""
        with self._lock:
            self.stats = replace(
                self.stats,
                total_calls=self.stats.total_calls + 1,
                total_failures=self.stats.total_failures + 1,
                consecutive_failures=self.stats.consecutive_failures + 1,
                consecutive_successes=0,
                last_failure_time=time.monotonic(),
            )

            if self._state == ResilienceCircuitState.HALF_OPEN:
                # Probe failed → re-open
                self._trip()
                logger.warning("Circuit breaker '%s' probe failed → re-OPEN", self.name)
            elif self._state == ResilienceCircuitState.CLOSED:
                if self.stats.consecutive_failures >= self.config.failure_threshold:
                    self._trip()
                    logger.warning(
                        "Circuit breaker '%s' tripped after %d consecutive failures → OPEN",
                        self.name,
                        self.stats.consecutive_failures,
                    )

    def record_budget_exhaustion(self) -> None:
        """Record that an agent's budget was exhausted during this call.

        Budget exhaustion is treated as a failure for circuit breaker
        purposes — if an agent repeatedly exhausts its budget it likely
        indicates a misconfigured budget or runaway agent loop, and the
        circuit should trip to prevent further resource consumption.
        """
        logger.warning(
            "Circuit breaker '%s': budget exhaustion recorded (counting as failure)",
            self.name,
        )
        self.record_failure()

    def call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Execute ``fn`` through the circuit breaker.

        Raises CircuitBreakerOpen if the circuit is open and not ready
        for a probe.

        Args:
            fn: Callable to execute.
            *args: Positional arguments forwarded to ``fn``.
            **kwargs: Keyword arguments forwarded to ``fn``.

        Returns:
            Whatever ``fn`` returns on success.

        Raises:
            CircuitBreakerOpen: If the circuit is OPEN and not ready for a probe.
        """
        if not self.allow_request():
            with self._lock:
                remaining = self._time_until_half_open()
                self.stats = replace(self.stats, total_rejections=self.stats.total_rejections + 1)
            raise CircuitBreakerOpen(self.name, remaining)

        try:
            result = fn(*args, **kwargs)
            self.record_success()
            return result
        except Exception:
            self.record_failure()
            raise

    def reset(self) -> None:
        """Manually reset the breaker to CLOSED."""
        with self._lock:
            self._transition(ResilienceCircuitState.CLOSED)
            self.stats = replace(self.stats, consecutive_failures=0, consecutive_successes=0)
            logger.info("Circuit breaker '%s' manually reset → CLOSED", self.name)

    def trip(self) -> None:
        """Manually trip the breaker → OPEN. Used by external callers like anomaly detection."""
        with self._lock:
            if self._state != ResilienceCircuitState.OPEN:
                self._trip()
                logger.warning("Circuit breaker '%s' manually tripped → OPEN", self.name)

    def get_backoff_delay(self) -> float:
        """Return the current exponential backoff delay in seconds.

        Returns:
            Delay in seconds based on the number of consecutive failures and
            the configured backoff base, factor, and maximum. Returns 0.0 when
            there are no consecutive failures.
        """
        n = self.stats.consecutive_failures
        if n == 0:
            return 0.0
        delay = self.config.backoff_base * (self.config.backoff_factor ** (n - 1))
        return min(delay, self.config.backoff_max)

    # ── Internal ──────────────────────────────────────────────────────

    def _trip(self) -> None:
        """Trip the breaker → OPEN. Must hold lock."""
        self._transition(ResilienceCircuitState.OPEN)
        self._opened_at = time.monotonic()
        self.stats = replace(self.stats, trips=self.stats.trips + 1)

    def _transition(self, new_state: ResilienceCircuitState) -> None:
        """Transition to a new state. Must hold lock."""
        if self._state != new_state:
            self._state = new_state
            self.stats = replace(self.stats, last_state_change=time.monotonic())
            if new_state == ResilienceCircuitState.HALF_OPEN:
                # Reset both probe counters when entering HALF_OPEN so each
                # recovery window starts with a clean slate.
                self._half_open_calls = 0
                self._half_open_successes = 0

    def _maybe_transition(self) -> None:
        """Auto-transition OPEN → HALF_OPEN if recovery timeout elapsed. Must hold lock."""
        if self._state == ResilienceCircuitState.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self.config.recovery_timeout:
                self._transition(ResilienceCircuitState.HALF_OPEN)
                logger.info(
                    "Circuit breaker '%s' recovery timeout elapsed → HALF_OPEN",
                    self.name,
                )

    def _time_until_half_open(self) -> float:
        """Seconds until OPEN → HALF_OPEN transition."""
        if self._state != ResilienceCircuitState.OPEN:
            return 0.0
        elapsed = time.monotonic() - self._opened_at
        return max(0.0, self.config.recovery_timeout - elapsed)

    def to_dict(self) -> dict[str, Any]:
        """Serialize state for dashboards."""
        return {
            "name": self.name,
            "state": self.state.value,
            "stats": self.stats.to_dict(),
            "config": {
                "failure_threshold": self.config.failure_threshold,
                "recovery_timeout": self.config.recovery_timeout,
                "half_open_max_calls": self.config.half_open_max_calls,
            },
            "time_until_half_open": self._time_until_half_open(),
        }


# ── Per-agent defaults ────────────────────────────────────────────────

_AGENT_BREAKER_CONFIGS: dict[str, CircuitBreakerConfig] = {
    AgentType.FOREMAN.value: CircuitBreakerConfig(
        failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        recovery_timeout=CIRCUIT_BREAKER_RECOVERY_TIMEOUT,
    ),
    AgentType.WORKER.value: CircuitBreakerConfig(
        failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD_HIGH,
        recovery_timeout=CIRCUIT_BREAKER_RECOVERY_TIMEOUT_LONG,
    ),
    AgentType.INSPECTOR.value: CircuitBreakerConfig(
        failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        recovery_timeout=CIRCUIT_BREAKER_RECOVERY_TIMEOUT,
    ),
    # Non-agent breakers for external call sites (session 14.6)
    "model_scout": CircuitBreakerConfig(
        failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        recovery_timeout=CIRCUIT_BREAKER_RECOVERY_TIMEOUT_LONG,
    ),
    "external_api": CircuitBreakerConfig(
        failure_threshold=CIRCUIT_BREAKER_FAILURE_THRESHOLD_HIGH,
        recovery_timeout=CIRCUIT_BREAKER_RECOVERY_TIMEOUT_LONG,
    ),
}


class CircuitBreakerRegistry:
    """Singleton registry of per-agent circuit breakers."""

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get(self, agent_type: str) -> CircuitBreaker:
        """Get or create the circuit breaker for an agent type.

        Args:
            agent_type: Agent name (e.g. "FOREMAN", "WORKER", "INSPECTOR").

        Returns:
            The CircuitBreaker for the given agent type, creating it with
            per-agent defaults if it does not yet exist.
        """
        with self._lock:
            if agent_type not in self._breakers:
                config = _AGENT_BREAKER_CONFIGS.get(agent_type, CircuitBreakerConfig())
                self._breakers[agent_type] = CircuitBreaker(agent_type, config)
            return self._breakers[agent_type]

    def get_all(self) -> dict[str, CircuitBreaker]:
        """Return all registered breakers.

        Returns:
            Snapshot dictionary mapping agent type name to its CircuitBreaker.
        """
        with self._lock:
            return dict(self._breakers)

    def get_summary(self) -> dict[str, Any]:
        """Return a dashboard-friendly summary of all breakers.

        Returns:
            Dictionary mapping each agent type to its serialized breaker state
            (state, stats, config, time_until_half_open).
        """
        with self._lock:
            return {name: cb.to_dict() for name, cb in self._breakers.items()}

    def reset_all(self) -> None:
        """Reset all breakers to CLOSED."""
        with self._lock:
            for cb in self._breakers.values():
                cb.reset()


# ── Singleton ─────────────────────────────────────────────────────────

_registry: CircuitBreakerRegistry | None = None
_registry_lock = threading.Lock()


def get_circuit_breaker_registry() -> CircuitBreakerRegistry:
    """Get or create the global circuit breaker registry.

    Returns:
        The singleton CircuitBreakerRegistry shared by all subsystems.
    """
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = CircuitBreakerRegistry()
    return _registry


def reset_circuit_breaker_registry() -> None:
    """Reset the global registry singleton to None.

    Intended for use in tests that need a clean registry between cases.
    NEVER call this in production code.
    """
    global _registry
    with _registry_lock:
        _registry = None


# Backward compatibility alias
CircuitState = ResilienceCircuitState
