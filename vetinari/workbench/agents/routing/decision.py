"""Typed route-decision evidence for Workbench agent routing."""

from __future__ import annotations

from vetinari.workbench.agents.routing.decision_records import (
    DEFAULT_DECISION_TIME_UTC,
    SCHEMA_VERSION,
    AgentRouteCostEstimate,
    CapabilityEvidence,
    HarnessAdmissionSummary,
    LatencyEstimate,
    MemoryContextSummary,
    PolicyGateResult,
    RouteCandidate,
    RouteCandidateKind,
    RouteDecisionError,
    RouteDecisionOutcome,
    RouteDecisionRecord,
    RouteRejection,
)


def build_route_decision(
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
    selected_candidate_id: str | None = None,
    fallback_reason: str | None = None,
    created_at_utc: str = DEFAULT_DECISION_TIME_UTC,
) -> RouteDecisionRecord:
    """Assemble a fail-closed route decision from already-computed evidence.

    Returns:
        Newly constructed route decision value.
    """
    all_candidates = tuple(candidate_agents) + tuple(candidate_models) + tuple(candidate_tools)
    blockers, rejections = _route_evidence_blockers(policy_gates, harness_admission, memory_context)
    blockers.extend(_estimate_blockers(cost_estimate, latency_estimate, all_candidates))
    viable_candidates, capability_rejections = _viable_candidates(all_candidates, set(required_capabilities))
    rejections.extend(capability_rejections)
    if all_candidates and not viable_candidates:
        blockers.append("candidate_capability_evidence_missing")

    selected_candidate = _select_candidate(viable_candidates, selected_candidate_id)
    if selected_candidate_id and selected_candidate is None:
        blockers.append("selected_candidate_unavailable")

    if blockers:
        outcome = _blocked_route_outcome(blockers)
        selected_id = None
    else:
        selected_candidate = selected_candidate or viable_candidates[0]
        selected_id = selected_candidate.candidate_id
        first_candidate_id = all_candidates[0].candidate_id
        outcome = (
            RouteDecisionOutcome.FALLBACK_SELECTED
            if selected_id != first_candidate_id or fallback_reason
            else RouteDecisionOutcome.SELECTED
        )

    return RouteDecisionRecord(
        decision_id=decision_id,
        schema_version=SCHEMA_VERSION,
        created_at_utc=created_at_utc,
        candidate_agents=tuple(candidate_agents),
        candidate_models=tuple(candidate_models),
        candidate_tools=tuple(candidate_tools),
        rejected_alternatives=tuple(rejections),
        policy_gates=tuple(policy_gates or ()),
        memory_context=memory_context,
        harness_admission=harness_admission,
        cost_estimate=cost_estimate,
        latency_estimate=latency_estimate,
        selected_candidate_id=selected_id,
        fallback_reason=fallback_reason if outcome is RouteDecisionOutcome.FALLBACK_SELECTED else None,
        outcome=outcome,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def _route_evidence_blockers(
    policy_gates: tuple[PolicyGateResult, ...] | None,
    harness_admission: HarnessAdmissionSummary | None,
    memory_context: MemoryContextSummary | None,
) -> tuple[list[str], list[RouteRejection]]:
    blockers: list[str] = []
    rejections: list[RouteRejection] = []
    _add_policy_gate_blockers(policy_gates, blockers, rejections)
    _add_harness_blockers(harness_admission, blockers, rejections)
    _add_memory_blockers(memory_context, blockers, rejections)
    return blockers, rejections


def _add_policy_gate_blockers(
    policy_gates: tuple[PolicyGateResult, ...] | None,
    blockers: list[str],
    rejections: list[RouteRejection],
) -> None:
    if not policy_gates:
        blockers.append("policy_gate_missing")
        rejections.append(_route_rejection("policy_gate_missing", "policy", "no policy gate evidence supplied"))
        return
    for gate in tuple(gate for gate in policy_gates if not gate.passed):
        blockers.append(f"policy_gate_failed:{gate.gate_id}")
        rejections.append(_route_rejection("policy_gate_failed", gate.gate_id, gate.explanation))


def _add_harness_blockers(
    harness_admission: HarnessAdmissionSummary | None,
    blockers: list[str],
    rejections: list[RouteRejection],
) -> None:
    if harness_admission is None:
        blockers.append("harness_admission_missing")
        rejections.append(
            _route_rejection("harness_admission_missing", "harness", "no harness admission evidence supplied")
        )
    elif not harness_admission.admitted:
        blockers.extend(f"harness_blocker:{blocker}" for blocker in harness_admission.blockers)
        rejections.append(
            _route_rejection("harness_admission_denied", "harness", "harness admission blocked the proposed route")
        )


def _add_memory_blockers(
    memory_context: MemoryContextSummary | None,
    blockers: list[str],
    rejections: list[RouteRejection],
) -> None:
    if memory_context is None:
        blockers.append("memory_context_missing")
        rejections.append(
            _route_rejection("memory_context_missing", "memory", "no memory eligibility summary supplied")
        )
    elif memory_context.status == "unavailable":
        blockers.append("memory_context_unavailable")
        rejections.append(
            _route_rejection("memory_context_unavailable", "memory", "memory eligibility state unavailable")
        )
    elif memory_context.eligible_count <= 0:
        blockers.append("memory_context_no_eligible_items")
        rejections.append(_route_rejection("memory_context_no_eligible_items", "memory", "no eligible memory context"))
    blockers.extend(f"memory_blocker:{signal}" for signal in (memory_context.blocked_signals if memory_context else ()))


def _estimate_blockers(
    cost_estimate: AgentRouteCostEstimate | None,
    latency_estimate: LatencyEstimate | None,
    all_candidates: tuple[RouteCandidate, ...],
) -> list[str]:
    blockers = []
    if cost_estimate is None:
        blockers.append("cost_estimate_missing")
    if latency_estimate is None:
        blockers.append("latency_estimate_missing")
    if not all_candidates:
        blockers.append("route_candidates_missing")
    return blockers


def _viable_candidates(
    all_candidates: tuple[RouteCandidate, ...],
    required: set[str],
) -> tuple[list[RouteCandidate], list[RouteRejection]]:
    viable_candidates: list[RouteCandidate] = []
    rejections: list[RouteRejection] = []
    for candidate in all_candidates:
        reasons = _candidate_rejection_reasons(candidate, required)
        if not reasons:
            viable_candidates.append(candidate)
            continue
        rejections.extend(
            RouteRejection(
                candidate_id=candidate.candidate_id,
                source_gate="capability",
                reason=reason,
                explanation=f"candidate {candidate.candidate_id!r} lacks required capability evidence",
            )
            for reason in reasons
        )
    return viable_candidates, rejections


def _blocked_route_outcome(blockers: list[str]) -> RouteDecisionOutcome:
    hard_blocker_prefixes = (
        "policy_",
        "harness_",
        "candidate_",
        "route_candidates_missing",
        "selected_candidate_unavailable",
    )
    has_hard_blocker = any(blocker.startswith(hard_blocker_prefixes) for blocker in blockers)
    has_degraded_blocker = any(
        blocker.startswith(("memory_context_unavailable", "cost_estimate_missing", "latency_estimate_missing"))
        for blocker in blockers
    )
    return (
        RouteDecisionOutcome.DENIED if has_hard_blocker or not has_degraded_blocker else RouteDecisionOutcome.DEGRADED
    )


def _select_candidate(candidates: list[RouteCandidate], selected_candidate_id: str | None) -> RouteCandidate | None:
    if selected_candidate_id is None:
        return None
    for candidate in candidates:
        if candidate.candidate_id == selected_candidate_id:
            return candidate
    return None


def _candidate_rejection_reasons(candidate: RouteCandidate, required: set[str]) -> tuple[str, ...]:
    reasons: list[str] = []
    if not candidate.capability_evidence:
        reasons.append("missing_capability_evidence")
    evidenced_capabilities = {item.capability for item in candidate.capability_evidence}
    if required and not required.issubset(evidenced_capabilities):
        reasons.append("required_capability_not_supported")
    if any(item.confidence <= 0 for item in candidate.capability_evidence):
        reasons.append("zero_confidence_capability_evidence")
    return tuple(dict.fromkeys(reasons))


def _route_rejection(reason: str, source_gate: str, explanation: str) -> RouteRejection:
    return RouteRejection(candidate_id="route", source_gate=source_gate, reason=reason, explanation=explanation)


__all__ = [
    "SCHEMA_VERSION",
    "AgentRouteCostEstimate",
    "CapabilityEvidence",
    "HarnessAdmissionSummary",
    "LatencyEstimate",
    "MemoryContextSummary",
    "PolicyGateResult",
    "RouteCandidate",
    "RouteCandidateKind",
    "RouteDecisionError",
    "RouteDecisionOutcome",
    "RouteDecisionRecord",
    "RouteRejection",
    "build_route_decision",
]
