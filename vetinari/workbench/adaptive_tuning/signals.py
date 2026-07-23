"""Normalize dependency and local signals into adaptive tuning evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from vetinari.workbench.adaptive_tuning.contracts import (
    AdaptationTarget,
    EvidenceBlocker,
    EvidenceScope,
    EvidenceStatus,
    FrictionObservation,
    FrictionSignalKind,
    NormalizedEvidence,
)
from vetinari.workbench.adaptive_tuning.policy import AdaptiveTuningPolicy, observation_blockers
from vetinari.workbench.hardware import OptimizationProposal
from vetinari.workbench.network import NetworkRoutingDecision
from vetinari.workbench.preferences import PreferenceCard
from vetinari.workbench.user_observability import UserInputSignal, UserSignalKind


def normalize_observation(
    observation: FrictionObservation,
    policy: AdaptiveTuningPolicy,
    *,
    now_utc: datetime | None = None,
) -> NormalizedEvidence:
    """Normalize one raw observation and keep rejection reasons explicit.

    Args:
        observation: Observation value consumed by normalize_observation().
        policy: Policy value consumed by normalize_observation().
        now_utc: Now utc value consumed by normalize_observation().

    Returns:
        Normalized observation value.
    """
    blockers = observation_blockers(observation, policy, now_utc=now_utc)
    status = EvidenceStatus.REJECTED if blockers else EvidenceStatus.ACCEPTED
    return NormalizedEvidence(
        evidence_id=observation.observation_id,
        kind=observation.kind,
        summary=observation.summary,
        scope=observation.scope,
        observed_at_utc=observation.observed_at_utc,
        confidence=observation.confidence,
        evidence_refs=observation.evidence_refs,
        provenance_ref=observation.provenance_ref,
        status=status,
        blockers=blockers,
        target=observation.target,
    )


def normalize_user_input_signal(
    signal: UserInputSignal,
    policy: AdaptiveTuningPolicy,
    *,
    now_utc: datetime | None = None,
) -> NormalizedEvidence:
    """Normalize the canonical user-observability signal type.

    Args:
        signal: Signal value consumed by normalize_user_input_signal().
        policy: Policy value consumed by normalize_user_input_signal().
        now_utc: Now utc value consumed by normalize_user_input_signal().

    Returns:
        Normalized user input signal value.
    """
    kind = _signal_kind_from_user_signal(signal.kind)
    observation = FrictionObservation(
        observation_id=signal.signal_id,
        kind=kind,
        summary=signal.summary,
        scope=EvidenceScope(project_id=signal.project_id, surface=signal.target_ref or "workbench"),
        observed_at_utc=signal.captured_at_utc,
        confidence=float(signal.confidence),
        evidence_refs=tuple(signal.evidence_refs),
        provenance_ref=signal.provenance_ref,
        private=signal.contains_sensitive_data,
        denied=not bool(signal.authority_ref.strip()) and signal.high_impact,
        target=AdaptationTarget.AUTOMATION if signal.high_impact else AdaptationTarget.LOCAL_UI_DEFAULT,
    )
    return normalize_observation(observation, policy, now_utc=now_utc)


def normalize_preference_card(
    card: PreferenceCard,
    policy: AdaptiveTuningPolicy,
    *,
    now_utc: datetime | None = None,
) -> NormalizedEvidence:
    """Normalize preference-card concepts as scoped evidence only.

    Args:
        card: Card value consumed by normalize_preference_card().
        policy: Policy value consumed by normalize_preference_card().
        now_utc: Now utc value consumed by normalize_preference_card().

    Returns:
        Normalized preference card value.
    """
    latest = card.evidence[-1]
    observation = FrictionObservation(
        observation_id=f"preference:{card.card_id}",
        kind=FrictionSignalKind.PROMPT_CORRECTION,
        summary=card.statement,
        scope=EvidenceScope(project_id=card.scope.project_id or "global", surface=card.scope.scope_type.value),
        observed_at_utc=latest.observed_at_utc,
        confidence=float(card.confidence),
        evidence_refs=tuple(item.evidence_id for item in card.evidence),
        provenance_ref=latest.source,
        denied=not card.consent_granted,
        target=AdaptationTarget.LOCAL_UI_DEFAULT,
    )
    return normalize_observation(observation, policy, now_utc=now_utc)


def normalize_hardware_proposal(
    proposal: OptimizationProposal,
    policy: AdaptiveTuningPolicy,
    *,
    project_id: str = "default",
    now_utc: datetime | None = None,
) -> NormalizedEvidence:
    """Normalize hardware optimizer output as advisory evidence.

    Args:
        proposal: Proposal value consumed by normalize_hardware_proposal().
        policy: Policy value consumed by normalize_hardware_proposal().
        project_id: Project identifier that scopes the operation.
        now_utc: Now utc value consumed by normalize_hardware_proposal().

    Returns:
        Normalized hardware proposal value.
    """
    evidence_ids = tuple(getattr(proposal, "evidence_ids", ())) or (f"hardware:{proposal.proposal_id}",)
    observation = FrictionObservation(
        observation_id=f"hardware:{proposal.proposal_id}",
        kind=FrictionSignalKind.HARDWARE_ADVISORY,
        summary=proposal.title,
        scope=EvidenceScope(project_id=project_id, surface="hardware-advisory"),
        observed_at_utc=_now_iso(now_utc),
        confidence=0.75,
        evidence_refs=evidence_ids,
        provenance_ref="hardware-digital-twin",
        target=AdaptationTarget.RESOURCE_POLICY,
    )
    return normalize_observation(observation, policy, now_utc=now_utc)


def normalize_network_decision(
    decision: NetworkRoutingDecision,
    policy: AdaptiveTuningPolicy,
    *,
    project_id: str = "default",
    now_utc: datetime | None = None,
) -> NormalizedEvidence:
    """Normalize network routing output as advisory evidence.

    Args:
        decision: Decision value consumed by normalize_network_decision().
        policy: Policy value consumed by normalize_network_decision().
        project_id: Project identifier that scopes the operation.
        now_utc: Now utc value consumed by normalize_network_decision().

    Returns:
        Normalized network decision value.
    """
    observation = FrictionObservation(
        observation_id=f"network:{decision.decision_id}",
        kind=FrictionSignalKind.NETWORK_ADVISORY,
        summary=f"{decision.provider_id} {decision.mode}",
        scope=EvidenceScope(project_id=project_id, surface="network-advisory"),
        observed_at_utc=_now_iso(now_utc),
        confidence=0.75,
        evidence_refs=tuple(decision.evidence_ids),
        provenance_ref="network-transport-optimizer",
        target=AdaptationTarget.NETWORK_ROUTE,
    )
    return normalize_observation(observation, policy, now_utc=now_utc)


def normalize_any_signal(
    value: Any, policy: AdaptiveTuningPolicy, *, now_utc: datetime | None = None
) -> NormalizedEvidence:
    """Normalize a supported canonical dependency object.

    Args:
        value: Value processed by the operation.
        policy: Policy value consumed by normalize_any_signal().
        now_utc: Now utc value consumed by normalize_any_signal().

    Returns:
        Normalized any signal value.
    """
    if isinstance(value, FrictionObservation):
        return normalize_observation(value, policy, now_utc=now_utc)
    if isinstance(value, UserInputSignal):
        return normalize_user_input_signal(value, policy, now_utc=now_utc)
    if isinstance(value, PreferenceCard):
        return normalize_preference_card(value, policy, now_utc=now_utc)
    if isinstance(value, OptimizationProposal):
        return normalize_hardware_proposal(value, policy, now_utc=now_utc)
    if isinstance(value, NetworkRoutingDecision):
        return normalize_network_decision(value, policy, now_utc=now_utc)
    return NormalizedEvidence(
        evidence_id="unsupported",
        kind=FrictionSignalKind.PROMPT_CORRECTION,
        summary="unsupported signal",
        scope=None,
        observed_at_utc=_now_iso(now_utc),
        confidence=0.0,
        evidence_refs=(),
        provenance_ref="",
        status=EvidenceStatus.REJECTED,
        blockers=(EvidenceBlocker.UNREADABLE_EVIDENCE,),
    )


def _signal_kind_from_user_signal(kind: UserSignalKind | str) -> FrictionSignalKind:
    value = kind.value if isinstance(kind, UserSignalKind) else str(kind)
    if value == UserSignalKind.REPEATED_WORKFLOW.value:
        return FrictionSignalKind.REOPENED_FLOW
    if value == UserSignalKind.PREFERENCE_CANDIDATE.value:
        return FrictionSignalKind.PROMPT_CORRECTION
    if value == UserSignalKind.EFFORT_ACCOUNTING.value:
        return FrictionSignalKind.LONG_PAUSE
    return FrictionSignalKind.WRONG_MENU_NAVIGATION


def _now_iso(value: datetime | None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
