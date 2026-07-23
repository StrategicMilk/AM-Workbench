"""Admission-control contract for Workbench prosumer resources."""

from __future__ import annotations

import math
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from vetinari.runtime.workbench_scheduler import Lane
from vetinari.workbench.costing import CostBudgetStatus, CostResourcePlan


class ResourceGovernorError(ValueError):
    """Raised when a resource governor input cannot be trusted."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


class ResourceLeaseStatus(str, Enum):
    """Admission result for a workload lease."""

    APPROVED = "approved"
    APPROVAL_REQUIRED = "approval_required"
    DENIED = "denied"


class ResourceWorkloadKind(str, Enum):
    """First-pass workload classes admitted by the governor."""

    INTERACTIVE = "interactive"
    HUB_AGENT = "hub_agent"
    RAG_JOB = "rag_job"
    DOWNLOAD = "download"
    EVAL = "eval"
    TRAINING = "training"


class ResidencyPlacement(str, Enum):
    """Model placement selected by the governor."""

    CPU = "cpu"
    GPU = "gpu"
    DEFERRED = "deferred"


class ResidencyAction(str, Enum):
    """Residency action to satisfy a workload."""

    USE_RESIDENT = "use_resident"
    LOAD = "load"
    CPU_FIRST = "cpu_first"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class MachineProfile:
    """Read-only machine and runtime state used for admission control."""

    profile_id: str
    total_vram_gb: float
    available_vram_gb: float
    total_ram_gb: float
    available_ram_gb: float
    cpu_threads: int
    available_cpu_threads: int
    storage_free_gb: float
    queue_depth: int
    queue_capacity: int
    runtime_status: str
    model_store_status: str
    cloud_fallback_enabled: bool | None
    evidence_ids: tuple[str, ...]
    measured_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.profile_id, "profile_id")
        _require_non_empty(self.measured_at_utc, "measured_at_utc")
        for field_name in (
            "total_vram_gb",
            "available_vram_gb",
            "total_ram_gb",
            "available_ram_gb",
            "storage_free_gb",
        ):
            _require_finite_non_negative(getattr(self, field_name), field_name)
        for field_name in ("cpu_threads", "available_cpu_threads", "queue_depth", "queue_capacity"):
            _require_non_negative_int(getattr(self, field_name), field_name)
        if self.available_vram_gb > self.total_vram_gb:
            raise ResourceGovernorError("vram-state-invalid", "available VRAM exceeds total VRAM")
        if self.available_ram_gb > self.total_ram_gb:
            raise ResourceGovernorError("ram-state-invalid", "available RAM exceeds total RAM")
        if self.available_cpu_threads > self.cpu_threads:
            raise ResourceGovernorError("cpu-state-invalid", "available CPU threads exceed total threads")
        if self.queue_depth > self.queue_capacity:
            raise ResourceGovernorError("queue-state-invalid", "queue depth exceeds capacity")
        if not self.evidence_ids:
            raise ResourceGovernorError("machine-evidence-missing", "machine profile needs evidence_ids")
        if self.cloud_fallback_enabled is None:
            raise ResourceGovernorError("cloud-fallback-unknown", "cloud fallback state must be known")

    @property
    def runtime_ready(self) -> bool:
        return self.runtime_status == "ready" and self.model_store_status == "ready"

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MachineProfile(profile_id={self.profile_id!r}, total_vram_gb={self.total_vram_gb!r}, available_vram_gb={self.available_vram_gb!r})"


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    """Hard admission limits for one operator profile."""

    max_vram_gb: float
    max_ram_gb: float
    max_cpu_threads: int
    max_storage_gb: float
    max_queue_depth: int
    max_context_tokens: int
    max_kv_cache_gb: float
    max_agent_slots: int
    max_rag_jobs: int
    max_downloads: int
    max_eval_jobs: int
    max_training_jobs: int
    interactive_vram_reserve_gb: float = 2.0
    interactive_ram_reserve_gb: float = 4.0

    def __post_init__(self) -> None:
        for field_name in (
            "max_vram_gb",
            "max_ram_gb",
            "max_storage_gb",
            "max_kv_cache_gb",
            "interactive_vram_reserve_gb",
            "interactive_ram_reserve_gb",
        ):
            _require_finite_non_negative(getattr(self, field_name), field_name)
        for field_name in (
            "max_cpu_threads",
            "max_queue_depth",
            "max_context_tokens",
            "max_agent_slots",
            "max_rag_jobs",
            "max_downloads",
            "max_eval_jobs",
            "max_training_jobs",
        ):
            _require_non_negative_int(getattr(self, field_name), field_name)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ResourceBudget(max_vram_gb={self.max_vram_gb!r}, max_ram_gb={self.max_ram_gb!r}, max_cpu_threads={self.max_cpu_threads!r})"


@dataclass(frozen=True, slots=True)
class WorkloadEnvelope:
    """Resource request envelope submitted before scheduler/model execution."""

    workload_id: str
    lane: Lane | str
    workload_kind: ResourceWorkloadKind | str
    model_id: str
    requested_vram_gb: float
    requested_ram_gb: float
    requested_cpu_threads: int
    requested_storage_gb: float
    context_tokens: int
    kv_cache_gb: float
    agent_slots: int = 0
    rag_jobs: int = 0
    downloads: int = 0
    eval_jobs: int = 0
    training_jobs: int = 0
    provenance_ref: str = ""
    authority_ref: str = ""
    safety_tier: str = "standard"
    confidence: float = 1.0
    gpu_required: bool = False
    measured_gpu_benefit: bool = False

    def __post_init__(self) -> None:
        _require_non_empty(self.workload_id, "workload_id")
        _require_non_empty(self.model_id, "model_id")
        _require_non_empty(self.provenance_ref, "provenance_ref")
        _require_non_empty(self.authority_ref, "authority_ref")
        _require_non_empty(self.safety_tier, "safety_tier")
        object.__setattr__(self, "lane", _coerce_enum(Lane, self.lane, "lane-unknown"))
        object.__setattr__(
            self, "workload_kind", _coerce_enum(ResourceWorkloadKind, self.workload_kind, "workload-kind-unknown")
        )
        for field_name in ("requested_vram_gb", "requested_ram_gb", "requested_storage_gb", "kv_cache_gb"):
            _require_finite_non_negative(getattr(self, field_name), field_name)
        for field_name in (
            "requested_cpu_threads",
            "context_tokens",
            "agent_slots",
            "rag_jobs",
            "downloads",
            "eval_jobs",
            "training_jobs",
        ):
            _require_non_negative_int(getattr(self, field_name), field_name)
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ResourceGovernorError("confidence-invalid", "confidence must be between 0 and 1")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkloadEnvelope(workload_id={self.workload_id!r}, lane={self.lane!r}, workload_kind={self.workload_kind!r})"


@dataclass(frozen=True, slots=True)
class ModelResidencyPlan:
    """A planned model placement decision for one resource lease."""

    model_id: str
    placement: ResidencyPlacement
    action: ResidencyAction
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "placement": self.placement.value,
            "action": self.action.value,
            "reasons": list(self.reasons),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ModelResidencyPlan(model_id={self.model_id!r}, placement={self.placement!r}, action={self.action!r})"


@dataclass(frozen=True, slots=True)
class ResourceLease:
    """Admission-control result for a Workbench workload."""

    lease_id: str
    workload_id: str
    lane: Lane
    status: ResourceLeaseStatus
    model_residency: ModelResidencyPlan
    reasons: tuple[str, ...]
    budget_status: str
    machine_profile_id: str | None
    evidence_ids: tuple[str, ...]

    @property
    def approved(self) -> bool:
        return self.status is ResourceLeaseStatus.APPROVED

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "workload_id": self.workload_id,
            "lane": self.lane.value,
            "status": self.status.value,
            "model_residency": self.model_residency.to_dict(),
            "reasons": list(self.reasons),
            "budget_status": self.budget_status,
            "machine_profile_id": self.machine_profile_id,
            "evidence_ids": list(self.evidence_ids),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ResourceLease(lease_id={self.lease_id!r}, workload_id={self.workload_id!r}, lane={self.lane!r})"


class ProsumerResourceGovernor:
    """First-pass admission controller over scheduler, runtime, and cost inputs."""

    def __init__(self, budget: ResourceBudget) -> None:
        self._budget = budget

    def request_lease(
        self,
        workload: WorkloadEnvelope,
        *,
        machine_profile: MachineProfile | None,
        cost_plan: CostResourcePlan | None = None,
    ) -> ResourceLease:
        """Return a fail-closed lease decision for a workload.

        Returns:
            ResourceLease value produced by request_lease().
        """
        if machine_profile is None:
            return self._deny_without_state(workload, "machine-profile-missing")

        blockers = list(_machine_blockers(machine_profile))
        blockers.extend(_budget_blockers(workload, machine_profile, self._budget))
        residency = _plan_model_residency(workload, machine_profile, bool(blockers))

        if blockers:
            return _lease(
                workload,
                status=ResourceLeaseStatus.DENIED,
                residency=residency,
                reasons=tuple(blockers),
                budget_status=CostBudgetStatus.BLOCKED.value,
                machine_profile=machine_profile,
            )

        approval_reasons = list(_approval_reasons(workload, cost_plan))
        if approval_reasons:
            return _lease(
                workload,
                status=ResourceLeaseStatus.APPROVAL_REQUIRED,
                residency=residency,
                reasons=tuple(approval_reasons),
                budget_status=CostBudgetStatus.APPROVAL_REQUIRED.value,
                machine_profile=machine_profile,
            )

        return _lease(
            workload,
            status=ResourceLeaseStatus.APPROVED,
            residency=residency,
            reasons=("admitted",),
            budget_status=CostBudgetStatus.WITHIN_BUDGET.value,
            machine_profile=machine_profile,
        )

    @staticmethod
    def _deny_without_state(workload: WorkloadEnvelope, reason: str) -> ResourceLease:
        residency = ModelResidencyPlan(
            model_id=workload.model_id,
            placement=ResidencyPlacement.DEFERRED,
            action=ResidencyAction.DEFER,
            reasons=(reason,),
        )
        return _lease(
            workload,
            status=ResourceLeaseStatus.DENIED,
            residency=residency,
            reasons=(reason,),
            budget_status=CostBudgetStatus.BLOCKED.value,
            machine_profile=None,
        )


def request_resource_lease(
    workload: WorkloadEnvelope,
    *,
    machine_profile: MachineProfile | None,
    budget: ResourceBudget,
    cost_plan: CostResourcePlan | None = None,
) -> ResourceLease:
    """Convenience entry point for callers that do not keep a governor instance."""
    return ProsumerResourceGovernor(budget).request_lease(
        workload,
        machine_profile=machine_profile,
        cost_plan=cost_plan,
    )


def _machine_blockers(profile: MachineProfile) -> tuple[str, ...]:
    blockers: list[str] = []
    if not profile.runtime_ready:
        blockers.append("runtime-or-model-store-not-ready")
    if profile.queue_depth >= profile.queue_capacity:
        blockers.append("queue-at-capacity")
    return tuple(blockers)


def _budget_blockers(workload: WorkloadEnvelope, profile: MachineProfile, budget: ResourceBudget) -> tuple[str, ...]:
    blockers: list[str] = []
    effective_vram = min(profile.available_vram_gb, budget.max_vram_gb)
    effective_ram = min(profile.available_ram_gb, budget.max_ram_gb)
    if workload.lane is not Lane.INTERACTIVE:
        effective_vram = max(0.0, effective_vram - budget.interactive_vram_reserve_gb)
        effective_ram = max(0.0, effective_ram - budget.interactive_ram_reserve_gb)
    checks = (
        ("vram-over-budget", workload.requested_vram_gb, effective_vram),
        ("ram-over-budget", workload.requested_ram_gb, effective_ram),
        ("storage-over-budget", workload.requested_storage_gb, min(profile.storage_free_gb, budget.max_storage_gb)),
        ("context-over-budget", workload.context_tokens, budget.max_context_tokens),
        ("kv-cache-over-budget", workload.kv_cache_gb, budget.max_kv_cache_gb),
        ("agent-slots-over-budget", workload.agent_slots, budget.max_agent_slots),
        ("rag-jobs-over-budget", workload.rag_jobs, budget.max_rag_jobs),
        ("downloads-over-budget", workload.downloads, budget.max_downloads),
        ("eval-jobs-over-budget", workload.eval_jobs, budget.max_eval_jobs),
        ("training-jobs-over-budget", workload.training_jobs, budget.max_training_jobs),
    )
    for reason, requested, maximum in checks:
        if requested > maximum:
            blockers.append(reason)
    if workload.requested_cpu_threads > min(profile.available_cpu_threads, budget.max_cpu_threads):
        blockers.append("cpu-threads-over-budget")
    if profile.queue_depth >= budget.max_queue_depth:
        blockers.append("queue-over-budget")
    return tuple(blockers)


def _approval_reasons(workload: WorkloadEnvelope, cost_plan: CostResourcePlan | None) -> tuple[str, ...]:
    reasons: list[str] = []
    if workload.confidence < 0.75:
        reasons.append("confidence-below-auto-admit-threshold")
    if cost_plan is not None and cost_plan.approval_required:
        reasons.append("cost-plan-requires-approval")
    return tuple(reasons)


def _plan_model_residency(
    workload: WorkloadEnvelope,
    profile: MachineProfile,
    has_blockers: bool,
) -> ModelResidencyPlan:
    if has_blockers:
        return ModelResidencyPlan(
            model_id=workload.model_id,
            placement=ResidencyPlacement.DEFERRED,
            action=ResidencyAction.DEFER,
            reasons=("resource-blocked",),
        )
    if not workload.gpu_required and not workload.measured_gpu_benefit and _small_specialist(workload):
        return ModelResidencyPlan(
            model_id=workload.model_id,
            placement=ResidencyPlacement.CPU,
            action=ResidencyAction.CPU_FIRST,
            reasons=("small-specialist-cpu-first",),
        )
    if workload.requested_vram_gb <= profile.available_vram_gb:
        return ModelResidencyPlan(
            model_id=workload.model_id,
            placement=ResidencyPlacement.GPU,
            action=ResidencyAction.USE_RESIDENT if workload.requested_vram_gb == 0 else ResidencyAction.LOAD,
            reasons=("gpu-placement-within-headroom",),
        )
    return ModelResidencyPlan(
        model_id=workload.model_id,
        placement=ResidencyPlacement.DEFERRED,
        action=ResidencyAction.DEFER,
        reasons=("gpu-headroom-unavailable",),
    )


def _small_specialist(workload: WorkloadEnvelope) -> bool:
    return (
        workload.lane is Lane.HUB_AGENT
        and workload.context_tokens <= 16_000
        and workload.requested_vram_gb <= 4.0
        and workload.requested_cpu_threads <= 4
    )


def _lease(
    workload: WorkloadEnvelope,
    *,
    status: ResourceLeaseStatus,
    residency: ModelResidencyPlan,
    reasons: tuple[str, ...],
    budget_status: str,
    machine_profile: MachineProfile | None,
) -> ResourceLease:
    return ResourceLease(
        lease_id=uuid.uuid4().hex,
        workload_id=workload.workload_id,
        lane=workload.lane,
        status=status,
        model_residency=residency,
        reasons=reasons,
        budget_status=budget_status,
        machine_profile_id=machine_profile.profile_id if machine_profile else None,
        evidence_ids=machine_profile.evidence_ids if machine_profile else (),
    )


def _coerce_enum(enum_type: type[Enum], value: Enum | str, reason: str) -> Any:
    if isinstance(value, enum_type):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return enum_type(str(raw_value))
    except ValueError as exc:
        raise ResourceGovernorError(reason, str(value)) from exc


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ResourceGovernorError(f"{field_name}-missing", f"{field_name} must be non-empty")


def _require_non_negative_int(value: int, field_name: str) -> None:
    if not isinstance(value, int) or value < 0:
        raise ResourceGovernorError(f"{field_name}-invalid", f"{field_name} must be a non-negative integer")


def _require_finite_non_negative(value: float, field_name: str) -> None:
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ResourceGovernorError(f"{field_name}-invalid", f"{field_name} must be finite and non-negative")


def lease_payload(lease: ResourceLease) -> dict[str, Any]:
    """Return a JSON-schema-friendly lease payload."""
    return asdict(lease) | lease.to_dict()


__all__ = [
    "MachineProfile",
    "ModelResidencyPlan",
    "ProsumerResourceGovernor",
    "ResidencyAction",
    "ResidencyPlacement",
    "ResourceBudget",
    "ResourceGovernorError",
    "ResourceLease",
    "ResourceLeaseStatus",
    "ResourceWorkloadKind",
    "WorkloadEnvelope",
    "lease_payload",
    "request_resource_lease",
]
