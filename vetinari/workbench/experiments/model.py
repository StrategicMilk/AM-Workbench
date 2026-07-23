"""Unified Workbench experiment contract for sweeps and comparisons."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any


class WorkbenchExperimentValidationError(ValueError):
    """Raised when an experiment contract fails closed validation."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


class ExperimentKind(str, Enum):
    """Supported experiment shapes."""

    SWEEP = "sweep"
    CONTROLLED_COMPARISON = "controlled_comparison"


class VariantKind(str, Enum):
    """Variant dimensions normalized into one comparison shape."""

    PROMPT = "prompt"
    MODEL = "model"
    RETRIEVAL = "retrieval"
    ROUTE = "route"
    BACKEND = "backend"
    POLICY = "policy"
    FINE_TUNE = "fine_tune"


class SchedulerPolicy(str, Enum):
    """Deterministic candidate scheduling policies."""

    SEQUENTIAL = "sequential"
    LOWEST_COST_FIRST = "lowest_cost_first"


class TerminationReason(str, Enum):
    """Typed reasons a sweep can stop."""

    NOT_TERMINATED = "not_terminated"
    BUDGET_EXHAUSTED = "budget_exhausted"
    OBJECTIVE_MET = "objective_met"
    MAX_TRIALS_REACHED = "max_trials_reached"
    NO_CANDIDATES = "no_candidates"
    SAFETY_CONSTRAINTS = "safety_constraints"


class PromotionReadiness(str, Enum):
    """Fail-closed promotion readiness states."""

    BLOCKED = "blocked"
    DEGRADED = "degraded"
    ELIGIBLE = "eligible"
    ROLLBACK_REQUIRED = "rollback_required"


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    """Cost, resource, and trial ceilings for one experiment."""

    max_trials: int
    max_cost_usd: float
    max_latency_ms: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.max_trials, int) or self.max_trials < 1:
            raise WorkbenchExperimentValidationError(
                "budget-max-trials-invalid", "max_trials must be a positive integer"
            )
        _require_finite_non_negative(self.max_cost_usd, "max_cost_usd")
        if self.max_latency_ms is not None:
            _require_finite_non_negative(self.max_latency_ms, "max_latency_ms")


@dataclass(frozen=True, slots=True)
class ExperimentMetricSpec:
    """Objective or observed metric under comparison."""

    name: str
    target: float
    unit: str = ""
    higher_is_better: bool = True

    def __post_init__(self) -> None:
        _require_non_empty(self.name, "metric name")
        _require_finite(self.target, "target")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ExperimentMetricSpec(name={self.name!r}, target={self.target!r}, unit={self.unit!r})"


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Stable reference to an upstream artifact without copying it."""

    artifact_id: str
    artifact_kind: str
    revision: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.artifact_id, "artifact_id")
        _require_non_empty(self.artifact_kind, "artifact_kind")


@dataclass(frozen=True, slots=True)
class RollbackMetadata:
    """Rollback target required before an experiment can become promotable."""

    target_ref: str
    owner: str
    tested_at_utc: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.target_ref, "rollback target_ref")
        _require_non_empty(self.owner, "rollback owner")


@dataclass(frozen=True, slots=True)
class ExperimentVariant:
    """One candidate in any prompt/model/retrieval/route/runtime surface."""

    variant_id: str
    kind: VariantKind | str
    parameters: Mapping[str, Any] = field(default_factory=dict)
    asset_refs: tuple[str, ...] = ()
    label: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.variant_id, "variant_id")
        object.__setattr__(self, "kind", _coerce_variant_kind(self.kind))
        if not isinstance(self.parameters, Mapping):
            raise WorkbenchExperimentValidationError("variant-parameters-invalid", "parameters must be a mapping")
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))
        object.__setattr__(self, "asset_refs", _string_tuple(self.asset_refs, "asset_refs", allow_empty=True))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ExperimentVariant(variant_id={self.variant_id!r}, kind={self.kind!r}, parameters={self.parameters!r})"


@dataclass(frozen=True, slots=True)
class WorkbenchExperiment:
    """Unified experiment object shared by sweep, eval, and promotion gates."""

    experiment_id: str
    kind: ExperimentKind | str
    search_space: tuple[ExperimentVariant, ...]
    objective: ExperimentMetricSpec
    constraints: Mapping[str, Any]
    budget: BudgetSpec
    scheduler: SchedulerPolicy | str
    termination_reason: TerminationReason | str
    metrics: tuple[ExperimentMetricSpec, ...]
    artifacts: tuple[ArtifactRef, ...]
    baseline_ref: str
    candidate_ref: str
    confidence_threshold: float
    rollback: RollbackMetadata | None
    promotion_readiness: PromotionReadiness | str = PromotionReadiness.BLOCKED
    cost_limit_usd: float | None = None
    resource_limits: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.experiment_id, "experiment_id")
        object.__setattr__(self, "kind", _coerce_enum(ExperimentKind, self.kind, "experiment-kind-unknown"))
        if not self.search_space:
            raise WorkbenchExperimentValidationError("search-space-missing", "search_space must contain variants")
        for variant in self.search_space:
            if not isinstance(variant, ExperimentVariant):
                raise WorkbenchExperimentValidationError(
                    "search-space-invalid", "search_space must contain ExperimentVariant"
                )
        if not isinstance(self.objective, ExperimentMetricSpec):
            raise WorkbenchExperimentValidationError("objective-invalid", "objective must be ExperimentMetricSpec")
        if not isinstance(self.budget, BudgetSpec):
            raise WorkbenchExperimentValidationError("budget-invalid", "budget must be BudgetSpec")
        object.__setattr__(self, "scheduler", _coerce_enum(SchedulerPolicy, self.scheduler, "scheduler-unknown"))
        object.__setattr__(
            self,
            "termination_reason",
            _coerce_enum(TerminationReason, self.termination_reason, "termination-reason-unknown"),
        )
        object.__setattr__(
            self,
            "promotion_readiness",
            _coerce_enum(PromotionReadiness, self.promotion_readiness, "promotion-readiness-unknown"),
        )
        object.__setattr__(self, "constraints", MappingProxyType(dict(self.constraints)))
        object.__setattr__(self, "resource_limits", MappingProxyType(dict(self.resource_limits)))
        for metric in self.metrics:
            if not isinstance(metric, ExperimentMetricSpec):
                raise WorkbenchExperimentValidationError("metrics-invalid", "metrics must contain ExperimentMetricSpec")
        for artifact in self.artifacts:
            if not isinstance(artifact, ArtifactRef):
                raise WorkbenchExperimentValidationError("artifacts-invalid", "artifacts must contain ArtifactRef")
        _require_non_empty(self.baseline_ref, "baseline_ref")
        _require_non_empty(self.candidate_ref, "candidate_ref")
        _require_finite(self.confidence_threshold, "confidence_threshold")
        if not 0 <= float(self.confidence_threshold) <= 1:
            raise WorkbenchExperimentValidationError(
                "confidence-threshold-invalid", "confidence_threshold must be in [0, 1]"
            )
        if self.cost_limit_usd is not None:
            _require_finite_non_negative(self.cost_limit_usd, "cost_limit_usd")
        for name, limit in self.resource_limits.items():
            _require_non_empty(str(name), "resource limit name")
            _require_finite_non_negative(limit, f"resource limit {name}")
        if (
            self.promotion_readiness in {PromotionReadiness.ELIGIBLE, PromotionReadiness.ROLLBACK_REQUIRED}
            and self.rollback is None
        ):
            raise WorkbenchExperimentValidationError(
                "rollback-metadata-missing", "promotable experiments require rollback"
            )
        if self.rollback is not None and not isinstance(self.rollback, RollbackMetadata):
            raise WorkbenchExperimentValidationError("rollback-invalid", "rollback must be RollbackMetadata")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchExperiment(experiment_id={self.experiment_id!r}, kind={self.kind!r}, search_space={self.search_space!r})"


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchExperimentValidationError(f"{field_name}-missing", f"{field_name} must be non-empty")


def _require_finite(value: float, field_name: str) -> None:
    if not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise WorkbenchExperimentValidationError(f"{field_name}-not-finite", f"{field_name} must be finite")


def _require_finite_non_negative(value: float, field_name: str) -> None:
    _require_finite(value, field_name)
    if float(value) < 0:
        raise WorkbenchExperimentValidationError(f"{field_name}-negative", f"{field_name} must be non-negative")


def _string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise WorkbenchExperimentValidationError(f"{field_name}-invalid", f"{field_name} must be a tuple")
    if not allow_empty and not values:
        raise WorkbenchExperimentValidationError(f"{field_name}-missing", f"{field_name} must be non-empty")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise WorkbenchExperimentValidationError(
            f"{field_name}-invalid", f"{field_name} entries must be non-empty strings"
        )
    return values


def _coerce_variant_kind(value: VariantKind | str) -> VariantKind:
    return _coerce_enum(VariantKind, value, "variant-kind-unknown")


def _coerce_enum(enum_type: type[Enum], value: Enum | str, reason: str) -> Any:
    if isinstance(value, enum_type):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return enum_type(raw_value)
    except ValueError as exc:
        raise WorkbenchExperimentValidationError(reason, f"unknown {enum_type.__name__} value {value!r}") from exc


__all__ = [
    "ArtifactRef",
    "BudgetSpec",
    "ExperimentKind",
    "ExperimentMetricSpec",
    "ExperimentVariant",
    "PromotionReadiness",
    "RollbackMetadata",
    "SchedulerPolicy",
    "TerminationReason",
    "VariantKind",
    "WorkbenchExperiment",
    "WorkbenchExperimentValidationError",
]
