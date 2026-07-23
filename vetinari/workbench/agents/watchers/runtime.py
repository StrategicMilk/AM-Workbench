"""Independent agent watcher runtime with policy-receipt emission."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from vetinari.workbench.gateway_policy import (
    GatewayPolicyDecision,
    GuardrailAction,
    PolicyDecisionKind,
    record_policy_decision,
)

from .events import (
    WatcherAction,
    WatcherDecision,
    WatcherDecisionReason,
    WatcherObservation,
    assess_watcher_transition,
)
from .loop_cost import (
    LoopCostReasonCode,
    LoopCostWatcherAction,
    LoopCostWatcherDecision,
    assess_loop_cost_state,
    loop_cost_input_from_metadata,
)

_WATCHER_PROFILE_ID = "agent-watcher-runtime"


class AgentWatcherRuntime:
    """Deterministic watcher runtime that assesses observations and records receipts."""

    def __init__(
        self,
        *,
        project_id: str = "default",
        receipt_store: Any | None = None,
        assessor: Callable[[WatcherObservation], WatcherDecision] = assess_watcher_transition,
        policy_kind: PolicyDecisionKind = PolicyDecisionKind.GUARDRAIL_PRE,
    ) -> None:
        self._project_id = project_id
        self._receipt_store = receipt_store
        self._assessor = assessor
        self._policy_kind = policy_kind
        self._decision_lock = threading.Lock()

    def observe(self, observation: WatcherObservation) -> WatcherDecision:
        """Assess one observation and append exactly one policy receipt.

        Returns:
            WatcherDecision value produced by observe().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(observation, WatcherObservation):
            raise TypeError(f"observe() expects WatcherObservation, got {type(observation).__name__!r}")
        with self._decision_lock:
            decision = self._loop_cost_decision_for(observation) or self._assessor(observation)
            record_policy_decision(
                self._policy_decision_for(decision),
                project_id=self._project_id,
                receipt_store=self._receipt_store,
            )
            return decision

    @staticmethod
    def _loop_cost_decision_for(observation: WatcherObservation) -> WatcherDecision | None:
        watcher_input = loop_cost_input_from_metadata(
            run_id=observation.run_id,
            actor_id=observation.actor_id,
            metadata=observation.metadata,
            evidence_refs=observation.evidence_refs,
            summary=observation.summary,
        )
        if watcher_input is None:
            return None
        return _watcher_decision_from_loop_cost(observation, assess_loop_cost_state(watcher_input))

    def _policy_decision_for(self, decision: WatcherDecision) -> GatewayPolicyDecision:
        action = _policy_action_for(decision.action, passed=decision.passed)
        reason = f"watcher_reason={decision.reason.value}; watcher_action={decision.action.value}"
        return GatewayPolicyDecision(
            decision_id=decision.decision_id,
            kind=self._policy_kind,
            passed=decision.passed,
            action=action,
            profile_id=_WATCHER_PROFILE_ID,
            lane=None,
            run_id=decision.run_id or None,
            trace_id=decision.observation_id,
            lease_id=None,
            reason=reason,
            evaluated_at_utc=decision.decided_at_utc,
            inputs_summary=_truncate(f"watcher observation {decision.observation_id}"),
            outputs_summary=_truncate(
                f"watcher_action={decision.action.value}|watcher_reason={decision.reason.value}",
            ),
            details={
                "watcher_action": decision.action.value,
                "watcher_reason": decision.reason.value,
                "watcher_degraded": decision.degraded,
                "evidence_refs": list(decision.evidence_refs),
            },
        )


def _policy_action_for(action: WatcherAction, *, passed: bool) -> GuardrailAction | None:
    if passed and action is WatcherAction.OBSERVE:
        return None
    return {
        WatcherAction.OBSERVE: GuardrailAction.LOG,
        WatcherAction.PAUSE: GuardrailAction.RETRY,
        WatcherAction.ESCALATE: GuardrailAction.HUMAN_APPROVAL,
        WatcherAction.REQUIRE_APPROVAL: GuardrailAction.HUMAN_APPROVAL,
        WatcherAction.TERMINATE: GuardrailAction.BLOCK,
    }[action]


def _watcher_decision_from_loop_cost(
    observation: WatcherObservation,
    loop_decision: LoopCostWatcherDecision,
) -> WatcherDecision:
    action = {
        LoopCostWatcherAction.OBSERVE: WatcherAction.OBSERVE,
        LoopCostWatcherAction.PAUSE: WatcherAction.PAUSE,
        LoopCostWatcherAction.DOWNGRADE: WatcherAction.ESCALATE,
        LoopCostWatcherAction.ASK_USER: WatcherAction.REQUIRE_APPROVAL,
        LoopCostWatcherAction.STRICT_BLOCK: WatcherAction.TERMINATE,
    }[loop_decision.recommended_action]
    reason = (
        WatcherDecisionReason.ALLOWED
        if loop_decision.reason_codes == (LoopCostReasonCode.ALLOWED,)
        else WatcherDecisionReason.LOOP_AMPLIFICATION
    )
    return WatcherDecision(
        decision_id=loop_decision.decision_id,
        observation_id=observation.observation_id,
        run_id=loop_decision.run_id,
        transition_kind=None,
        action=action,
        passed=loop_decision.recommended_action is LoopCostWatcherAction.OBSERVE,
        degraded=loop_decision.degraded,
        reason=reason,
        evidence_refs=loop_decision.evidence_summary.evidence_refs
        or loop_decision.evidence_summary.trace_event_refs
        or observation.evidence_refs,
        decided_at_utc=loop_decision.decided_at_utc,
        summary=loop_decision.summary,
        details={
            "actor_id": loop_decision.actor_id,
            "watcher_branch": "loop_cost_state",
            "loop_cost_decision": loop_decision.to_schema_payload(),
        },
    )


def _truncate(value: str, limit: int = 200) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


__all__ = ["AgentWatcherRuntime"]
