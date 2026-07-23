"""Side-effect-free governance mode projection for current Workbench actions."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from vetinari.workbench.agents.watchers.loop_cost import LoopCostWatcherAction, LoopCostWatcherDecision
from vetinari.workbench.policy.risk_context import RiskContext, RiskContextDecision
from vetinari.workbench.policy.verdicts import ActionVerdict, VerdictValue
from vetinari.workbench.shields import ShieldDecision, ShieldDecisionValue

from .contracts import GovernanceEnforcementEffect, GovernanceMode, GovernanceModeDecision, GovernanceModeError


def apply_governance_mode(
    *,
    mode: GovernanceMode | str,
    verdict: ActionVerdict,
    shield_decision: ShieldDecision | None = None,
    watcher_decision: LoopCostWatcherDecision | None = None,
    risk_context: RiskContext | None = None,
    decision_id: str | None = None,
    evaluated_at_utc: str | None = None,
) -> GovernanceModeDecision:
    """Project upstream safety outputs into one current-action governance decision.

    Returns:
        GovernanceModeDecision value produced by apply_governance_mode().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    governance_mode = GovernanceMode(mode)
    if governance_mode is GovernanceMode.RETROSPECTIVE_SCAN:
        raise GovernanceModeError(
            "retrospective_scan_not_current_decision",
            "use run_retrospective_policy_scan for historical replay",
        )

    severe = _has_current_block_or_escalation(verdict, shield_decision, watcher_decision, risk_context)
    warned = _has_current_warning(verdict, shield_decision, watcher_decision, risk_context)
    missing_safety_inputs = shield_decision is None or watcher_decision is None

    if governance_mode is GovernanceMode.STRICT:
        effect = _strict_effect(verdict, shield_decision, watcher_decision, risk_context, missing_safety_inputs)
        advisory_only = False
        enforced = severe or missing_safety_inputs
    elif governance_mode is GovernanceMode.WARN:
        effect = (
            GovernanceEnforcementEffect.ADVISORY_WARNING
            if severe or warned or missing_safety_inputs
            else GovernanceEnforcementEffect.ALLOW
        )
        advisory_only = True
        enforced = False
    else:
        effect = (
            GovernanceEnforcementEffect.ADVISORY_WARNING
            if severe or warned or missing_safety_inputs
            else GovernanceEnforcementEffect.ALLOW
        )
        advisory_only = True
        enforced = False

    evidence_refs = _evidence_refs(verdict, shield_decision, watcher_decision, risk_context)
    shield_version = shield_decision.policy_version if shield_decision is not None else "missing-shield-decision"
    return GovernanceModeDecision(
        decision_id=decision_id or f"governance-decision-{uuid4().hex}",
        mode=governance_mode,
        enforcement_effect=effect,
        verdict_value=verdict.value.value,
        upstream_verdict_id=verdict.verdict_id,
        action_id=verdict.action_id,
        run_id=verdict.run_id,
        policy_version=verdict.policy_version,
        shield_version=shield_version,
        evidence_refs=evidence_refs,
        advisory_only=advisory_only,
        enforced=enforced,
        history_mutated=False,
        summary=_summary(governance_mode, effect, verdict, shield_decision, missing_safety_inputs),
        shield_decision_value=shield_decision.value.value if shield_decision is not None else "",
        watcher_action=watcher_decision.recommended_action.value if watcher_decision is not None else "",
        risk_context_decision=risk_context.decision.value if risk_context is not None else "",
        evaluated_at_utc=evaluated_at_utc or _utc_now_iso(),
    )


def _strict_effect(
    verdict: ActionVerdict,
    shield_decision: ShieldDecision | None,
    watcher_decision: LoopCostWatcherDecision | None,
    risk_context: RiskContext | None,
    missing_safety_inputs: bool,
) -> GovernanceEnforcementEffect:
    if _blocks(verdict, shield_decision, watcher_decision, risk_context):
        return GovernanceEnforcementEffect.BLOCKED
    if _requires_review(verdict, shield_decision, watcher_decision, risk_context):
        return GovernanceEnforcementEffect.REQUIRES_REVIEW
    if missing_safety_inputs:
        return GovernanceEnforcementEffect.REQUIRES_REVIEW
    return GovernanceEnforcementEffect.ALLOW


def _has_current_block_or_escalation(
    verdict: ActionVerdict,
    shield_decision: ShieldDecision | None,
    watcher_decision: LoopCostWatcherDecision | None,
    risk_context: RiskContext | None,
) -> bool:
    return _blocks(verdict, shield_decision, watcher_decision, risk_context) or _requires_review(
        verdict,
        shield_decision,
        watcher_decision,
        risk_context,
    )


def _blocks(
    verdict: ActionVerdict,
    shield_decision: ShieldDecision | None,
    watcher_decision: LoopCostWatcherDecision | None,
    risk_context: RiskContext | None,
) -> bool:
    if verdict.value is VerdictValue.BLOCK:
        return True
    if shield_decision is not None and shield_decision.value is ShieldDecisionValue.BLOCK:
        return True
    if watcher_decision is not None and watcher_decision.recommended_action is LoopCostWatcherAction.STRICT_BLOCK:
        return True
    return risk_context is not None and risk_context.decision is RiskContextDecision.DENY


def _requires_review(
    verdict: ActionVerdict,
    shield_decision: ShieldDecision | None,
    watcher_decision: LoopCostWatcherDecision | None,
    risk_context: RiskContext | None,
) -> bool:
    if verdict.value is VerdictValue.ESCALATE:
        return True
    if shield_decision is not None and shield_decision.value is ShieldDecisionValue.ESCALATE:
        return True
    if watcher_decision is not None and watcher_decision.recommended_action in {
        LoopCostWatcherAction.ASK_USER,
        LoopCostWatcherAction.PAUSE,
    }:
        return True
    return risk_context is not None and risk_context.decision in {
        RiskContextDecision.REQUIRE_APPROVAL,
        RiskContextDecision.DEGRADED,
    }


def _has_current_warning(
    verdict: ActionVerdict,
    shield_decision: ShieldDecision | None,
    watcher_decision: LoopCostWatcherDecision | None,
    risk_context: RiskContext | None,
) -> bool:
    if verdict.value is VerdictValue.WARN:
        return True
    if shield_decision is not None and shield_decision.value in {
        ShieldDecisionValue.WARN,
        ShieldDecisionValue.DEGRADED,
    }:
        return True
    if watcher_decision is not None and watcher_decision.recommended_action is LoopCostWatcherAction.DOWNGRADE:
        return True
    return risk_context is not None and risk_context.decision is RiskContextDecision.WARN


def _evidence_refs(
    verdict: ActionVerdict,
    shield_decision: ShieldDecision | None,
    watcher_decision: LoopCostWatcherDecision | None,
    risk_context: RiskContext | None,
) -> tuple[str, ...]:
    refs: list[str] = [link.ref for link in verdict.evidence_links]
    if shield_decision is not None:
        refs.extend(shield_decision.evidence_refs)
    if watcher_decision is not None:
        refs.extend(watcher_decision.evidence_summary.evidence_refs)
        refs.extend(watcher_decision.evidence_summary.trace_event_refs)
        refs.extend(watcher_decision.evidence_summary.policy_verdict_refs)
        refs.extend(watcher_decision.evidence_summary.predicate_refs)
    if risk_context is not None:
        refs.extend(str(link.get("ref", "")) for link in risk_context.evidence_links)
    clean = tuple(dict.fromkeys(ref for ref in refs if str(ref).strip()))
    return clean or (f"verdict:{verdict.verdict_id}",)


def _summary(
    mode: GovernanceMode,
    effect: GovernanceEnforcementEffect,
    verdict: ActionVerdict,
    shield_decision: ShieldDecision | None,
    missing_safety_inputs: bool,
) -> str:
    shield = shield_decision.value.value if shield_decision is not None else "missing"
    suffix = (
        "; missing upstream safety input fails closed"
        if missing_safety_inputs and mode is GovernanceMode.STRICT
        else ""
    )
    return f"{mode.value} projected verdict={verdict.value.value}, shield={shield}, effect={effect.value}{suffix}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
