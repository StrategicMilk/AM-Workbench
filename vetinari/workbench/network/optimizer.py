"""Advisory network transport optimizer."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from vetinari.workbench.network.contracts import (
    NetworkApprovalPacket,
    NetworkEvidenceStatus,
    NetworkObservation,
    NetworkRecommendation,
    NetworkRoutingDecision,
    NetworkSignalKind,
    NetworkTransportPolicy,
    ProviderHealth,
    RecommendationRisk,
)

_CLOCK_SKEW_TOLERANCE_SECONDS = 60.0
logger = logging.getLogger(__name__)


def optimize_network_transport(
    *,
    observations: tuple[NetworkObservation, ...],
    policy: NetworkTransportPolicy,
    providers: tuple[ProviderHealth, ...] = (),
    now_utc: datetime | None = None,
) -> tuple[NetworkRoutingDecision, tuple[NetworkRecommendation, ...]]:
    """Return advisory decisions without mutating host or provider settings.

    Returns:
        tuple[NetworkRoutingDecision, tuple[NetworkRecommendation, ...]] value produced by optimize_network_transport().
    """
    evidence_ids = tuple(item.evidence_id for item in observations)
    if not observations or any(item.status is not NetworkEvidenceStatus.READY for item in observations):
        return (
            NetworkRoutingDecision(
                decision_id="network-route-degraded",
                provider_id="none",
                mode="fail-closed",
                backoff_seconds=policy.max_retry_backoff_seconds,
                use_cache=True,
                refresh_connectors=False,
                reasons=("network-evidence-untrusted",),
                evidence_ids=evidence_ids or ("network-evidence-missing",),
            ),
            (),
        )
    current_time = _coerce_now_utc(now_utc)
    if any(_observation_exceeds_freshness_window(item, policy, current_time) for item in observations):
        return (
            NetworkRoutingDecision(
                decision_id="network-route-degraded",
                provider_id="none",
                mode="fail-closed",
                backoff_seconds=policy.max_retry_backoff_seconds,
                use_cache=True,
                refresh_connectors=True,
                reasons=("network-evidence-stale",),
                evidence_ids=evidence_ids,
            ),
            (),
        )

    provider = _choose_provider(policy, providers)
    reasons: list[str] = []
    backoff = 0.0
    use_cache = False
    refresh_connectors = False
    recommendations: list[NetworkRecommendation] = []

    for observation in observations:
        if observation.kind is NetworkSignalKind.RATE_LIMIT and observation.value:
            backoff = policy.max_retry_backoff_seconds
            use_cache = True
            reasons.append("rate-limit-observed")
        if observation.kind is NetworkSignalKind.CACHE_FRESHNESS and str(observation.value) == "stale":
            refresh_connectors = True
            reasons.append("cache-stale")
        if (
            observation.kind is NetworkSignalKind.THROUGHPUT_MBPS
            and float(observation.value) < policy.bandwidth_budget_mbps
        ):
            use_cache = True
            reasons.append("bandwidth-budget-constrained")
        if observation.kind is NetworkSignalKind.VPN_FIREWALL and str(observation.value) != "clear":
            recommendations.append(_approval_recommendation("network-vpn-firewall", observation.evidence_id))

    if provider and (provider.rate_limited or provider.status is not NetworkEvidenceStatus.READY):
        backoff = max(backoff, policy.max_retry_backoff_seconds)
        use_cache = True
        reasons.append(f"provider-{provider.provider_id}-not-ready")

    decision = NetworkRoutingDecision(
        decision_id="network-route-advisory",
        provider_id=provider.provider_id if provider else "default",
        mode="cache-first" if use_cache else "direct",
        backoff_seconds=backoff,
        use_cache=use_cache,
        refresh_connectors=refresh_connectors,
        reasons=tuple(dict.fromkeys(reasons or ["network-evidence-ready"])),
        evidence_ids=evidence_ids,
    )
    return decision, tuple(recommendations)


def _choose_provider(policy: NetworkTransportPolicy, providers: tuple[ProviderHealth, ...]) -> ProviderHealth | None:
    if not providers:
        return None
    ready = [item for item in providers if item.status is NetworkEvidenceStatus.READY and not item.rate_limited]
    for preferred in policy.preferred_providers:
        for provider in ready:
            if provider.provider_id == preferred:
                return provider
    return min(
        ready or list(providers), key=lambda item: item.latency_ms if item.latency_ms is not None else float("inf")
    )


def _approval_recommendation(recommendation_id: str, evidence_id: str) -> NetworkRecommendation:
    packet = NetworkApprovalPacket(
        recommendation_id=recommendation_id,
        risk=RecommendationRisk.NEEDS_APPROVAL,
        explanation="Network environment suggests a risky transport change; caller approval is required before action.",
        rollback_guidance="Keep the current transport path until a caller-owned approval record binds this recommendation.",
        before_after_evidence_required=("latency_ms", "packet_loss", "provider_health"),
    )
    return NetworkRecommendation(
        recommendation_id=recommendation_id,
        title="Review network transport constraint",
        risk=RecommendationRisk.NEEDS_APPROVAL,
        action="request-caller-approval",
        explanation="The optimizer is advisory only and does not mutate host or provider settings.",
        evidence_ids=(evidence_id,),
        approval_packet=packet,
    )


def _observation_exceeds_freshness_window(
    observation: NetworkObservation,
    policy: NetworkTransportPolicy,
    now_utc: datetime,
) -> bool:
    measured: datetime | None
    try:
        measured = datetime.fromisoformat(observation.measured_at_utc.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        logger.warning(
            "Network observation %s has invalid measured_at_utc=%r; treating evidence as stale",
            observation.evidence_id,
            observation.measured_at_utc,
        )
        measured = None
    if measured is None:
        return True
    age_seconds = (now_utc - measured).total_seconds()
    if age_seconds < -_CLOCK_SKEW_TOLERANCE_SECONDS:
        return True
    return age_seconds > policy.stale_after_seconds


def _coerce_now_utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


__all__ = ["optimize_network_transport"]
