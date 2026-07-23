"""Resilience subsystem — circuit breakers, retries, degradation, and fault tolerance."""

from __future__ import annotations

from vetinari.resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpen,
    CircuitBreakerRegistry,
    CircuitState,
    ResilienceCircuitState,
    get_circuit_breaker_registry,
    reset_circuit_breaker_registry,
)
from vetinari.resilience.degradation import (
    DegradationLevel,
    DegradationManager,
    reset_degradation_manager,
)
from vetinari.resilience.retry_intelligence import (
    RetryAnalyzer,
    RetryStrategy,
    get_retry_analyzer,
    reset_retry_analyzer,
)
from vetinari.resilience.retry_policy import (
    RetryBudget,
    RetryPolicy,
    is_retryable_transport_error,
)
from vetinari.resilience.wiring import (
    call_with_breaker,
    check_breaker_health,
    get_all_breaker_health,
    get_inference_breaker,
    wire_resilience_subsystem,
)

__all__ = [
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerOpen",
    "CircuitBreakerRegistry",
    "CircuitState",
    "DegradationLevel",
    "DegradationManager",
    "ResilienceCircuitState",
    "RetryAnalyzer",
    "RetryBudget",
    "RetryPolicy",
    "RetryStrategy",
    "call_with_breaker",
    "check_breaker_health",
    "get_all_breaker_health",
    "get_circuit_breaker_registry",
    "get_inference_breaker",
    "get_retry_analyzer",
    "is_retryable_transport_error",
    "reset_circuit_breaker_registry",
    "reset_degradation_manager",
    "reset_retry_analyzer",
    "wire_resilience_subsystem",
]
