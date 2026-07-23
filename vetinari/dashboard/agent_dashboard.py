"""Agent Performance Dashboard (C10).

==================================
Aggregates metrics from CostTracker, CircuitBreakerRegistry, and agent
execution data into a unified dashboard view.

Provides Flask-compatible route handlers for the web UI.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from vetinari.boundary_guards import assert_dependency_success
from vetinari.types import AgentType
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


@dataclass
class AgentMetrics:
    """Performance metrics for a single agent type."""

    agent_type: str
    total_executions: int = 0
    successful: int = 0
    failed: int = 0
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    total_tokens: int = 0
    total_cost: float = 0.0
    circuit_breaker_state: str = "CLOSED"
    active_modes: list[str] = field(default_factory=list)
    token_budget_remaining: int = 0
    last_execution: str = ""

    def __repr__(self) -> str:
        """Compact representation showing key performance identifiers."""
        return (
            f"AgentMetrics(agent={self.agent_type!r}, "
            f"executions={self.total_executions}, "
            f"success_rate={self.success_rate:.1%}, "
            f"cb={self.circuit_breaker_state!r})"
        )

    @property
    def success_rate(self) -> float:
        """Ratio of successful executions to total executions (0.0 when none)."""
        if self.total_executions == 0:
            return 0.0
        return self.successful / self.total_executions

    def to_dict(self) -> dict[str, Any]:
        """Serialize execution counts, latency, token usage, cost, and circuit breaker state."""
        return dataclass_to_dict(self)


@dataclass
class SystemHealth:
    """Overall system health summary."""

    status: str = "healthy"  # healthy, degraded, unhealthy
    uptime_seconds: float = 0.0
    total_agents: int = 3
    healthy_agents: int = 3
    open_circuit_breakers: list[str] = field(default_factory=list)
    total_cost_24h: float = 0.0
    total_tokens_24h: int = 0
    active_plans: int = 0

    def __repr__(self) -> str:
        """Compact representation showing health status and agent counts."""
        return (
            f"SystemHealth(status={self.status!r}, "
            f"healthy={self.healthy_agents}/{self.total_agents}, "
            f"open_cb={self.open_circuit_breakers!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize health status, uptime, agent counts, circuit breaker states, and 24-hour cost totals."""
        return dataclass_to_dict(self)


_AGENT_TYPES = [
    AgentType.FOREMAN.value,
    AgentType.WORKER.value,
    AgentType.INSPECTOR.value,
]

_AGENT_MODES = {
    AgentType.FOREMAN.value: ["plan", "clarify", "consolidate", "summarise", "prune", "extract"],
    AgentType.WORKER.value: [
        "build",
        "image_generation",
        "code_discovery",
        "domain_research",
        "api_lookup",
        "lateral_thinking",
        "ui_design",
        "database",
        "devops",
        "git_workflow",
        "architecture",
        "risk_assessment",
        "ontological_analysis",
        "contrarian_review",
        "documentation",
        "creative_writing",
        "cost_analysis",
        "experiment",
        "error_recovery",
        "synthesis",
        "improvement",
        "monitor",
        "devops_ops",
        "code_review",
    ],
    AgentType.INSPECTOR.value: ["code_review", "security_audit", "test_generation", "simplification"],
}


class AgentDashboard:
    """Live agent performance dashboard backed by real subsystem data.

    Queries the telemetry collector, cost tracker, and circuit breaker
    registry on every call so the dashboard always reflects current state.
    Subsystem failures are silently absorbed so one unavailable backend
    does not degrade the entire dashboard.
    """

    def __init__(self) -> None:
        self._start_time = time.monotonic()

    @staticmethod
    def _mark_health_degraded(health: SystemHealth, source: str, exc: BaseException) -> None:
        try:
            assert_dependency_success(False, dependency_id=source)
        except RuntimeError:
            logger.warning("Dashboard health source %s failed: %s", source, exc)
        if health.status == "healthy":
            health.status = "degraded"

    def get_agent_metrics(self, agent_type: str) -> AgentMetrics:
        """Return system-wide adapter metrics associated with the given agent type slot.

        NOTE: The returned metrics are **system-wide aggregates** across all
        adapters (e.g. ``llama_cpp``, ``openai``).  Adapter telemetry is not
        keyed by agent type (FOREMAN / WORKER / INSPECTOR), so true per-agent
        breakdown is not available without a telemetry schema change.  Callers
        should treat these as overall system throughput numbers, not per-agent
        figures.  The ``agent_type`` argument controls only the circuit breaker
        lookup and the ``active_modes`` label — the execution counters and
        latency figures reflect the whole system.

        Args:
            agent_type: One of the _AGENT_TYPES identifiers (e.g. ``"FOREMAN"``).
                Used for circuit-breaker lookup and the ``active_modes`` label
                only; does not filter telemetry data.

        Returns:
            AgentMetrics whose execution/latency/token fields contain
            system-wide aggregates and whose ``circuit_breaker_state`` and
            ``active_modes`` fields are agent-specific where available.
        """
        metrics = AgentMetrics(
            agent_type=agent_type,
            active_modes=_AGENT_MODES.get(agent_type, []),
        )

        try:
            from vetinari.resilience import get_circuit_breaker_registry

            cb = get_circuit_breaker_registry().get(agent_type)
            metrics.circuit_breaker_state = cb.state.value
        except Exception:
            logger.warning("Circuit breaker state unavailable for agent %s", agent_type, exc_info=True)

        try:
            from vetinari.analytics.cost import get_cost_tracker

            tracker = get_cost_tracker()
            report = tracker.get_report(agent=agent_type)
            if report.total_requests > 0:
                metrics.total_cost = report.total_cost_usd
                metrics.total_tokens = report.total_tokens
                metrics.total_executions = report.total_requests
        except Exception:
            logger.warning("Cost data unavailable for agent %s", agent_type, exc_info=True)

        try:
            from vetinari.telemetry import get_telemetry_collector

            tel = get_telemetry_collector()
            adapter_metrics = tel.get_adapter_metrics()
            total_latency_sum = 0.0
            total_successful_for_latency = 0
            for _key, am in adapter_metrics.items():
                metrics.successful += am.successful_requests
                metrics.failed += am.failed_requests
                metrics.total_executions += am.total_requests
                total_latency_sum += am.total_latency_ms
                total_successful_for_latency += am.successful_requests
                metrics.total_tokens += am.total_tokens_used
                if am.last_request_time and am.last_request_time > metrics.last_execution:
                    metrics.last_execution = am.last_request_time
            if total_successful_for_latency > 0:
                metrics.avg_latency_ms = total_latency_sum / total_successful_for_latency
        except Exception:
            logger.warning("Telemetry data unavailable for agent %s", agent_type, exc_info=True)

        return metrics

    def get_all_agent_metrics(self) -> list[AgentMetrics]:
        """Return system-wide metrics mapped to each factory pipeline agent type.

        Because adapter telemetry is not keyed by agent type, all three
        returned entries share the same system-wide execution/latency/token
        totals.  The ``agent_type``, ``circuit_breaker_state``, and
        ``active_modes`` fields differ per entry; everything else is identical.
        This is the honest representation of what the telemetry schema
        currently supports.  A per-agent breakdown requires a telemetry
        schema change to tag records with the originating agent type.

        Returns:
            List of AgentMetrics in FOREMAN, WORKER, INSPECTOR order, each
            containing the same system-wide aggregate numbers.
        """
        # Compute once and stamp the correct agent_type onto each copy so the
        # circuit breaker state and active_modes differ per slot, while the
        # execution/latency figures reflect the shared system-wide view.
        return [self.get_agent_metrics(at) for at in _AGENT_TYPES]

    def get_system_health(self) -> SystemHealth:
        """Aggregate system health from circuit breaker and cost tracker state.

        Derives the ``status`` field from how many agent circuit breakers are open:
        all closed → healthy, ≥1 open → degraded, all open → unhealthy.
        Pulls 24-hour cost and token totals from the CostTracker singleton.

        Returns:
            A SystemHealth snapshot with uptime, circuit breaker states, and
            24-hour cost/token totals.  Subsystem failures are logged and
            silently absorbed so the health endpoint always responds.
        """
        health = SystemHealth(
            uptime_seconds=time.monotonic() - self._start_time,
        )

        # Check circuit breakers
        try:
            from vetinari.resilience import get_circuit_breaker_registry

            registry = get_circuit_breaker_registry()
            for at in _AGENT_TYPES:
                cb = registry.get(at)
                if cb.state.value == "open":
                    health.open_circuit_breakers.append(at)
        except Exception as exc:
            self._mark_health_degraded(health, "circuit_breaker_registry", exc)
            logger.warning("Circuit breaker registry unavailable; skipping open-breaker check", exc_info=True)

        health.healthy_agents = health.total_agents - len(health.open_circuit_breakers)
        if len(health.open_circuit_breakers) >= 3:
            health.status = "unhealthy"
        elif len(health.open_circuit_breakers) >= 1:
            health.status = "degraded"

        # Cost summary from CostTracker report (last 24 hours)
        try:
            from vetinari.analytics.cost import get_cost_tracker

            report = get_cost_tracker().get_report(since=time.time() - 86400)
            health.total_cost_24h = report.total_cost_usd
            health.total_tokens_24h = report.total_tokens
        except Exception as exc:
            self._mark_health_degraded(health, "cost_tracker", exc)
            logger.warning("Cost tracker summary unavailable; 24h totals will be zero", exc_info=True)

        return health

    def get_dashboard_data(self) -> dict[str, Any]:
        """Build the complete dashboard payload for the API response.

        Returns:
            Dict with three top-level keys: ``health`` (system health summary),
            ``agents`` (per-agent metrics list), and ``agent_modes`` (mode
            registry keyed by agent type).
        """
        return {
            "health": self.get_system_health().to_dict(),
            "agents": [m.to_dict() for m in self.get_all_agent_metrics()],
            "agent_modes": _AGENT_MODES,
        }


# ── Singleton ─────────────────────────────────────────────────────────

_dashboard: AgentDashboard | None = None
_dashboard_lock = threading.Lock()


def get_agent_dashboard() -> AgentDashboard:
    """Return the process-wide AgentDashboard singleton, creating it on first call.

    Returns:
        The shared AgentDashboard instance.
    """
    global _dashboard
    if _dashboard is None:
        with _dashboard_lock:
            if _dashboard is None:
                _dashboard = AgentDashboard()
    return _dashboard
