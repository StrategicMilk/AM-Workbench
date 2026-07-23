"""Unified Workbench experiment contracts and gates."""

from __future__ import annotations

from vetinari.workbench.experiments.adapters import (
    build_backend_variant,
    build_fine_tune_variant,
    build_model_variant,
    build_policy_variant,
    build_prompt_variant,
    build_retrieval_variant,
    build_route_variant,
    build_runtime_variant,
    method_kind_metric,
)
from vetinari.workbench.experiments.gates import PromotionGateResult, evaluate_promotion_readiness
from vetinari.workbench.experiments.model import (
    ArtifactRef,
    BudgetSpec,
    ExperimentKind,
    ExperimentMetricSpec,
    ExperimentVariant,
    PromotionReadiness,
    RollbackMetadata,
    SchedulerPolicy,
    TerminationReason,
    VariantKind,
    WorkbenchExperiment,
    WorkbenchExperimentValidationError,
)

__all__ = [
    "ArtifactRef",
    "BudgetSpec",
    "ExperimentKind",
    "ExperimentMetricSpec",
    "ExperimentVariant",
    "PromotionGateResult",
    "PromotionReadiness",
    "RollbackMetadata",
    "SchedulerPolicy",
    "TerminationReason",
    "VariantKind",
    "WorkbenchExperiment",
    "WorkbenchExperimentValidationError",
    "build_backend_variant",
    "build_fine_tune_variant",
    "build_model_variant",
    "build_policy_variant",
    "build_prompt_variant",
    "build_retrieval_variant",
    "build_route_variant",
    "build_runtime_variant",
    "evaluate_promotion_readiness",
    "method_kind_metric",
]
