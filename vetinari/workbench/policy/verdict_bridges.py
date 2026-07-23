"""Private adapters between Workbench policy verdicts and adjacent systems."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from vetinari.workbench.gateway_policy import GatewayPolicyDecision, GuardrailAction, PolicyDecisionKind


def _decision_kind(domain: Any) -> PolicyDecisionKind:
    if getattr(domain, "value", domain) in {"token_budget", "cost_budget"}:
        return PolicyDecisionKind.BUDGET
    return PolicyDecisionKind.GUARDRAIL_PRE


def _guardrail_action(value: Any) -> GuardrailAction | None:
    raw = getattr(value, "value", value)
    if raw == "allow":
        return None
    if raw == "warn":
        return GuardrailAction.LOG
    if raw == "block":
        return GuardrailAction.BLOCK
    return GuardrailAction.HUMAN_APPROVAL


def _first_ref(evidence_links: tuple[Any, ...], kind: str) -> str | None:
    for link in evidence_links:
        if getattr(link, "kind", None) == kind:
            return str(link.ref)
    return None


def _gateway_value(decision: GatewayPolicyDecision) -> str:
    if decision.passed:
        return "allow"
    if decision.action is GuardrailAction.BLOCK:
        return "block"
    if decision.action is GuardrailAction.HUMAN_APPROVAL:
        return "escalate"
    return "warn"


def _verdict_from_watcher_decision_impl(
    decision: Any,
    *,
    policy_version: str,
    evidence_links: tuple[Any, ...],
) -> Any:
    from vetinari.workbench.policy.verdicts import (
        _SCHEMA_VERSION,
        ActionVerdict,
        PolicyMode,
        ReplayMetadata,
        VerdictValue,
        _utc_now_iso,
    )

    risk_domain = _watcher_risk_domain(decision.transition_kind)
    value = VerdictValue(_watcher_value(decision.action, decision.passed))
    reason = _watcher_reason(decision.reason, value)
    return ActionVerdict(
        verdict_id=f"verdict-{uuid4().hex}",
        value=value,
        mode=PolicyMode.STRICT,
        risk_domain=risk_domain,
        reason_code=reason,
        action_id=str(decision.observation_id),
        actor_id=str(decision.details.get("actor_id", "watcher")),
        run_id=str(decision.run_id),
        evidence_links=evidence_links,
        replay_metadata=ReplayMetadata(
            schema_version=_SCHEMA_VERSION,
            policy_version=policy_version,
            mode=PolicyMode.STRICT.value,
            captured_at_utc=_utc_now_iso(),
        ),
        policy_version=policy_version,
        evaluated_at_utc=str(decision.decided_at_utc),
        summary=str(decision.summary),
        details={"source": "watcher_decision", "decision_id": str(decision.decision_id)},
    )


def _verdict_from_gateway_policy_decision_impl(
    decision: GatewayPolicyDecision,
    *,
    risk_domain: Any,
    policy_version: str,
    evidence_links: tuple[Any, ...],
) -> Any:
    from vetinari.workbench.policy.verdicts import (
        _SCHEMA_VERSION,
        ActionVerdict,
        PolicyMode,
        PolicyReasonCode,
        ReplayMetadata,
        VerdictValue,
        _utc_now_iso,
    )

    value = VerdictValue(_gateway_value(decision))
    reason = PolicyReasonCode.ALLOWED if value is VerdictValue.ALLOW else PolicyReasonCode.POLICY_MODE_STRICT_BLOCKED
    return ActionVerdict(
        verdict_id=f"verdict-{uuid4().hex}",
        value=value,
        mode=PolicyMode.STRICT,
        risk_domain=risk_domain,
        reason_code=reason,
        action_id=decision.decision_id,
        actor_id="workbench-gateway-policy",
        run_id=decision.run_id or "gateway-policy",
        evidence_links=evidence_links,
        replay_metadata=ReplayMetadata(
            schema_version=_SCHEMA_VERSION,
            policy_version=policy_version,
            mode=PolicyMode.STRICT.value,
            captured_at_utc=_utc_now_iso(),
        ),
        policy_version=policy_version,
        evaluated_at_utc=decision.evaluated_at_utc,
        summary=decision.outputs_summary,
        details={"source": "gateway_policy_decision", "kind": decision.kind.value},
    )


def _make_verdict_impl(
    action: Any,
    risk_domain: Any,
    value: Any,
    reason: Any,
    replay: Any,
    policy: dict[str, Any],
) -> Any:
    from uuid import uuid4

    from vetinari.workbench.policy.verdicts import ActionVerdict, PolicyMode, _utc_now_iso

    return ActionVerdict(
        verdict_id=f"verdict-{uuid4().hex}",
        value=value,
        mode=PolicyMode(replay.mode),
        risk_domain=risk_domain,
        reason_code=reason,
        action_id=action.action_id,
        actor_id=action.actor_id,
        run_id=action.run_id,
        evidence_links=action.evidence_links,
        replay_metadata=replay,
        policy_version=str(policy["policy_version"]),
        evaluated_at_utc=_utc_now_iso(),
        summary=action.summary,
        details={**action.details, "action_type": action.action_type},
    )


def _matches_blocked_rule_impl(
    action: Any, policy: dict[str, Any] | tuple[str, ...], risk_domain: Any | None = None
) -> bool:
    if isinstance(policy, tuple):
        blocked_patterns = tuple(str(item).lower() for item in policy)
    else:
        domain = risk_domain if risk_domain is not None else getattr(action, "risk_domain", None)
        rule = policy.get("domain_rules", {}).get(getattr(domain, "value", domain), {})
        blocked_patterns = tuple(str(item).lower() for item in rule.get("blocked_patterns", ()))
    target_type = action.action_type.lower()
    target_summary = action.summary.lower()
    return any(pattern and (pattern in target_type or pattern in target_summary) for pattern in blocked_patterns)


def _watcher_risk_domain(kind: Any) -> Any:
    from vetinari.workbench.policy.verdicts import RiskDomain

    value = getattr(kind, "value", kind)
    mapping = {
        "shell": RiskDomain.SHELL,
        "file": RiskDomain.FILE_SYSTEM,
        "network": RiskDomain.NETWORK,
        "tool": RiskDomain.TOOL_INVOCATION,
        "memory": RiskDomain.MEMORY_SCOPE,
        "token": RiskDomain.USAGE_BUDGET,
        "cost": RiskDomain.COST_BUDGET,
        "loop": RiskDomain.LOOP_AMPLIFICATION,
        "side_effect": RiskDomain.SIDE_EFFECT,
        "permission": RiskDomain.PERMISSION,
    }
    return mapping.get(str(value), RiskDomain.UNKNOWN)


def _watcher_value(action: Any, passed: bool) -> str:
    value = getattr(action, "value", action)
    if passed or value == "observe":
        return "allow"
    if value == "terminate":
        return "block"
    if value in {"escalate", "require_approval"}:
        return "escalate"
    return "warn"


def _watcher_reason(reason: Any, value: Any) -> Any:
    from vetinari.workbench.policy.verdicts import PolicyReasonCode, VerdictValue

    raw = str(getattr(reason, "value", reason))
    mapping = {
        "allowed": PolicyReasonCode.ALLOWED,
        "missing_run_id": PolicyReasonCode.MISSING_RUN_ID,
        "missing_actor_id": PolicyReasonCode.MISSING_ACTOR_ID,
        "missing_evidence": PolicyReasonCode.MISSING_EVIDENCE,
        "missing_authority": PolicyReasonCode.MISSING_AUTHORITY,
        "unknown_transition_kind": PolicyReasonCode.UNKNOWN_RISK_DOMAIN,
    }
    if raw in mapping:
        return mapping[raw]
    if value is VerdictValue.ESCALATE:
        return PolicyReasonCode.ESCALATED_TO_USER
    if value is VerdictValue.WARN:
        return PolicyReasonCode.POLICY_MODE_WARN_WARNED
    if value is VerdictValue.BLOCK:
        return PolicyReasonCode.POLICY_MODE_STRICT_BLOCKED
    return PolicyReasonCode.ALLOWED
