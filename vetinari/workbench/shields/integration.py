"""Read-only integration between shields and existing Workbench authorities."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from vetinari.workbench.capability_packs import CapabilityEnablementDecision
from vetinari.workbench.policy.verdicts import (
    ActionInput,
    EvidenceLink,
    RiskDomain,
    VerdictValue,
    classify_action,
)
from vetinari.workbench.shields.contracts import (
    ShieldDecision,
    ShieldDecisionValue,
    ShieldEvaluationRequest,
    ShieldRiskDomain,
)
from vetinari.workbench.shields.runtime import WorkbenchShieldRuntime
from vetinari.workbench.tool_trust.contracts import ToolSurfaceTrustDecision

logger = logging.getLogger(__name__)


_POLICY_DOMAIN_BY_SHIELD_DOMAIN = {
    ShieldRiskDomain.DLP: RiskDomain.MEMORY_SCOPE,
    ShieldRiskDomain.SHELL_SAFETY: RiskDomain.SHELL,
    ShieldRiskDomain.GIT_RELEASE_SAFETY: RiskDomain.APPROVAL,
    ShieldRiskDomain.BROWSER_NETWORK_EGRESS: RiskDomain.NETWORK,
    ShieldRiskDomain.PACKAGE_INSTALL: RiskDomain.TOOL_INVOCATION,
    ShieldRiskDomain.SECRETS: RiskDomain.MEMORY_SCOPE,
    ShieldRiskDomain.DESTRUCTIVE_FILESYSTEM: RiskDomain.FILE_SYSTEM,
    ShieldRiskDomain.MCP_PROMPT_INJECTION: RiskDomain.TOOL_INVOCATION,
    ShieldRiskDomain.PUBLIC_EXPORT_BOUNDARY: RiskDomain.PERMISSION,
    ShieldRiskDomain.MOBILE_REMOTE_CONTROL: RiskDomain.REMOTE_CONTROL,
}


def policy_domain_for_shield_domain(value: ShieldRiskDomain | str) -> RiskDomain:
    """Map a shield domain to the existing policy risk domain vocabulary.

    Returns:
        RiskDomain value produced by policy_domain_for_shield_domain().
    """
    try:
        domain = ShieldRiskDomain(value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return RiskDomain.UNKNOWN
    return _POLICY_DOMAIN_BY_SHIELD_DOMAIN.get(domain, RiskDomain.UNKNOWN)


def shield_request_to_action_input(
    request: ShieldEvaluationRequest,
    decision: ShieldDecision,
    *,
    risk_domain: ShieldRiskDomain | str | None = None,
) -> ActionInput:
    """Build existing policy-verdict input from shield facts without new verdict values.

    Args:
        request: Request object sent through the operation.
        decision: Decision value consumed by shield_request_to_action_input().
        risk_domain: Risk domain value consumed by shield_request_to_action_input().

    Returns:
        ActionInput value produced by shield_request_to_action_input().
    """
    policy_domain = policy_domain_for_shield_domain(
        risk_domain or request.risk_domain or decision.details.get("risk_domain", "")
    )
    return ActionInput(
        action_id=f"shield:{decision.pack_id}:{decision.rule_id}",
        action_type=request.action_type,
        actor_id=request.actor_id,
        run_id=request.run_id,
        risk_domain=policy_domain,
        summary=request.action_summary,
        evidence_links=_evidence_links(decision),
        authority_refs=request.authority_refs,
        details={
            "shield_pack_id": decision.pack_id,
            "shield_rule_id": decision.rule_id,
            "shield_reason_code": decision.reason_code,
            "shield_risk_domain": str(risk_domain or request.risk_domain or decision.details.get("risk_domain", "")),
        },
        metadata=dict(request.metadata),
    )


def evaluate_shield_action(
    runtime: WorkbenchShieldRuntime,
    request: ShieldEvaluationRequest,
    *,
    policy_config: Mapping[str, Any] | None = None,
    tool_trust_decisions: Mapping[str, ToolSurfaceTrustDecision] | None = None,
    capability_decisions: Mapping[str, CapabilityEnablementDecision] | None = None,
) -> ShieldDecision:
    """Evaluate one shield and compose existing policy, tool, and capability decisions.

    Args:
        runtime: Runtime value consumed by evaluate_shield_action().
        request: Request object sent through the operation.
        policy_config: Policy config value consumed by evaluate_shield_action().
        tool_trust_decisions: Tool trust decisions value consumed by evaluate_shield_action().
        capability_decisions: Capability decisions value consumed by evaluate_shield_action().

    Returns:
        ShieldDecision value produced by evaluate_shield_action().
    """
    base = runtime.evaluate(request)
    if not base.allowed:
        return base
    tool_block = _blocked_tool_decision(request, base, tool_trust_decisions or {})
    if tool_block is not None:
        return tool_block
    capability_block = _blocked_capability_decision(request, base, capability_decisions or {})
    if capability_block is not None:
        return capability_block

    action = shield_request_to_action_input(request, base)
    verdict = classify_action(action, config=dict(policy_config) if policy_config is not None else None)
    if verdict.value is VerdictValue.ALLOW:
        return ShieldDecision(
            value=base.value,
            pack_id=base.pack_id,
            rule_id=base.rule_id,
            reason_code=base.reason_code,
            policy_version=verdict.policy_version,
            evidence_refs=base.evidence_refs,
            details={
                **dict(base.details),
                "policy_verdict_value": verdict.value.value,
                "policy_reason_code": verdict.reason_code.value,
            },
        )
    return ShieldDecision(
        value=_shield_value_from_verdict(verdict.value),
        pack_id=base.pack_id,
        rule_id=base.rule_id,
        reason_code=verdict.reason_code.value,
        policy_version=verdict.policy_version,
        evidence_refs=base.evidence_refs,
        details={
            **dict(base.details),
            "policy_verdict_value": verdict.value.value,
            "policy_reason_code": verdict.reason_code.value,
        },
    )


def _blocked_tool_decision(
    request: ShieldEvaluationRequest,
    base: ShieldDecision,
    decisions: Mapping[str, ToolSurfaceTrustDecision],
) -> ShieldDecision | None:
    for surface_id in request.tool_surface_ids:
        decision = decisions.get(surface_id)
        if decision is None or not decision.allowed:
            reasons = (
                tuple(reason.value for reason in decision.reasons) if decision else ("missing_tool_surface_trust",)
            )
            return ShieldDecision(
                value=ShieldDecisionValue.BLOCK,
                pack_id=base.pack_id,
                rule_id=base.rule_id,
                reason_code="tool_surface_untrusted",
                policy_version=base.policy_version,
                evidence_refs=base.evidence_refs,
                details={**dict(base.details), "tool_surface_id": surface_id, "tool_trust_reasons": list(reasons)},
            )
    return None


def _blocked_capability_decision(
    request: ShieldEvaluationRequest,
    base: ShieldDecision,
    decisions: Mapping[str, CapabilityEnablementDecision],
) -> ShieldDecision | None:
    for pack_id in request.capability_pack_ids:
        decision = decisions.get(pack_id)
        if decision is None or not decision.allowed:
            reasons = tuple(decision.reasons) if decision else ("missing_capability_pack_decision",)
            return ShieldDecision(
                value=ShieldDecisionValue.DEGRADED,
                pack_id=base.pack_id,
                rule_id=base.rule_id,
                reason_code="capability_pack_untrusted",
                policy_version=base.policy_version,
                evidence_refs=base.evidence_refs,
                details={**dict(base.details), "capability_pack_id": pack_id, "capability_reasons": list(reasons)},
            )
    return None


def _shield_value_from_verdict(value: VerdictValue) -> ShieldDecisionValue:
    if value is VerdictValue.BLOCK:
        return ShieldDecisionValue.BLOCK
    if value is VerdictValue.ESCALATE:
        return ShieldDecisionValue.ESCALATE
    if value is VerdictValue.WARN:
        return ShieldDecisionValue.WARN
    return ShieldDecisionValue.ALLOW


def _evidence_links(decision: ShieldDecision) -> tuple[EvidenceLink, ...]:
    return tuple(
        EvidenceLink(
            evidence_id=f"shield-{index}",
            kind="external",
            ref=ref,
            summary=f"shield evidence for {decision.pack_id}",
        )
        for index, ref in enumerate(decision.evidence_refs, start=1)
    )
