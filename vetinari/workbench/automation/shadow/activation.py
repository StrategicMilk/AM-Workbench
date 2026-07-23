"""Activation-gate evaluation for Workbench shadow-run plans."""

from __future__ import annotations

from collections.abc import Sequence

from vetinari.workbench.agents.harness.contracts import AgentRunAdmission
from vetinari.workbench.automation.shadow.contracts import (
    BudgetPosture,
    QuietHoursPosture,
    ShadowActivationDecision,
    ShadowContractError,
    ShadowRunMode,
    ShadowRunPlan,
    _harness_evidence_ref,
    _policy_evidence_ref,
    _require_string_sequence,
)
from vetinari.workbench.policy_explainability import PolicyExplanation


def evaluate_shadow_activation(
    plan: ShadowRunPlan,
    *,
    policy_explanation: PolicyExplanation | None = None,
    harness_admission: AgentRunAdmission | None = None,
    operator_approved_gate_ids: Sequence[str] = (),
) -> ShadowActivationDecision:
    """Evaluate whether a shadow plan may be activated by an operator.

    Returns:
        ShadowActivationDecision value produced by evaluate_shadow_activation().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(plan, ShadowRunPlan):
        raise ShadowContractError("plan must be ShadowRunPlan")
    _require_string_sequence(operator_approved_gate_ids, "operator_approved_gate_ids", allow_empty=True)
    blocked: list[str] = list(plan.blocked_reasons)
    blocked.extend(_operator_gate_blockers(plan, operator_approved_gate_ids))
    blocked.extend(_policy_blockers(policy_explanation, plan))
    blocked.extend(_harness_blockers(harness_admission, plan))
    blocked.extend(_plan_integrity_blockers(plan))
    blockers = tuple(dict.fromkeys(blocked))
    return ShadowActivationDecision(
        plan_id=plan.plan_id,
        activation_eligible=not blockers,
        decision_kind="eligible" if not blockers else "blocked",
        blocked_reasons=blockers,
        required_operator_gate_ids=plan.activation_gate_ids,
        receipt_id=plan.receipt.receipt_id,
    )


def _operator_gate_blockers(plan: ShadowRunPlan, approved_gate_ids: Sequence[str]) -> list[str]:
    approved = set(approved_gate_ids)
    missing = [gate_id for gate_id in plan.activation_gate_ids if gate_id not in approved]
    return [f"operator-gate-missing:{gate_id}" for gate_id in missing]


def _policy_blockers(explanation: PolicyExplanation | None, plan: ShadowRunPlan) -> list[str]:
    if explanation is None:
        return ["policy-explanation-missing"]
    blocked: list[str] = []
    if not explanation.allowed:
        blocked.append("policy-explanation-denied")
    if explanation.degraded:
        blocked.append("policy-explanation-degraded")
    if explanation.budget.limit == "unavailable" or explanation.budget.remaining == "unknown":
        blocked.append("policy-budget-unavailable")
    if not explanation.trace.will_record or not explanation.trace.receipt_kind:
        blocked.append("policy-trace-receipt-missing")
    if "deny-before-use" not in explanation.failure_behavior:
        blocked.append("policy-failure-behavior-missing")
    if not plan.policy_evidence_ref:
        blocked.append("policy-evidence-ref-missing")
    elif _policy_evidence_ref(explanation) != plan.policy_evidence_ref:
        blocked.append("policy-evidence-ref-mismatch")
    return blocked


def _harness_blockers(admission: AgentRunAdmission | None, plan: ShadowRunPlan) -> list[str]:
    if not _requires_harness(plan):
        return []
    if admission is None:
        return ["harness-admission-missing"]
    blocked: list[str] = []
    if not admission.admitted:
        blocked.extend(f"harness-blocker:{blocker}" for blocker in admission.blockers)
    if not admission.replay_boundary_ref:
        blocked.append("harness-replay-boundary-missing")
    if not admission.admitted_tools:
        blocked.append("harness-tool-permission-missing")
    if not plan.harness_evidence_ref:
        blocked.append("harness-evidence-ref-missing")
    elif _harness_evidence_ref(admission) != plan.harness_evidence_ref:
        blocked.append("harness-evidence-ref-mismatch")
    if admission.replay_boundary_ref != plan.receipt.replay_boundary_ref:
        blocked.append("harness-replay-boundary-mismatch")
    if not plan.harness_authority_ref:
        blocked.append("harness-authority-ref-missing")
    if not plan.harness_provenance_ref:
        blocked.append("harness-provenance-ref-missing")
    return blocked


def _plan_integrity_blockers(plan: ShadowRunPlan) -> list[str]:
    blocked: list[str] = []
    if plan.mode is ShadowRunMode.DRY_RUN:
        blocked.append("dry-run-cannot-activate")
    if plan.high_impact and _is_agent_or_model_ref(plan.proposed_by_ref):
        blocked.append("high-impact-agent-or-model-self-promotion-blocked")
    if plan.budget.posture is BudgetPosture.UNKNOWN:
        blocked.append("budget-posture-unknown")
    if plan.budget.posture is BudgetPosture.EXCEEDED:
        blocked.append("budget-exceeded")
    if plan.quiet_hours.posture is QuietHoursPosture.UNKNOWN:
        blocked.append("quiet-hours-posture-unknown")
    if plan.quiet_hours.posture is QuietHoursPosture.ACTIVE:
        blocked.append("quiet-hours-active")
    if not plan.rollback.strategy or not plan.rollback.target_ref:
        blocked.append("rollback-path-missing")
    if not plan.approval_diff.operator_review_ref:
        blocked.append("operator-approval-diff-missing")
    if not plan.receipt.replay_boundary_ref or not plan.receipt.evidence_refs:
        blocked.append("replay-receipt-missing")
    if not plan.activation_gate_ids:
        blocked.append("activation-gates-missing")
    return blocked


def _requires_harness(plan: ShadowRunPlan) -> bool:
    action = plan.action_type.lower()
    privileged_tokens = (
        "agent",
        "workflow",
        "shell",
        "command",
        "file",
        "write",
        "network",
        "http",
        "mcp",
        "tool",
        "model",
    )
    return any(token in action for token in privileged_tokens)


def _is_agent_or_model_ref(value: str) -> bool:
    principal = value.strip().lower()
    return principal.startswith(("agent:", "model:"))
