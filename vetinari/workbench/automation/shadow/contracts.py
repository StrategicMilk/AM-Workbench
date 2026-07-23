"""Dry-run and shadow-run contracts for Workbench automations.

The module is import-safe: it does not execute automation actions, read or
write persistent state, register routes, spawn workers, or construct live
services. It converts existing automation-builder declarations into replayable
proposal data and evaluates deterministic activation gates.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from vetinari.workbench.agents.harness.contracts import AgentRunAdmission
from vetinari.workbench.automation.builder import (
    AutomationDefinition,
    AutomationSimulation,
    SimulationContext,
    simulate_automation,
)
from vetinari.workbench.policy_explainability import PolicyExplanation


class ShadowContractError(ValueError):
    """Raised when a shadow-run contract cannot be constructed safely."""


class ShadowRunMode(str, Enum):
    """Preview mode used before any represented action can execute."""

    DRY_RUN = "dry_run"
    SHADOW_RUN = "shadow_run"


class ShadowPlanStatus(str, Enum):
    """Deterministic plan status before activation evaluation."""

    PROPOSED = "proposed"
    BLOCKED = "blocked"


class BudgetPosture(str, Enum):
    """Budget signal posture for activation gates."""

    WITHIN_LIMIT = "within_limit"
    EXCEEDED = "exceeded"
    UNKNOWN = "unknown"


class QuietHoursPosture(str, Enum):
    """Quiet-hours signal posture for activation gates."""

    OUTSIDE_QUIET_HOURS = "outside_quiet_hours"
    ACTIVE = "active"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SimulatedSideEffect:
    """Data-only representation of what the automation would have changed."""

    effect_id: str
    effect_type: str
    target_ref: str
    description: str
    reversible: bool
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.effect_id, "effect_id")
        _require_text(self.effect_type, "effect_type")
        _require_text(self.target_ref, "target_ref")
        _require_text(self.description, "description")
        _require_string_tuple(self.evidence_refs, "evidence_refs", allow_empty=True)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SimulatedSideEffect(effect_id={self.effect_id!r}, effect_type={self.effect_type!r}, target_ref={self.target_ref!r})"


@dataclass(frozen=True, slots=True)
class ShadowApprovalDiff:
    """Operator-visible before/after summary for an activation request."""

    diff_id: str
    before_ref: str
    after_ref: str
    summary: str
    operator_review_ref: str
    changed_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.diff_id, "diff_id")
        _require_text(self.before_ref, "before_ref")
        _require_text(self.after_ref, "after_ref")
        _require_text(self.summary, "summary")
        _require_text(self.operator_review_ref, "operator_review_ref")
        _require_string_tuple(self.changed_fields, "changed_fields")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShadowApprovalDiff(diff_id={self.diff_id!r}, before_ref={self.before_ref!r}, after_ref={self.after_ref!r})"


@dataclass(frozen=True, slots=True)
class BudgetCeiling:
    """Budget ceiling shown in the shadow plan."""

    max_cost_usd: float
    max_runtime_minutes: int
    posture: BudgetPosture
    evidence_ref: str

    def __post_init__(self) -> None:
        if self.max_cost_usd <= 0:
            raise ShadowContractError("budget.max_cost_usd must be > 0")
        if self.max_runtime_minutes <= 0:
            raise ShadowContractError("budget.max_runtime_minutes must be > 0")
        _require_text(self.evidence_ref, "budget.evidence_ref")
        _require_enum(self.posture, BudgetPosture, "budget.posture")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BudgetCeiling(max_cost_usd={self.max_cost_usd!r}, max_runtime_minutes={self.max_runtime_minutes!r}, posture={self.posture!r})"


@dataclass(frozen=True, slots=True)
class QuietHoursPolicy:
    """Quiet-hours policy snapshot shown in the shadow plan."""

    timezone: str
    start_hour: int
    end_hour: int
    posture: QuietHoursPosture
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_text(self.timezone, "quiet_hours.timezone")
        if not 0 <= self.start_hour <= 23 or not 0 <= self.end_hour <= 23:
            raise ShadowContractError("quiet_hours hours must be 0..23")
        _require_enum(self.posture, QuietHoursPosture, "quiet_hours.posture")
        _require_text(self.evidence_ref, "quiet_hours.evidence_ref")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"QuietHoursPolicy(timezone={self.timezone!r}, start_hour={self.start_hour!r}, end_hour={self.end_hour!r})"
        )


@dataclass(frozen=True, slots=True)
class RollbackPath:
    """Rollback action that must be visible before activation."""

    strategy: str
    target_ref: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_text(self.strategy, "rollback.strategy")
        _require_text(self.target_ref, "rollback.target_ref")
        _require_text(self.evidence_ref, "rollback.evidence_ref")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass(frozen=True, slots=True)
class ShadowRunReceipt:
    """Replayable receipt for a dry-run or shadow-run plan."""

    receipt_id: str
    automation_id: str
    mode: ShadowRunMode
    replay_boundary_ref: str
    evidence_refs: tuple[str, ...]
    simulated_effect_ids: tuple[str, ...]
    emitted_at_utc: str

    def __post_init__(self) -> None:
        _require_text(self.receipt_id, "receipt_id")
        _require_text(self.automation_id, "automation_id")
        _require_enum(self.mode, ShadowRunMode, "mode")
        _require_text(self.replay_boundary_ref, "replay_boundary_ref")
        _require_string_tuple(self.evidence_refs, "evidence_refs")
        _require_string_tuple(self.simulated_effect_ids, "simulated_effect_ids")
        _require_text(self.emitted_at_utc, "emitted_at_utc")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShadowRunReceipt(receipt_id={self.receipt_id!r}, automation_id={self.automation_id!r}, mode={self.mode!r})"


@dataclass(frozen=True, slots=True)
class ShadowRunPlan:
    """Inspectable automation proposal created before execution."""

    plan_id: str
    automation_id: str
    mode: ShadowRunMode
    status: ShadowPlanStatus
    high_impact: bool
    action_type: str
    target_ref: str
    proposed_by_ref: str
    blocked_reasons: tuple[str, ...]
    simulated_side_effects: tuple[SimulatedSideEffect, ...]
    approval_diff: ShadowApprovalDiff
    budget: BudgetCeiling
    quiet_hours: QuietHoursPolicy
    rollback: RollbackPath
    activation_gate_ids: tuple[str, ...]
    receipt: ShadowRunReceipt
    policy_evidence_ref: str | None = None
    harness_evidence_ref: str | None = None
    harness_authority_ref: str | None = None
    harness_provenance_ref: str | None = None
    source_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.plan_id, "plan_id")
        _require_text(self.automation_id, "automation_id")
        _require_enum(self.mode, ShadowRunMode, "mode")
        _require_enum(self.status, ShadowPlanStatus, "status")
        _require_text(self.action_type, "action_type")
        _require_text(self.target_ref, "target_ref")
        _require_text(self.proposed_by_ref, "proposed_by_ref")
        _require_string_tuple(self.blocked_reasons, "blocked_reasons", allow_empty=True)
        if not self.simulated_side_effects:
            raise ShadowContractError("simulated_side_effects must be non-empty")
        if not isinstance(self.approval_diff, ShadowApprovalDiff):
            raise ShadowContractError("approval_diff must be ShadowApprovalDiff")
        if not isinstance(self.budget, BudgetCeiling):
            raise ShadowContractError("budget must be BudgetCeiling")
        if not isinstance(self.quiet_hours, QuietHoursPolicy):
            raise ShadowContractError("quiet_hours must be QuietHoursPolicy")
        if not isinstance(self.rollback, RollbackPath):
            raise ShadowContractError("rollback must be RollbackPath")
        _require_string_tuple(self.activation_gate_ids, "activation_gate_ids")
        if not isinstance(self.receipt, ShadowRunReceipt):
            raise ShadowContractError("receipt must be ShadowRunReceipt")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShadowRunPlan(plan_id={self.plan_id!r}, automation_id={self.automation_id!r}, mode={self.mode!r})"


@dataclass(frozen=True, slots=True)
class ShadowActivationDecision:
    """Deterministic activation decision for a shadow plan."""

    plan_id: str
    activation_eligible: bool
    decision_kind: str
    blocked_reasons: tuple[str, ...]
    required_operator_gate_ids: tuple[str, ...]
    receipt_id: str

    def __post_init__(self) -> None:
        _require_text(self.plan_id, "plan_id")
        _require_text(self.decision_kind, "decision_kind")
        _require_string_tuple(self.blocked_reasons, "blocked_reasons", allow_empty=True)
        _require_string_tuple(self.required_operator_gate_ids, "required_operator_gate_ids")
        _require_text(self.receipt_id, "receipt_id")
        if self.activation_eligible and self.blocked_reasons:
            raise ShadowContractError("eligible decision cannot include blockers")
        if not self.activation_eligible and self.decision_kind != "blocked":
            raise ShadowContractError("ineligible decision_kind must be blocked")

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(asdict(self))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShadowActivationDecision(plan_id={self.plan_id!r}, activation_eligible={self.activation_eligible!r}, decision_kind={self.decision_kind!r})"


def compile_shadow_plan(
    definition: AutomationDefinition,
    *,
    mode: ShadowRunMode = ShadowRunMode.DRY_RUN,
    simulation_context: SimulationContext | None = None,
    policy_explanation: PolicyExplanation | None = None,
    harness_admission: AgentRunAdmission | None = None,
    harness_authority_ref: str | None = None,
    harness_provenance_ref: str | None = None,
    activation_gate_ids: Sequence[str] = (),
    operator_review_ref: str = "",
    proposed_by_ref: str = "system:deterministic-shadow-compiler",
) -> ShadowRunPlan:
    """Compile a data-only dry-run or shadow-run plan from an automation definition.

    The represented automation action is never executed; only the pure
    simulation helper is used to gather proposed effects and receipt evidence.

    Returns:
        Value produced for the caller.

    Raises:
        ShadowContractError: Propagated when validation, persistence, or execution fails.
    """
    if not isinstance(definition, AutomationDefinition):
        raise ShadowContractError("definition must be AutomationDefinition")
    _require_enum(mode, ShadowRunMode, "mode")
    _require_text(operator_review_ref, "operator_review_ref")
    _require_text(proposed_by_ref, "proposed_by_ref")
    _require_string_sequence(activation_gate_ids, "activation_gate_ids")

    context = simulation_context or _default_simulation_context(definition)
    simulation = simulate_automation(definition, context)
    side_effect = _side_effect_for(definition)
    policy_ref = _policy_evidence_ref(policy_explanation)
    harness_ref = _harness_evidence_ref(harness_admission)
    blocked = _shadow_blocked_reasons(definition, simulation.blocked_reasons, proposed_by_ref)
    receipt = _shadow_receipt_for(definition, mode, side_effect, simulation, harness_admission, policy_ref, harness_ref)
    return ShadowRunPlan(
        plan_id=_stable_id("shadow-plan", definition.automation_id, mode.value, receipt.receipt_id),
        automation_id=definition.automation_id,
        mode=mode,
        status=ShadowPlanStatus.BLOCKED if blocked else ShadowPlanStatus.PROPOSED,
        high_impact=definition.action.high_impact,
        action_type=definition.action.action_type,
        target_ref=definition.action.target_ref,
        proposed_by_ref=proposed_by_ref,
        blocked_reasons=tuple(dict.fromkeys(blocked)),
        simulated_side_effects=(side_effect,),
        approval_diff=ShadowApprovalDiff(
            diff_id=_stable_id("approval-diff", definition.automation_id, definition.action.target_ref),
            before_ref=f"before:{definition.action.target_ref}",
            after_ref=f"proposed:{definition.action.target_ref}",
            summary=f"Would run {definition.action.action_type} against {definition.action.target_ref}",
            operator_review_ref=operator_review_ref,
            changed_fields=("action", "target_ref", "rollback", "budget", "quiet_hours"),
        ),
        budget=BudgetCeiling(
            definition.budget.max_cost_usd,
            definition.budget.max_runtime_minutes,
            _budget_posture(policy_explanation, context, definition),
            policy_ref or "budget:missing-policy-evidence",
        ),
        quiet_hours=QuietHoursPolicy(
            definition.quiet_hours.timezone,
            definition.quiet_hours.start_hour,
            definition.quiet_hours.end_hour,
            _quiet_hours_posture(context, definition),
            f"quiet-hours:{definition.quiet_hours.timezone}",
        ),
        rollback=RollbackPath(
            definition.rollback.strategy, definition.rollback.target_ref, f"rollback:{definition.rollback.target_ref}"
        ),
        activation_gate_ids=tuple(dict.fromkeys(activation_gate_ids)),
        receipt=receipt,
        policy_evidence_ref=policy_ref,
        harness_evidence_ref=harness_ref,
        harness_authority_ref=harness_authority_ref,
        harness_provenance_ref=harness_provenance_ref,
        source_metadata={
            "builder_receipt_id": simulation.receipt.receipt_id,
            "builder_proposed_only": simulation.proposed_only,
            "builder_passed": simulation.receipt.passed,
        },
    )


def _shadow_blocked_reasons(
    definition: AutomationDefinition,
    simulation_blocked_reasons: Sequence[str],
    proposed_by_ref: str,
) -> list[str]:
    blocked = list(simulation_blocked_reasons)
    if definition.action.self_promote and definition.action.high_impact:
        blocked.append("high-impact-self-promotion-blocked")
    if definition.action.high_impact and _is_agent_or_model_ref(proposed_by_ref):
        blocked.append("high-impact-agent-or-model-self-promotion-blocked")
    return blocked


def _shadow_receipt_for(
    definition: AutomationDefinition,
    mode: ShadowRunMode,
    side_effect: SimulatedSideEffect,
    simulation: AutomationSimulation,
    harness_admission: AgentRunAdmission | None,
    policy_ref: str | None,
    harness_ref: str | None,
) -> ShadowRunReceipt:
    evidence_refs = tuple(
        dict.fromkeys((
            *simulation.receipt.evidence_refs,
            f"builder-receipt:{simulation.receipt.receipt_id}",
            f"policy:{policy_ref}" if policy_ref else "policy:missing",
            f"harness:{harness_ref}" if harness_ref else "harness:missing",
        ))
    )
    return ShadowRunReceipt(
        receipt_id=_stable_id("shadow-receipt", definition.automation_id, mode.value, side_effect.effect_id),
        automation_id=definition.automation_id,
        mode=mode,
        replay_boundary_ref=_replay_boundary_ref(harness_admission),
        evidence_refs=evidence_refs,
        simulated_effect_ids=(side_effect.effect_id,),
        emitted_at_utc=datetime.now(timezone.utc).isoformat(),
    )


def evaluate_shadow_activation(
    plan: ShadowRunPlan,
    *,
    policy_explanation: PolicyExplanation | None = None,
    harness_admission: AgentRunAdmission | None = None,
    operator_approved_gate_ids: Sequence[str] = (),
) -> ShadowActivationDecision:
    """Evaluate whether a shadow plan may be activated by an operator.

    Returns:
        Value produced for the caller.
    """
    from vetinari.workbench.automation.shadow.activation import (
        evaluate_shadow_activation as _evaluate_shadow_activation,
    )

    return _evaluate_shadow_activation(
        plan,
        policy_explanation=policy_explanation,
        harness_admission=harness_admission,
        operator_approved_gate_ids=operator_approved_gate_ids,
    )


def _is_agent_or_model_ref(value: str) -> bool:
    principal = value.strip().lower()
    return principal.startswith(("agent:", "model:"))


def _default_simulation_context(definition: AutomationDefinition) -> SimulationContext:
    return SimulationContext(
        trigger_source=definition.trigger,
        observed=dict(definition.condition.required_context),
        current_hour=None,
        recent_run_count=0,
        estimated_cost_usd=0.0,
        estimated_runtime_minutes=0,
        lease_available=True,
    )


def _side_effect_for(definition: AutomationDefinition) -> SimulatedSideEffect:
    return SimulatedSideEffect(
        effect_id=_stable_id(
            "effect", definition.automation_id, definition.action.action_type, definition.action.target_ref
        ),
        effect_type=definition.action.action_type,
        target_ref=definition.action.target_ref,
        description=f"Would propose {definition.action.action_type} for {definition.action.target_ref}",
        reversible=bool(definition.rollback.strategy and definition.rollback.target_ref),
        evidence_refs=(
            f"automation:{definition.automation_id}",
            f"rollback:{definition.rollback.target_ref}",
        ),
    )


def _policy_evidence_ref(explanation: PolicyExplanation | None) -> str | None:
    if explanation is None:
        return None
    if explanation.trace.trace_id:
        return f"policy-trace:{explanation.trace.trace_id}"
    return f"policy:{explanation.policy_id}:{explanation.decision_kind}"


def _harness_evidence_ref(admission: AgentRunAdmission | None) -> str | None:
    if admission is None:
        return None
    return f"harness:{admission.run_id}:{admission.replay_boundary_ref}"


def _replay_boundary_ref(admission: AgentRunAdmission | None) -> str:
    if admission is None:
        return "replay:shadow-plan-local-boundary"
    return admission.replay_boundary_ref


def _budget_posture(
    explanation: PolicyExplanation | None,
    context: SimulationContext,
    definition: AutomationDefinition,
) -> BudgetPosture:
    if explanation is None or explanation.budget.limit == "unavailable":
        return BudgetPosture.UNKNOWN
    if context.estimated_cost_usd > definition.budget.max_cost_usd:
        return BudgetPosture.EXCEEDED
    if context.estimated_runtime_minutes > definition.budget.max_runtime_minutes:
        return BudgetPosture.EXCEEDED
    return BudgetPosture.WITHIN_LIMIT


def _quiet_hours_posture(context: SimulationContext, definition: AutomationDefinition) -> QuietHoursPosture:
    if context.current_hour is None:
        return QuietHoursPosture.UNKNOWN
    hour = context.current_hour
    quiet = definition.quiet_hours
    if quiet.start_hour == quiet.end_hour:
        return QuietHoursPosture.OUTSIDE_QUIET_HOURS
    if quiet.start_hour < quiet.end_hour:
        active = quiet.start_hour <= hour < quiet.end_hour
    else:
        active = hour >= quiet.start_hour or hour < quiet.end_hour
    return QuietHoursPosture.ACTIVE if active else QuietHoursPosture.OUTSIDE_QUIET_HOURS


def _stable_id(prefix: str, *parts: str) -> str:
    raw = "|".join((prefix, *parts))
    return f"{prefix}:{uuid5(NAMESPACE_URL, raw).hex}"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _require_enum(value: object, enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise ShadowContractError(f"{field_name} must be {enum_type.__name__}")


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ShadowContractError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise ShadowContractError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ShadowContractError(f"{field_name} must contain non-empty strings")


def _require_string_sequence(values: Sequence[str], field_name: str, *, allow_empty: bool = False) -> None:
    if isinstance(values, (str, bytes)) or (not allow_empty and not values):
        raise ShadowContractError(f"{field_name} must be a non-empty sequence")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ShadowContractError(f"{field_name} must contain non-empty strings")
