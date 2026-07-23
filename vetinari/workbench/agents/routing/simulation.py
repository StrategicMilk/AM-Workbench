"""Deterministic route simulation helpers."""

from __future__ import annotations

from .decision import (
    AgentRouteCostEstimate,
    HarnessAdmissionSummary,
    LatencyEstimate,
    MemoryContextSummary,
    PolicyGateResult,
    RouteCandidate,
    RouteDecisionRecord,
    build_route_decision,
)


def simulate_route_decision(
    *,
    decision_id: str,
    candidate_agents: tuple[RouteCandidate, ...],
    candidate_models: tuple[RouteCandidate, ...],
    candidate_tools: tuple[RouteCandidate, ...],
    policy_gates: tuple[PolicyGateResult, ...] | None,
    memory_context: MemoryContextSummary | None,
    harness_admission: HarnessAdmissionSummary | None,
    cost_estimate: AgentRouteCostEstimate | None,
    latency_estimate: LatencyEstimate | None,
    required_capabilities: tuple[str, ...] = (),
    fallback_reason: str | None = None,
    created_at_utc: str = "1970-01-01T00:00:00Z",
) -> RouteDecisionRecord:
    """Return a pure route decision snapshot without executing models, tools, or agents."""
    return build_route_decision(
        decision_id=decision_id,
        candidate_agents=tuple(candidate_agents),
        candidate_models=tuple(candidate_models),
        candidate_tools=tuple(candidate_tools),
        policy_gates=policy_gates,
        memory_context=memory_context,
        harness_admission=harness_admission,
        cost_estimate=cost_estimate,
        latency_estimate=latency_estimate,
        required_capabilities=required_capabilities,
        fallback_reason=fallback_reason,
        created_at_utc=created_at_utc,
    )


__all__ = ["simulate_route_decision"]
