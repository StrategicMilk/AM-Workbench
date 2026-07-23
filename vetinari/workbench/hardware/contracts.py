"""Contracts for the Workbench hardware digital twin."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, cast

from vetinari.api.responses import json_safe as _json_safe


class HardwareTwinError(ValueError):
    """Raised when hardware twin state cannot be trusted."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


class MeasurementStatus(str, Enum):
    """Trust state for one measured observation."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


class ObservationKind(str, Enum):
    """Benchmark categories required by the hardware twin charter."""

    CPU = "cpu"
    RAM = "ram"
    DISK = "disk"
    GPU_VRAM = "gpu_vram"
    MODEL_LOAD = "model_load"
    EMBEDDING_VECTOR_SEARCH = "embedding_vector_search"
    WINDOWS_WSL_PATH = "windows_wsl_path"
    SERVICE_RESIDENCY = "service_residency"
    THERMAL_POWER = "thermal_power"
    RUNTIME_VERSION = "runtime_version"


class OptimizationScope(str, Enum):
    """Surfaces the optimizer may advise without mutating the host."""

    MODEL_RESIDENCY = "model_residency"
    BACKEND_FLAGS = "backend_flags"
    STORAGE_TIERING = "storage_tiering"
    IDLE_TIME_WORK = "idle_time_work"
    UPGRADE_ADVICE = "upgrade_advice"
    OS_RECOMMENDATION = "os_recommendation"
    LOCAL_SERVICE_BROKERING = "local_service_brokering"
    FLEXIBLE_ROUTING = "flexible_routing"
    SCHEDULING = "scheduling"
    CONTEXT_STRATEGY = "context_strategy"


class ProposalRisk(str, Enum):
    """Risk level for an optimization proposal."""

    SAFE_ADAPTATION = "safe_adaptation"
    SAFE_HOST_IMPROVEMENT = "safe_host_improvement"
    RISKY_HOST_CHANGE = "risky_host_change"


REQUIRED_OBSERVATION_KINDS: frozenset[ObservationKind] = frozenset(ObservationKind)


@dataclass(frozen=True, slots=True)
class MeasurementObservation:
    """One JSON-safe measured hardware observation."""

    kind: ObservationKind | str
    status: MeasurementStatus | str
    value: float | int | None
    unit: str
    evidence_id: str
    measured_at_utc: str
    details: dict[str, Any] = field(default_factory=dict)
    stale: bool = False

    def __post_init__(self) -> None:
        kind = _coerce_enum(ObservationKind, self.kind, "observation-kind-unknown")
        status = _coerce_enum(MeasurementStatus, self.status, "measurement-status-unknown")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(
            self,
            "status",
            status,
        )
        _require_text(self.evidence_id, "evidence_id")
        _require_text(self.measured_at_utc, "measured_at_utc")
        if status is MeasurementStatus.READY:
            _require_finite_number(self.value, f"{kind.value}.value")
            if self.stale:
                raise HardwareTwinError("ready-observation-stale", kind.value)
        elif self.value is not None:
            _require_finite_number(self.value, f"{kind.value}.value")
        if not isinstance(self.details, dict):
            raise HardwareTwinError("observation-details-invalid", kind.value)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe observation payload."""
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MeasurementObservation(kind={self.kind!r}, status={self.status!r}, value={self.value!r})"


@dataclass(frozen=True, slots=True)
class RuntimeFingerprint:
    """Host/runtime fingerprint used for drift checks."""

    cpu_signature: str
    ram_signature: str
    gpu_device: str
    storage_signature: str
    driver_version: str
    firmware_version: str
    cuda_version: str
    wsl_version: str
    docker_version: str
    model_server_version: str
    model_store_path: str
    evidence_id: str

    def __post_init__(self) -> None:
        for field_name in (
            "cpu_signature",
            "ram_signature",
            "gpu_device",
            "storage_signature",
            "driver_version",
            "firmware_version",
            "cuda_version",
            "wsl_version",
            "docker_version",
            "model_server_version",
            "model_store_path",
            "evidence_id",
        ):
            _require_text(getattr(self, field_name), field_name)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe fingerprint."""
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RuntimeFingerprint(cpu_signature={self.cpu_signature!r}, ram_signature={self.ram_signature!r}, gpu_device={self.gpu_device!r})"


@dataclass(frozen=True, slots=True)
class HardwareTwinSnapshot:
    """Trusted or degraded digital twin snapshot."""

    snapshot_id: str
    project_id: str
    generated_at_utc: str
    observations: tuple[MeasurementObservation, ...]
    fingerprint: RuntimeFingerprint | None
    evidence_ids: tuple[str, ...]
    status: MeasurementStatus | str = MeasurementStatus.READY
    degradation_reasons: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_text(self.snapshot_id, "snapshot_id")
        _require_text(self.project_id, "project_id")
        _require_text(self.generated_at_utc, "generated_at_utc")
        object.__setattr__(
            self,
            "status",
            _coerce_enum(MeasurementStatus, self.status, "snapshot-status-unknown"),
        )
        if self.schema_version != 1:
            raise HardwareTwinError("schema-version-unsupported", str(self.schema_version))
        if not self.observations:
            raise HardwareTwinError("observations-missing", "at least one observation is required")
        if any(not isinstance(observation, MeasurementObservation) for observation in self.observations):
            raise HardwareTwinError("observation-invalid", "observations must contain MeasurementObservation")
        _require_text_tuple(self.evidence_ids, "evidence_ids")

        kinds = [cast(ObservationKind, observation.kind) for observation in self.observations]
        duplicate_count = len(kinds) - len(set(kinds))
        missing = sorted(kind.value for kind in REQUIRED_OBSERVATION_KINDS - set(kinds))
        reasons = list(self.degradation_reasons)
        if duplicate_count:
            raise HardwareTwinError("observation-kind-duplicate", str(duplicate_count))
        if missing:
            reasons.append(f"missing-required-categories:{','.join(missing)}")
        if self.fingerprint is None:
            reasons.append("runtime-fingerprint-missing")
        if any(observation.status is not MeasurementStatus.READY for observation in self.observations):
            reasons.append("observation-not-ready")
        if any(observation.stale for observation in self.observations):
            reasons.append("observation-stale")

        normalized_reasons = tuple(dict.fromkeys(reason for reason in reasons if reason))
        object.__setattr__(self, "degradation_reasons", normalized_reasons)
        if normalized_reasons and self.status is MeasurementStatus.READY:
            object.__setattr__(self, "status", MeasurementStatus.DEGRADED)
        if self.status is MeasurementStatus.READY and set(kinds) != REQUIRED_OBSERVATION_KINDS:
            raise HardwareTwinError("snapshot-ready-without-required-categories")

    @property
    def ready(self) -> bool:
        """Whether the snapshot is fully trusted for ready recommendations."""
        return self.status is MeasurementStatus.READY and not self.degradation_reasons

    def observation(self, kind: ObservationKind | str) -> MeasurementObservation:
        """Return the observation for a category, failing closed when absent.

        Returns:
            MeasurementObservation value produced by observation().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        selected = _coerce_enum(ObservationKind, kind, "observation-kind-unknown")
        for observation in self.observations:
            if observation.kind is selected:
                return observation
        raise HardwareTwinError("observation-missing", selected.value)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe snapshot."""
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"HardwareTwinSnapshot(snapshot_id={self.snapshot_id!r}, project_id={self.project_id!r}, generated_at_utc={self.generated_at_utc!r})"


@dataclass(frozen=True, slots=True)
class DriftChange:
    """One changed runtime or hardware fingerprint field."""

    field: str
    before: str
    after: str
    evidence_id: str
    requires_rebenchmark: bool = True

    def __post_init__(self) -> None:
        for field_name in ("field", "before", "after", "evidence_id"):
            _require_text(getattr(self, field_name), field_name)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe drift change."""
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DriftChange(field={self.field!r}, before={self.before!r}, after={self.after!r})"


@dataclass(frozen=True, slots=True)
class HardwareDriftReport:
    """Runtime drift result for a baseline/current comparison."""

    status: MeasurementStatus | str
    changes: tuple[DriftChange, ...]
    evidence_ids: tuple[str, ...]
    rebenchmark_required: bool
    degradation_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status",
            _coerce_enum(MeasurementStatus, self.status, "drift-status-unknown"),
        )
        _require_text_tuple(self.evidence_ids, "evidence_ids")
        if self.status is MeasurementStatus.READY and self.degradation_reasons:
            raise HardwareTwinError("ready-drift-with-degradation")
        if self.status is MeasurementStatus.READY and self.rebenchmark_required and not self.changes:
            raise HardwareTwinError("rebenchmark-without-change")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe drift report."""
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"HardwareDriftReport(status={self.status!r}, changes={self.changes!r}, evidence_ids={self.evidence_ids!r})"
        )


@dataclass(frozen=True, slots=True)
class OptimizationProposal:
    """Governed advisory optimization proposal."""

    proposal_id: str
    scope: OptimizationScope | str
    risk: ProposalRisk | str
    title: str
    rationale: str
    affected_surface: str
    confidence: float
    evidence_ids: tuple[str, ...]
    before_measurement_evidence_ids: tuple[str, ...]
    expected_after_evidence_requirements: tuple[str, ...]
    review_required: bool
    rollback_notes: str
    status: str = "ready"
    locally_executable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", _coerce_enum(OptimizationScope, self.scope, "optimization-scope-unknown"))
        object.__setattr__(self, "risk", _coerce_enum(ProposalRisk, self.risk, "proposal-risk-unknown"))
        for field_name in ("proposal_id", "title", "rationale", "affected_surface", "status"):
            _require_text(getattr(self, field_name), field_name)
        if self.status not in {"ready", "blocked", "review_required", "degraded"}:
            raise HardwareTwinError("proposal-status-unknown", self.status)
        _require_probability(self.confidence, "confidence")
        _require_text_tuple(self.evidence_ids, "evidence_ids")
        _require_text_tuple(self.before_measurement_evidence_ids, "before_measurement_evidence_ids")
        _require_text_tuple(self.expected_after_evidence_requirements, "expected_after_evidence_requirements")
        if self.risk is ProposalRisk.RISKY_HOST_CHANGE:
            if not self.review_required:
                raise HardwareTwinError("risky-host-change-review-required", self.proposal_id)
            _require_text(self.rollback_notes, "rollback_notes")
            if self.locally_executable:
                raise HardwareTwinError("risky-host-change-locally-executable", self.proposal_id)
            if self.status != "review_required":
                raise HardwareTwinError("risky-host-change-status-invalid", self.proposal_id)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe proposal."""
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OptimizationProposal(proposal_id={self.proposal_id!r}, scope={self.scope!r}, risk={self.risk!r})"


def utc_now_seconds() -> str:
    """Return current UTC timestamp with second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _coerce_enum(enum_type: type[Enum], value: Enum | str, reason: str) -> Any:
    if isinstance(value, enum_type):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return enum_type(str(raw_value))
    except ValueError as exc:
        raise HardwareTwinError(reason, str(value)) from exc


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise HardwareTwinError(f"{field_name}-missing", f"{field_name} must be non-empty")


def _require_text_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not value:
        raise HardwareTwinError(f"{field_name}-missing", f"{field_name} must be non-empty")
    for item in value:
        _require_text(item, f"{field_name} entry")


def _require_finite_number(value: object, field_name: str) -> None:
    if not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise HardwareTwinError(f"{field_name}-not-finite", f"{field_name} must be finite")


def _require_probability(value: object, field_name: str) -> None:
    _require_finite_number(value, field_name)
    numeric_value = cast(int | float, value)
    if not 0.0 <= float(numeric_value) <= 1.0:
        raise HardwareTwinError(f"{field_name}-out-of-range", f"{field_name} must be between 0 and 1")


__all__ = [
    "REQUIRED_OBSERVATION_KINDS",
    "DriftChange",
    "HardwareDriftReport",
    "HardwareTwinError",
    "HardwareTwinSnapshot",
    "MeasurementObservation",
    "MeasurementStatus",
    "ObservationKind",
    "OptimizationProposal",
    "OptimizationScope",
    "ProposalRisk",
    "RuntimeFingerprint",
    "utc_now_seconds",
]
