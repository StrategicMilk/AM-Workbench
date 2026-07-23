"""Circuit breaker wiring — per-agent inference call protection.

Thin wrapper that connects the inference path to the CircuitBreaker registry.
Every outbound LLM call flows through here so that a misbehaving backend trips
the appropriate breaker and fails fast instead of stacking up blocked threads.

Pipeline role: sits between the agent execution layer and the adapter layer.
When an agent calls an LLM backend, it passes the callable through
``call_with_breaker`` so that repeated failures trip the breaker and subsequent
calls are rejected immediately (CircuitBreakerOpen) until the backend recovers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from vetinari.errors import FailClosedError
from vetinari.resilience.circuit_breaker import (
    CircuitBreaker,
    get_circuit_breaker_registry,
)

logger = logging.getLogger(__name__)


T = TypeVar("T")


def get_inference_breaker(agent_type: str) -> CircuitBreaker:
    """Return the circuit breaker assigned to the given agent type.

    The breaker is created with per-agent defaults on first access and reused
    for all subsequent calls from the same agent type.

    Args:
        agent_type: The agent type string (e.g. ``AgentType.FOREMAN.value``).

    Returns:
        The CircuitBreaker instance for that agent type from the global registry.

    Raises:
        FailClosedError: Raised when ``agent_type`` is blank or only whitespace.
    """
    if not agent_type or not agent_type.strip():
        raise FailClosedError(
            "circuit_breaker.agent_type",
            "agent type is required before breaker lookup",
            recovery="pass the concrete runtime agent type",
        )
    # Normalise to uppercase so callers passing either the enum .value string
    # (e.g. "foreman") or the raw uppercase form both hit the same registry entry.
    return get_circuit_breaker_registry().get(agent_type.upper())


def call_with_breaker(
    agent_type: str,
    fn: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Execute ``fn`` through the circuit breaker for ``agent_type``.

    The circuit breaker records success or failure automatically.  If the
    circuit is tripped (i.e. the backend is known to be down) the call is
    rejected immediately without invoking ``fn``, saving threads and latency.

    Args:
        agent_type: The agent type string used to look up the correct breaker.
        fn: The callable to execute — typically an inference method.
        *args: Positional arguments forwarded verbatim to ``fn``.
        **kwargs: Keyword arguments forwarded verbatim to ``fn``.

    Returns:
        Whatever ``fn`` returns on success.

    Raises:
        CircuitBreakerOpen: If the circuit is OPEN and not yet ready for a
            recovery probe.  The caller should surface this as a fast-failure
            rather than waiting for a timeout.
        Exception: Any exception raised by ``fn`` is re-raised after the
            failure is recorded on the breaker.
    """
    if not agent_type or not agent_type.strip():
        raise FailClosedError(
            "circuit_breaker.agent_type",
            "agent type is required before protected inference call",
            recovery="pass the concrete runtime agent type",
        )
    # Normalise to uppercase so callers passing either the enum .value string
    # (e.g. "foreman") or the raw uppercase form both hit the same registry entry.
    breaker = get_circuit_breaker_registry().get(agent_type.upper())
    try:
        return breaker.call(fn, *args, **kwargs)
    except Exception:
        # After any failure, log the recommended back-off delay so callers
        # know how long to wait before the next retry attempt.
        delay = breaker.get_backoff_delay()
        if delay > 0:
            logger.warning(
                "Circuit breaker '%s' recorded failure — recommended back-off: %.1fs",
                agent_type,
                delay,
            )
        raise


def check_breaker_health(agent_type: str) -> dict[str, Any]:
    """Return the current state of the circuit breaker for a single agent type.

    Intended for dashboard endpoints that need per-agent health snapshots.
    Includes ``backoff_delay_s``, the current exponential back-off delay that
    callers should observe before retrying a failed request.

    Args:
        agent_type: The agent type string to look up.

    Returns:
        Dictionary containing the breaker name, state, stats, config,
        time remaining until the next HALF_OPEN probe window (via
        ``CircuitBreaker.to_dict()``), and ``backoff_delay_s`` — the
        recommended wait before the next retry attempt.

    Raises:
        FailClosedError: Raised when ``agent_type`` is blank or only whitespace.
    """
    if not agent_type or not agent_type.strip():
        raise FailClosedError(
            "circuit_breaker.agent_type",
            "agent type is required before health lookup",
            recovery="pass the concrete runtime agent type",
        )
    breaker = get_circuit_breaker_registry().get(agent_type.upper())
    health = breaker.to_dict()
    health["name"] = agent_type.strip().lower()
    # Expose the computed back-off delay so callers can apply it without
    # duplicating the exponential-backoff formula.
    health["backoff_delay_s"] = breaker.get_backoff_delay()
    return health


def get_all_breaker_health() -> dict[str, Any]:
    """Return a health summary for every breaker registered so far.

    Delegates to ``CircuitBreakerRegistry.get_summary()`` which serializes all
    known breakers in a single pass.  Breakers that have never been accessed
    will not appear until their first use.

    Returns:
        Dictionary mapping each agent type name to its serialized breaker state
        (as produced by ``CircuitBreaker.to_dict()``).
    """
    return get_circuit_breaker_registry().get_summary()


def wire_resilience_subsystem() -> None:
    """Activate the resilience subsystem by ensuring the registry is initialised.

    Call once during application startup (e.g. from the Litestar lifespan hook)
    so the registry singleton is created eagerly rather than on the first
    inference call.  Subsequent calls are idempotent.
    """
    get_circuit_breaker_registry()
    logger.info("Resilience subsystem wired — circuit breaker registry ready")
