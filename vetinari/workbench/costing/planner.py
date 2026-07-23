"""Planning-grade Workbench cost and resource forecasting."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from vetinari.utils.bounded_collections import BoundedList


class CostResourcePlanningError(ValueError):
    """Raised when a cost plan cannot be trusted."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


class WorkloadKind(str, Enum):
    """Forecastable Workbench workload shapes."""

    RUN = "run"
    SWEEP = "sweep"
    TRAINING_JOB = "training_job"
    AUTOMATION = "automation"
    DEPLOYMENT = "deployment"


class CostBudgetStatus(str, Enum):
    """Budget status for a candidate or whole plan."""

    WITHIN_BUDGET = "within_budget"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


class CostPressureAction(str, Enum):
    """Observable planning changes caused by budget pressure."""

    SELECT_LOWER_COST_MODEL = "select_lower_cost_model"
    USE_LOCAL_BACKEND = "use_local_backend"
    REDUCE_DATASET_SIZE = "reduce_dataset_size"
    QUEUE_FOR_CAPACITY = "queue_for_capacity"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class ResourceEnvelope:
    """Workload resource dimensions consumed by a forecast."""

    input_tokens: int
    output_tokens: int
    requests: int = 1
    dataset_rows: int = 0
    gpu_hours: float = 0.0
    cpu_core_hours: float = 0.0
    ram_gb: float = 0.0
    storage_gb: float = 0.0
    network_gb: float = 0.0
    queue_minutes: float = 0.0

    def __post_init__(self) -> None:
        _require_non_negative_int(self.input_tokens, "input_tokens")
        _require_non_negative_int(self.output_tokens, "output_tokens")
        _require_positive_int(self.requests, "requests")
        _require_non_negative_int(self.dataset_rows, "dataset_rows")
        for field_name in ("gpu_hours", "cpu_core_hours", "ram_gb", "storage_gb", "network_gb", "queue_minutes"):
            _require_finite_non_negative(getattr(self, field_name), field_name)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ResourceEnvelope(input_tokens={self.input_tokens!r}, output_tokens={self.output_tokens!r}, requests={self.requests!r})"


@dataclass(frozen=True, slots=True)
class ModelBackendOption:
    """One backend/model option to simulate."""

    backend: str
    model_id: str
    input_cost_per_1k_tokens: float
    output_cost_per_1k_tokens: float
    latency_ms_per_request: float
    gpu_vram_gb: float = 0.0
    ram_gb: float = 0.0
    cpu_cores: float = 0.0
    queue_minutes: float = 0.0
    quality_score: float = 0.0
    local_gpu: bool = False

    def __post_init__(self) -> None:
        _require_non_empty(self.backend, "backend")
        _require_non_empty(self.model_id, "model_id")
        for field_name in (
            "input_cost_per_1k_tokens",
            "output_cost_per_1k_tokens",
            "latency_ms_per_request",
            "gpu_vram_gb",
            "ram_gb",
            "cpu_cores",
            "queue_minutes",
            "quality_score",
        ):
            _require_finite_non_negative(getattr(self, field_name), field_name)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ModelBackendOption(backend={self.backend!r}, model_id={self.model_id!r}, input_cost_per_1k_tokens={self.input_cost_per_1k_tokens!r})"


@dataclass(frozen=True, slots=True)
class BudgetEnvelope:
    """Operator planning limits for cost, latency, and infrastructure pressure."""

    max_cost_usd: float
    max_latency_ms: float | None = None
    max_gpu_hours: float | None = None
    max_queue_minutes: float | None = None
    max_ram_gb: float | None = None
    max_storage_gb: float | None = None
    max_network_gb: float | None = None

    def __post_init__(self) -> None:
        _require_finite_non_negative(self.max_cost_usd, "max_cost_usd")
        for field_name in (
            "max_latency_ms",
            "max_gpu_hours",
            "max_queue_minutes",
            "max_ram_gb",
            "max_storage_gb",
            "max_network_gb",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_finite_non_negative(value, field_name)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BudgetEnvelope(max_cost_usd={self.max_cost_usd!r}, max_latency_ms={self.max_latency_ms!r}, max_gpu_hours={self.max_gpu_hours!r})"


@dataclass(frozen=True, slots=True)
class CostPlanRequest:
    """Input contract for the cost resource planner."""

    plan_id: str
    project_id: str
    workload_kind: WorkloadKind | str
    resource: ResourceEnvelope
    model_options: tuple[ModelBackendOption, ...]
    budget: BudgetEnvelope
    requires_approval_when_over_budget: bool = True

    def __post_init__(self) -> None:
        _require_non_empty(self.plan_id, "plan_id")
        _require_non_empty(self.project_id, "project_id")
        object.__setattr__(
            self, "workload_kind", _coerce_enum(WorkloadKind, self.workload_kind, "workload-kind-unknown")
        )
        if not isinstance(self.resource, ResourceEnvelope):
            raise CostResourcePlanningError("resource-invalid", "resource must be ResourceEnvelope")
        if not self.model_options:
            raise CostResourcePlanningError("model-options-missing", "at least one model/backend option is required")
        for option in self.model_options:
            if not isinstance(option, ModelBackendOption):
                raise CostResourcePlanningError("model-option-invalid", "model_options must contain ModelBackendOption")
        if not isinstance(self.budget, BudgetEnvelope):
            raise CostResourcePlanningError("budget-invalid", "budget must be BudgetEnvelope")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CostPlanRequest(plan_id={self.plan_id!r}, project_id={self.project_id!r}, workload_kind={self.workload_kind!r})"


@dataclass(frozen=True, slots=True)
class CostPressureAdjustment:
    """A concrete change made because of cost or resource pressure."""

    action: CostPressureAction
    reason: str
    before: str
    after: str

    def to_dict(self) -> dict[str, str]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "before": self.before,
            "after": self.after,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CostPressureAdjustment(action={self.action!r}, reason={self.reason!r}, before={self.before!r})"


@dataclass(frozen=True, slots=True)
class CostPlanCandidate:
    """Forecast for one backend/model option."""

    backend: str
    model_id: str
    total_cost_usd: float
    token_cost_usd: float
    total_latency_ms: float
    gpu_hours: float
    cpu_core_hours: float
    ram_gb: float
    storage_gb: float
    network_gb: float
    queue_minutes: float
    quality_score: float
    budget_status: CostBudgetStatus
    blocking_reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["budget_status"] = self.budget_status.value
        payload["blocking_reasons"] = list(self.blocking_reasons)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CostPlanCandidate(backend={self.backend!r}, model_id={self.model_id!r}, total_cost_usd={self.total_cost_usd!r})"


@dataclass(frozen=True, slots=True)
class CostResourcePlan:
    """User-facing forecast plan with recommendation and pressure trace."""

    plan_id: str
    project_id: str
    workload_kind: WorkloadKind
    recommended_backend: str
    recommended_model_id: str
    budget_status: CostBudgetStatus
    approval_required: bool
    changed_by_cost_pressure: bool
    decision_reasons: tuple[str, ...]
    pressure_adjustments: tuple[CostPressureAdjustment, ...]
    candidates: tuple[CostPlanCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "project_id": self.project_id,
            "workload_kind": self.workload_kind.value,
            "recommended_backend": self.recommended_backend,
            "recommended_model_id": self.recommended_model_id,
            "budget_status": self.budget_status.value,
            "approval_required": self.approval_required,
            "changed_by_cost_pressure": self.changed_by_cost_pressure,
            "decision_reasons": list(self.decision_reasons),
            "pressure_adjustments": [adjustment.to_dict() for adjustment in self.pressure_adjustments],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CostResourcePlan(plan_id={self.plan_id!r}, project_id={self.project_id!r}, workload_kind={self.workload_kind!r})"


def plan_cost_resources(request: CostPlanRequest) -> CostResourcePlan:
    """Forecast cost/resource impact and choose the safest planning option.

    Returns:
        CostResourcePlan value produced by plan_cost_resources().
    """
    candidates = tuple(_forecast_candidate(request, option) for option in request.model_options)
    viable = tuple(candidate for candidate in candidates if candidate.budget_status is CostBudgetStatus.WITHIN_BUDGET)
    recommended = _rank_candidates(viable or candidates)[0]
    highest_quality = max(candidates, key=lambda candidate: candidate.quality_score)
    changed_by_pressure = (
        highest_quality.model_id != recommended.model_id or highest_quality.backend != recommended.backend
    )
    pressure_adjustments = _pressure_adjustments(request, recommended, highest_quality, viable)
    plan_status = recommended.budget_status if viable else CostBudgetStatus.APPROVAL_REQUIRED
    approval_required = plan_status is not CostBudgetStatus.WITHIN_BUDGET or (
        changed_by_pressure and request.requires_approval_when_over_budget
    )
    decision_reasons = _decision_reasons(recommended, highest_quality, changed_by_pressure, bool(viable))
    return CostResourcePlan(
        plan_id=request.plan_id,
        project_id=request.project_id,
        workload_kind=request.workload_kind,
        recommended_backend=recommended.backend,
        recommended_model_id=recommended.model_id,
        budget_status=plan_status,
        approval_required=approval_required,
        changed_by_cost_pressure=changed_by_pressure,
        decision_reasons=decision_reasons,
        pressure_adjustments=pressure_adjustments,
        candidates=_rank_candidates(candidates),
    )


def _forecast_candidate(request: CostPlanRequest, option: ModelBackendOption) -> CostPlanCandidate:
    token_cost = (request.resource.input_tokens / 1000.0) * option.input_cost_per_1k_tokens + (
        request.resource.output_tokens / 1000.0
    ) * option.output_cost_per_1k_tokens
    total_latency_ms = (
        option.latency_ms_per_request * request.resource.requests
        + (request.resource.queue_minutes + option.queue_minutes) * 60_000
    )
    gpu_hours = request.resource.gpu_hours
    cpu_core_hours = request.resource.cpu_core_hours
    ram_gb = max(request.resource.ram_gb, option.ram_gb)
    storage_gb = request.resource.storage_gb + _dataset_storage_gb(request.resource.dataset_rows)
    network_gb = request.resource.network_gb + _dataset_network_gb(request.resource.dataset_rows, option)
    queue_minutes = request.resource.queue_minutes + option.queue_minutes
    reasons = _budget_reasons(
        request.budget,
        total_cost_usd=token_cost,
        total_latency_ms=total_latency_ms,
        gpu_hours=gpu_hours,
        ram_gb=ram_gb,
        storage_gb=storage_gb,
        network_gb=network_gb,
        queue_minutes=queue_minutes,
    )
    return CostPlanCandidate(
        backend=option.backend,
        model_id=option.model_id,
        total_cost_usd=round(token_cost, 6),
        token_cost_usd=round(token_cost, 6),
        total_latency_ms=round(total_latency_ms, 3),
        gpu_hours=round(gpu_hours, 3),
        cpu_core_hours=round(cpu_core_hours, 3),
        ram_gb=round(ram_gb, 3),
        storage_gb=round(storage_gb, 3),
        network_gb=round(network_gb, 3),
        queue_minutes=round(queue_minutes, 3),
        quality_score=round(option.quality_score, 3),
        budget_status=CostBudgetStatus.WITHIN_BUDGET if not reasons else CostBudgetStatus.APPROVAL_REQUIRED,
        blocking_reasons=reasons,
    )


def _rank_candidates(candidates: tuple[CostPlanCandidate, ...]) -> tuple[CostPlanCandidate, ...]:
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                candidate.budget_status is not CostBudgetStatus.WITHIN_BUDGET,
                candidate.total_cost_usd,
                candidate.total_latency_ms,
                -candidate.quality_score,
                candidate.backend,
                candidate.model_id,
            ),
        )
    )


def _pressure_adjustments(
    request: CostPlanRequest,
    recommended: CostPlanCandidate,
    highest_quality: CostPlanCandidate,
    viable: tuple[CostPlanCandidate, ...],
) -> tuple[CostPressureAdjustment, ...]:
    adjustments = BoundedList[CostPressureAdjustment](5)
    if highest_quality.model_id != recommended.model_id or highest_quality.backend != recommended.backend:
        action = (
            CostPressureAction.USE_LOCAL_BACKEND
            if recommended.backend.lower() in {"local", "wsl", "linux", "local-gpu"}
            else CostPressureAction.SELECT_LOWER_COST_MODEL
        )
        adjustments.append(
            CostPressureAdjustment(
                action=action,
                reason="budget-pressure-selected-cheaper-viable-candidate",
                before=f"{highest_quality.backend}:{highest_quality.model_id}",
                after=f"{recommended.backend}:{recommended.model_id}",
            )
        )
    if request.resource.dataset_rows and request.resource.storage_gb > request.budget.max_cost_usd * 10:
        adjustments.append(
            CostPressureAdjustment(
                action=CostPressureAction.REDUCE_DATASET_SIZE,
                reason="dataset-storage-pressure-above-cost-proportional-threshold",
                before=str(request.resource.dataset_rows),
                after=str(max(1, request.resource.dataset_rows // 2)),
            )
        )
    if recommended.queue_minutes > (request.budget.max_queue_minutes or math.inf):
        adjustments.append(
            CostPressureAdjustment(
                action=CostPressureAction.QUEUE_FOR_CAPACITY,
                reason="queue-pressure-exceeds-planning-limit",
                before=str(recommended.queue_minutes),
                after=str(request.budget.max_queue_minutes),
            )
        )
    if not viable:
        adjustments.append(
            CostPressureAdjustment(
                action=CostPressureAction.REQUIRE_APPROVAL,
                reason="no-candidate-fits-declared-budget",
                before="unapproved",
                after="approval-required",
            )
        )
    return tuple(adjustments)


def _decision_reasons(
    recommended: CostPlanCandidate,
    highest_quality: CostPlanCandidate,
    changed_by_pressure: bool,
    has_viable: bool,
) -> tuple[str, ...]:
    reasons = BoundedList[str](8, [f"recommended={recommended.backend}:{recommended.model_id}"])
    if changed_by_pressure:
        reasons.append(f"cost-pressure-changed-from={highest_quality.backend}:{highest_quality.model_id}")
    if not has_viable:
        reasons.extend(recommended.blocking_reasons)
        reasons.append("approval-required-before-run")
    return tuple(reasons)


def _budget_reasons(
    budget: BudgetEnvelope,
    *,
    total_cost_usd: float,
    total_latency_ms: float,
    gpu_hours: float,
    ram_gb: float,
    storage_gb: float,
    network_gb: float,
    queue_minutes: float,
) -> tuple[str, ...]:
    checks = (
        ("cost-over-budget", total_cost_usd, budget.max_cost_usd),
        ("latency-over-budget", total_latency_ms, budget.max_latency_ms),
        ("gpu-hours-over-budget", gpu_hours, budget.max_gpu_hours),
        ("queue-over-budget", queue_minutes, budget.max_queue_minutes),
        ("ram-over-budget", ram_gb, budget.max_ram_gb),
        ("storage-over-budget", storage_gb, budget.max_storage_gb),
        ("network-over-budget", network_gb, budget.max_network_gb),
    )
    return tuple(reason for reason, actual, limit in checks if limit is not None and actual > limit)


def _dataset_storage_gb(dataset_rows: int) -> float:
    return dataset_rows * 0.000002


def _dataset_network_gb(dataset_rows: int, option: ModelBackendOption) -> float:
    if option.backend.lower() in {"local", "wsl", "linux", "local-gpu"}:
        return 0.0
    return dataset_rows * 0.000001


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CostResourcePlanningError(f"{field_name}-missing", f"{field_name} must be non-empty")


def _require_positive_int(value: int, field_name: str) -> None:
    if not isinstance(value, int) or value < 1:
        raise CostResourcePlanningError(f"{field_name}-invalid", f"{field_name} must be a positive integer")


def _require_non_negative_int(value: int, field_name: str) -> None:
    if not isinstance(value, int) or value < 0:
        raise CostResourcePlanningError(f"{field_name}-invalid", f"{field_name} must be a non-negative integer")


def _require_finite_non_negative(value: float, field_name: str) -> None:
    if not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise CostResourcePlanningError(f"{field_name}-not-finite", f"{field_name} must be finite")
    if float(value) < 0:
        raise CostResourcePlanningError(f"{field_name}-negative", f"{field_name} must be non-negative")


def _coerce_enum(enum_type: type[Enum], value: Enum | str, reason: str) -> Any:
    if isinstance(value, enum_type):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return enum_type(raw_value)
    except ValueError as exc:
        raise CostResourcePlanningError(reason, f"unknown {enum_type.__name__} value {value!r}") from exc


__all__ = [
    "BudgetEnvelope",
    "CostBudgetStatus",
    "CostPlanCandidate",
    "CostPlanRequest",
    "CostPressureAction",
    "CostPressureAdjustment",
    "CostResourcePlan",
    "CostResourcePlanningError",
    "ModelBackendOption",
    "ResourceEnvelope",
    "WorkloadKind",
    "plan_cost_resources",
]
