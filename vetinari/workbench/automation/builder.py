"""Fail-closed automation builder contract for AM Workbench.

The builder is intentionally import-safe: it does not read or write disk, spawn
jobs, or register handlers. Callers construct an ``AutomationDefinition`` and run
``simulate_automation`` to get a receipt-shaped decision record before any
external scheduler can execute the action.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

ALLOWED_TRIGGER_SOURCES: tuple[str, ...] = (
    "file_change",
    "source_staleness",
    "new_trace",
    "failed_eval",
    "new_model",
    "dataset_drift",
    "benchmark_change",
    "cost_threshold",
    "annotation_queue",
    "training_completion",
)

_REQUIRED_DEFINITION_FIELDS = (
    "automation_id",
    "name",
    "trigger",
    "condition",
    "action",
    "approval",
    "rollback",
    "budget",
    "quiet_hours",
    "rate_limit",
    "resource_lease",
    "failure_policy",
)


class AutomationValidationError(ValueError):
    """Raised when an automation cannot be trusted enough to simulate."""


class AutomationFailurePolicy(str, Enum):
    """Operator-selected response when an automation run fails."""

    PROPOSE_ONLY = "propose_only"
    PAUSE_AUTOMATION = "pause_automation"
    ESCALATE = "escalate"


@dataclass(frozen=True, slots=True)
class AutomationCondition:
    """Structured condition evaluated against a simulation context."""

    description: str
    required_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.description, "condition.description")


@dataclass(frozen=True, slots=True)
class AutomationAction:
    """Action proposed by an automation definition."""

    action_type: str
    target_ref: str
    high_impact: bool
    self_promote: bool = False
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.action_type, "action.action_type")
        _require_text(self.target_ref, "action.target_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AutomationAction(action_type={self.action_type!r}, target_ref={self.target_ref!r}, high_impact={self.high_impact!r})"


@dataclass(frozen=True, slots=True)
class AutomationApproval:
    """Approval requirement for executing an automation action."""

    required: bool
    approver_role: str
    evidence_ref: str = ""

    def __post_init__(self) -> None:
        if self.required:
            _require_text(self.approver_role, "approval.approver_role")


@dataclass(frozen=True, slots=True)
class AutomationRollback:
    """Rollback path displayed before an automation can run."""

    strategy: str
    target_ref: str

    def __post_init__(self) -> None:
        _require_text(self.strategy, "rollback.strategy")
        _require_text(self.target_ref, "rollback.target_ref")


@dataclass(frozen=True, slots=True)
class AutomationBudget:
    """Execution budget guard."""

    max_cost_usd: float
    max_runtime_minutes: int

    def __post_init__(self) -> None:
        if self.max_cost_usd <= 0:
            raise AutomationValidationError("budget.max_cost_usd must be > 0")
        if self.max_runtime_minutes <= 0:
            raise AutomationValidationError("budget.max_runtime_minutes must be > 0")


@dataclass(frozen=True, slots=True)
class AutomationQuietHours:
    """Time window where automations may only propose."""

    timezone: str
    start_hour: int
    end_hour: int

    def __post_init__(self) -> None:
        _require_text(self.timezone, "quiet_hours.timezone")
        if not 0 <= self.start_hour <= 23 or not 0 <= self.end_hour <= 23:
            raise AutomationValidationError("quiet_hours start_hour and end_hour must be 0..23")


@dataclass(frozen=True, slots=True)
class AutomationRateLimit:
    """Per-window run limit."""

    max_runs: int
    window_minutes: int

    def __post_init__(self) -> None:
        if self.max_runs <= 0:
            raise AutomationValidationError("rate_limit.max_runs must be > 0")
        if self.window_minutes <= 0:
            raise AutomationValidationError("rate_limit.window_minutes must be > 0")


@dataclass(frozen=True, slots=True)
class AutomationLease:
    """Resource lease required before execution."""

    lane: str
    resource_ref: str
    required: bool = True

    def __post_init__(self) -> None:
        _require_text(self.lane, "resource_lease.lane")
        _require_text(self.resource_ref, "resource_lease.resource_ref")


@dataclass(frozen=True, slots=True)
class AutomationDefinition:
    """Complete automation declaration from the builder."""

    automation_id: str
    name: str
    trigger: str
    condition: AutomationCondition
    action: AutomationAction
    approval: AutomationApproval
    rollback: AutomationRollback
    budget: AutomationBudget
    quiet_hours: AutomationQuietHours
    rate_limit: AutomationRateLimit
    resource_lease: AutomationLease
    failure_policy: AutomationFailurePolicy
    enabled: bool = True

    def __post_init__(self) -> None:
        _require_text(self.automation_id, "automation_id")
        _require_text(self.name, "name")
        if self.trigger not in ALLOWED_TRIGGER_SOURCES:
            raise AutomationValidationError(f"unsupported trigger source: {self.trigger!r}")
        if self.action.high_impact and not self.approval.required:
            raise AutomationValidationError("high-impact automations must require approval")
        if self.action.self_promote and self.action.high_impact:
            raise AutomationValidationError("high-impact automations cannot self-promote")

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["failure_policy"] = self.failure_policy.value
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"AutomationDefinition(automation_id={self.automation_id!r}, name={self.name!r}, trigger={self.trigger!r})"
        )


@dataclass(frozen=True, slots=True)
class SimulationContext:
    """Runtime facts used by dry-run simulation."""

    trigger_source: str
    observed: Mapping[str, Any] = field(default_factory=dict)
    current_hour: int | None = None
    recent_run_count: int = 0
    estimated_cost_usd: float = 0.0
    estimated_runtime_minutes: int = 0
    lease_available: bool = True

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SimulationContext(trigger_source={self.trigger_source!r}, observed={self.observed!r}, current_hour={self.current_hour!r})"


@dataclass(frozen=True, slots=True)
class AutomationRunReceipt:
    """Receipt-shaped run-history row emitted by dry-run simulation."""

    receipt_id: str
    automation_id: str
    dry_run: bool
    proposed_only: bool
    passed: bool
    reason: str
    emitted_at_utc: str
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AutomationRunReceipt(receipt_id={self.receipt_id!r}, automation_id={self.automation_id!r}, dry_run={self.dry_run!r})"


@dataclass(frozen=True, slots=True)
class AutomationSimulation:
    """Dry-run verdict for the builder preview."""

    automation_id: str
    runnable: bool
    proposed_only: bool
    approval_required: bool
    blocked_reasons: tuple[str, ...]
    receipt: AutomationRunReceipt

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["receipt"] = self.receipt.to_dict()
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AutomationSimulation(automation_id={self.automation_id!r}, runnable={self.runnable!r}, proposed_only={self.proposed_only!r})"


def build_automation_definition(payload: Mapping[str, Any]) -> AutomationDefinition:
    """Build and validate an automation definition from a schema-shaped mapping.

    Returns:
        Newly constructed automation definition value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    missing = [field_name for field_name in _REQUIRED_DEFINITION_FIELDS if field_name not in payload]
    if missing:
        raise AutomationValidationError(f"automation definition missing fields: {', '.join(missing)}")
    condition = _mapping(payload["condition"], "condition")
    action = _mapping(payload["action"], "action")
    approval = _mapping(payload["approval"], "approval")
    rollback = _mapping(payload["rollback"], "rollback")
    budget = _mapping(payload["budget"], "budget")
    quiet_hours = _mapping(payload["quiet_hours"], "quiet_hours")
    rate_limit = _mapping(payload["rate_limit"], "rate_limit")
    resource_lease = _mapping(payload["resource_lease"], "resource_lease")
    return AutomationDefinition(
        automation_id=str(payload["automation_id"]),
        name=str(payload["name"]),
        trigger=str(payload["trigger"]),
        condition=AutomationCondition(
            description=str(condition.get("description", "")),
            required_context=dict(condition.get("required_context", {})),
        ),
        action=AutomationAction(
            action_type=str(action.get("action_type", "")),
            target_ref=str(action.get("target_ref", "")),
            high_impact=bool(action.get("high_impact", False)),
            self_promote=bool(action.get("self_promote", False)),
            parameters=dict(action.get("parameters", {})),
        ),
        approval=AutomationApproval(
            required=bool(approval.get("required", False)),
            approver_role=str(approval.get("approver_role", "")),
            evidence_ref=str(approval.get("evidence_ref", "")),
        ),
        rollback=AutomationRollback(
            strategy=str(rollback.get("strategy", "")),
            target_ref=str(rollback.get("target_ref", "")),
        ),
        budget=AutomationBudget(
            max_cost_usd=float(budget.get("max_cost_usd", 0)),
            max_runtime_minutes=int(budget.get("max_runtime_minutes", 0)),
        ),
        quiet_hours=AutomationQuietHours(
            timezone=str(quiet_hours.get("timezone", "")),
            start_hour=int(quiet_hours.get("start_hour", 0)),
            end_hour=int(quiet_hours.get("end_hour", 0)),
        ),
        rate_limit=AutomationRateLimit(
            max_runs=int(rate_limit.get("max_runs", 0)),
            window_minutes=int(rate_limit.get("window_minutes", 0)),
        ),
        resource_lease=AutomationLease(
            lane=str(resource_lease.get("lane", "")),
            resource_ref=str(resource_lease.get("resource_ref", "")),
            required=bool(resource_lease.get("required", True)),
        ),
        failure_policy=AutomationFailurePolicy(str(payload["failure_policy"])),
        enabled=bool(payload.get("enabled", True)),
    )


def simulate_automation(definition: AutomationDefinition, context: SimulationContext) -> AutomationSimulation:
    """Dry-run an automation without executing the action.

        The returned receipt is the run-history artifact for the preview. Execution
        remains blocked when safety, approval, budget, quiet-hour, rate-limit, lease,
        or condition checks cannot prove the action is allowed.

    Args:
        definition: Definition value consumed by simulate_automation().
        context: Context value consumed by simulate_automation().

    Returns:
        AutomationSimulation value produced by simulate_automation().
    """
    blocked: list[str] = []
    if not definition.enabled:
        blocked.append("automation-disabled")
    if context.trigger_source != definition.trigger:
        blocked.append("trigger-source-mismatch")
    if context.trigger_source not in ALLOWED_TRIGGER_SOURCES:
        blocked.append("trigger-source-unsupported")
    for key, expected in definition.condition.required_context.items():
        if context.observed.get(key) != expected:
            blocked.append(f"condition-mismatch:{key}")
    if _inside_quiet_hours(definition.quiet_hours, context.current_hour):
        blocked.append("quiet-hours-active")
    if context.recent_run_count >= definition.rate_limit.max_runs:
        blocked.append("rate-limit-exhausted")
    if context.estimated_cost_usd > definition.budget.max_cost_usd:
        blocked.append("budget-cost-exceeded")
    if context.estimated_runtime_minutes > definition.budget.max_runtime_minutes:
        blocked.append("budget-runtime-exceeded")
    if definition.resource_lease.required and not context.lease_available:
        blocked.append("resource-lease-unavailable")
    if definition.action.high_impact and not definition.approval.evidence_ref.strip():
        blocked.append("approval-evidence-missing")
    if definition.action.self_promote and definition.action.high_impact:
        blocked.append("high-impact-self-promotion-blocked")

    runnable = not blocked
    proposed_only = bool(blocked) or definition.action.high_impact
    receipt = AutomationRunReceipt(
        receipt_id=uuid4().hex,
        automation_id=definition.automation_id,
        dry_run=True,
        proposed_only=proposed_only,
        passed=runnable,
        reason="ready-for-external-execution" if runnable else ";".join(blocked),
        emitted_at_utc=datetime.now(timezone.utc).isoformat(),
        evidence_refs=tuple(_evidence_refs(definition, context)),
    )
    return AutomationSimulation(
        automation_id=definition.automation_id,
        runnable=runnable,
        proposed_only=proposed_only,
        approval_required=definition.approval.required,
        blocked_reasons=tuple(blocked),
        receipt=receipt,
    )


def _inside_quiet_hours(quiet_hours: AutomationQuietHours, current_hour: int | None) -> bool:
    if current_hour is None:
        return True
    if not 0 <= current_hour <= 23:
        raise AutomationValidationError("context.current_hour must be 0..23")
    if quiet_hours.start_hour == quiet_hours.end_hour:
        return False
    if quiet_hours.start_hour < quiet_hours.end_hour:
        return quiet_hours.start_hour <= current_hour < quiet_hours.end_hour
    return current_hour >= quiet_hours.start_hour or current_hour < quiet_hours.end_hour


def _evidence_refs(definition: AutomationDefinition, context: SimulationContext) -> list[str]:
    refs = [
        f"trigger:{definition.trigger}",
        f"action:{definition.action.action_type}",
        f"rollback:{definition.rollback.strategy}",
        f"lease:{definition.resource_lease.resource_ref}",
    ]
    refs.extend(f"context:{key}" for key in sorted(context.observed))
    if definition.approval.evidence_ref.strip():
        refs.append(f"approval:{definition.approval.evidence_ref}")
    return refs


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AutomationValidationError(f"{field_name} must be a mapping")
    return value


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise AutomationValidationError(f"{field_name} must be non-empty")
