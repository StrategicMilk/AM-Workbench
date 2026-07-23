"""Deterministic watcher observations and fail-closed transition assessment."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


class WatcherTransitionKind(str, Enum):
    """Transition categories observed independently from the acting agent."""

    SHELL = "shell"
    FILE = "file"
    NETWORK = "network"
    TOOL = "tool"
    MEMORY = "memory"
    USAGE = "token"
    COST = "cost"
    LOOP = "loop"
    SIDE_EFFECT = "side_effect"
    PERMISSION = "permission"


class WatcherAction(str, Enum):
    """Advisory actions returned to future orchestrators for enforcement."""

    OBSERVE = "observe"
    PAUSE = "pause"
    ESCALATE = "escalate"
    REQUIRE_APPROVAL = "require_approval"
    TERMINATE = "terminate"


class WatcherDecisionReason(str, Enum):
    """Machine-readable reasons a watcher transition did not pass."""

    ALLOWED = "allowed"
    UNKNOWN_TRANSITION_KIND = "unknown_transition_kind"
    MISSING_RUN_ID = "missing_run_id"
    MISSING_ACTOR_ID = "missing_actor_id"
    MISSING_EVIDENCE = "missing_evidence"
    UNREADABLE_USAGE_BUDGET = "unreadable_token_budget"
    UNREADABLE_COST_BUDGET = "unreadable_cost_budget"
    USAGE_BUDGET_EXCEEDED = "token_budget_exceeded"
    COST_BUDGET_EXCEEDED = "cost_budget_exceeded"
    LOOP_AMPLIFICATION = "loop_amplification"
    MISSING_EXPECTED_SIDE_EFFECT = "missing_expected_side_effect"
    MISSING_AUTHORITY = "missing_authority"


@dataclass(frozen=True, slots=True)
class WatcherObservation:
    """One immutable runtime transition observed by a watcher."""

    observation_id: str
    run_id: str
    actor_id: str
    transition_kind: WatcherTransitionKind | str
    evidence_refs: tuple[str, ...]
    observed_at_utc: str
    summary: str = ""
    tool_name: str = ""
    workspace_path: str = ""
    network_endpoint: str = ""
    memory_scope: str = ""
    token_budget_remaining: float | int | str | None = None
    cost_budget_remaining: float | int | str | None = None
    loop_iteration: int | str | None = None
    loop_limit: int | str | None = None
    expected_side_effect_refs: tuple[str, ...] = ()
    observed_side_effect_refs: tuple[str, ...] = ()
    authority_refs: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WatcherObservation(observation_id={self.observation_id!r}, run_id={self.run_id!r}, actor_id={self.actor_id!r})"


@dataclass(frozen=True, slots=True)
class WatcherDecision:
    """Deterministic watcher verdict for one observation."""

    decision_id: str
    observation_id: str
    run_id: str
    transition_kind: WatcherTransitionKind | None
    action: WatcherAction
    passed: bool
    degraded: bool
    reason: WatcherDecisionReason
    evidence_refs: tuple[str, ...]
    decided_at_utc: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WatcherDecision(decision_id={self.decision_id!r}, observation_id={self.observation_id!r}, run_id={self.run_id!r})"


def assess_watcher_transition(observation: WatcherObservation) -> WatcherDecision:
    """Assess one watcher observation, failing closed for unavailable proof.

    Returns:
        WatcherDecision value produced by assess_watcher_transition().
    """
    kind = _coerce_transition_kind(observation.transition_kind)
    evidence_refs = _clean_tuple(observation.evidence_refs)
    required_decision = _required_field_decision(observation, kind, evidence_refs)
    if required_decision is not None:
        return required_decision
    assert kind is not None
    budget_decision = _budget_decision(observation, kind, evidence_refs)
    if budget_decision is not None:
        return budget_decision
    transition_decision = _transition_guard_decision(observation, kind, evidence_refs)
    if transition_decision is not None:
        return transition_decision
    return _decision(
        observation,
        kind=kind,
        action=WatcherAction.OBSERVE,
        passed=True,
        reason=WatcherDecisionReason.ALLOWED,
        evidence_refs=evidence_refs,
    )


def _required_field_decision(
    observation: WatcherObservation,
    kind: WatcherTransitionKind | None,
    evidence_refs: tuple[str, ...],
) -> WatcherDecision | None:
    if kind is None:
        return _decision(
            observation,
            kind=None,
            action=WatcherAction.TERMINATE,
            passed=False,
            reason=WatcherDecisionReason.UNKNOWN_TRANSITION_KIND,
            evidence_refs=evidence_refs,
        )
    if not _has_text(observation.run_id):
        return _decision(
            observation,
            kind=kind,
            action=WatcherAction.TERMINATE,
            passed=False,
            reason=WatcherDecisionReason.MISSING_RUN_ID,
            evidence_refs=evidence_refs,
        )
    if not _has_text(observation.actor_id):
        return _decision(
            observation,
            kind=kind,
            action=WatcherAction.ESCALATE,
            passed=False,
            reason=WatcherDecisionReason.MISSING_ACTOR_ID,
            evidence_refs=evidence_refs,
        )
    if not evidence_refs:
        return _decision(
            observation,
            kind=kind,
            action=WatcherAction.REQUIRE_APPROVAL,
            passed=False,
            reason=WatcherDecisionReason.MISSING_EVIDENCE,
            evidence_refs=evidence_refs,
        )
    return None


def _transition_guard_decision(
    observation: WatcherObservation,
    kind: WatcherTransitionKind,
    evidence_refs: tuple[str, ...],
) -> WatcherDecision | None:
    if kind is WatcherTransitionKind.LOOP and _loop_amplified(observation):
        return _decision(
            observation,
            kind=kind,
            action=WatcherAction.TERMINATE,
            passed=False,
            reason=WatcherDecisionReason.LOOP_AMPLIFICATION,
            evidence_refs=evidence_refs,
        )
    if kind is WatcherTransitionKind.SIDE_EFFECT and not _expected_side_effect_observed(observation):
        return _decision(
            observation,
            kind=kind,
            action=WatcherAction.REQUIRE_APPROVAL,
            passed=False,
            reason=WatcherDecisionReason.MISSING_EXPECTED_SIDE_EFFECT,
            evidence_refs=evidence_refs,
        )
    if kind is WatcherTransitionKind.PERMISSION and not _clean_tuple(observation.authority_refs):
        return _decision(
            observation,
            kind=kind,
            action=WatcherAction.ESCALATE,
            passed=False,
            reason=WatcherDecisionReason.MISSING_AUTHORITY,
            evidence_refs=evidence_refs,
        )
    return None


def _decision(
    observation: WatcherObservation,
    *,
    kind: WatcherTransitionKind | None,
    action: WatcherAction,
    passed: bool,
    reason: WatcherDecisionReason,
    evidence_refs: tuple[str, ...],
) -> WatcherDecision:
    return WatcherDecision(
        decision_id=f"watcher-decision-{uuid4().hex}",
        observation_id=observation.observation_id,
        run_id=observation.run_id,
        transition_kind=kind,
        action=action,
        passed=passed,
        degraded=not passed,
        reason=reason,
        evidence_refs=evidence_refs,
        decided_at_utc=datetime.now(timezone.utc).isoformat(),
        summary=observation.summary or f"{kind.value if kind else 'unknown'} watcher transition",
        details={
            "actor_id": observation.actor_id,
            "tool_name": observation.tool_name,
            "workspace_path": observation.workspace_path,
            "network_endpoint": observation.network_endpoint,
            "memory_scope": observation.memory_scope,
        },
    )


def _budget_decision(
    observation: WatcherObservation,
    kind: WatcherTransitionKind,
    evidence_refs: tuple[str, ...],
) -> WatcherDecision | None:
    if kind is WatcherTransitionKind.USAGE:
        token_budget = _coerce_number(observation.token_budget_remaining)
        if token_budget is None:
            return _decision(
                observation,
                kind=kind,
                action=WatcherAction.REQUIRE_APPROVAL,
                passed=False,
                reason=WatcherDecisionReason.UNREADABLE_USAGE_BUDGET,
                evidence_refs=evidence_refs,
            )
        if token_budget < 0:
            return _decision(
                observation,
                kind=kind,
                action=WatcherAction.PAUSE,
                passed=False,
                reason=WatcherDecisionReason.USAGE_BUDGET_EXCEEDED,
                evidence_refs=evidence_refs,
            )
    if kind is WatcherTransitionKind.COST:
        cost_budget = _coerce_number(observation.cost_budget_remaining)
        if cost_budget is None:
            return _decision(
                observation,
                kind=kind,
                action=WatcherAction.REQUIRE_APPROVAL,
                passed=False,
                reason=WatcherDecisionReason.UNREADABLE_COST_BUDGET,
                evidence_refs=evidence_refs,
            )
        if cost_budget < 0:
            return _decision(
                observation,
                kind=kind,
                action=WatcherAction.PAUSE,
                passed=False,
                reason=WatcherDecisionReason.COST_BUDGET_EXCEEDED,
                evidence_refs=evidence_refs,
            )
    return None


def _coerce_transition_kind(value: WatcherTransitionKind | str) -> WatcherTransitionKind | None:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return value if isinstance(value, WatcherTransitionKind) else WatcherTransitionKind(raw_value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None


def _clean_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        return ()
    return tuple(str(value) for value in values if str(value).strip())


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _coerce_number(value: float | int | str | None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None
    return number if isfinite(number) else None


def _loop_amplified(observation: WatcherObservation) -> bool:
    iteration = _coerce_number(observation.loop_iteration)
    limit = _coerce_number(observation.loop_limit)
    return iteration is None or limit is None or limit <= 0 or iteration > limit


def _expected_side_effect_observed(observation: WatcherObservation) -> bool:
    expected = set(_clean_tuple(observation.expected_side_effect_refs))
    observed = set(_clean_tuple(observation.observed_side_effect_refs))
    return bool(expected) and expected <= observed


__all__ = [
    "WatcherAction",
    "WatcherDecision",
    "WatcherDecisionReason",
    "WatcherObservation",
    "WatcherTransitionKind",
    "assess_watcher_transition",
]
