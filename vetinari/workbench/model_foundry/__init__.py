"""Workbench model foundry contracts."""

from __future__ import annotations

from importlib import import_module
from types import ModuleType

from vetinari.workbench.model_foundry.artifacts import build_model_artifact
from vetinari.workbench.model_foundry.contracts import (
    BLOCKER_BUDGET_UNAVAILABLE,
    BLOCKER_FAILED_EVAL,
    BLOCKER_INCOMPATIBLE_LICENSE,
    BLOCKER_JOB_NOT_COMPLETE,
    BLOCKER_MISSING_CONSENT,
    BLOCKER_MISSING_EVALS,
    BLOCKER_MISSING_PROVENANCE,
    BLOCKER_MISSING_RECEIPT,
    BLOCKER_MISSING_ROLLBACK,
    BLOCKER_MISSING_SOURCE_CARD,
    BLOCKER_PII_TAINT,
    BLOCKER_ROUTE_NOT_ELIGIBLE,
    DatasetRevisionRef,
    EvalGate,
    FeasibilityEstimate,
    FoundryPromotionDecision,
    FoundryTrainingJob,
    ModelArtifact,
    ModelArtifactKind,
    ModelDevelopmentStrategy,
    ModelFoundryError,
    ModelFoundryPromotionBlocked,
    ModelRecipe,
    PromotionRequest,
    ScratchModelSpec,
    SourceCardRef,
    TokenizerKind,
    TokenizerSpec,
    TrainingJobStatus,
    to_jsonable,
)
from vetinari.workbench.model_foundry.jobs import estimate_feasibility, plan_training_job
from vetinari.workbench.model_foundry.promotion import evaluate_promotion, promote_or_raise
from vetinari.workbench.model_foundry.recipes import recipe_blockers, route_eligibility_blockers

_LAZY_SUBMODULES = {
    "tiny_artifacts",
    "tiny_trainer",
    "tokenizer",
    "utility_recipes",
}


def __getattr__(name: str) -> ModuleType:
    if name not in _LAZY_SUBMODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{name}")
    globals()[name] = module
    return module


__all__ = [
    "BLOCKER_BUDGET_UNAVAILABLE",
    "BLOCKER_FAILED_EVAL",
    "BLOCKER_INCOMPATIBLE_LICENSE",
    "BLOCKER_JOB_NOT_COMPLETE",
    "BLOCKER_MISSING_CONSENT",
    "BLOCKER_MISSING_EVALS",
    "BLOCKER_MISSING_PROVENANCE",
    "BLOCKER_MISSING_RECEIPT",
    "BLOCKER_MISSING_ROLLBACK",
    "BLOCKER_MISSING_SOURCE_CARD",
    "BLOCKER_PII_TAINT",
    "BLOCKER_ROUTE_NOT_ELIGIBLE",
    "DatasetRevisionRef",
    "EvalGate",
    "FeasibilityEstimate",
    "FoundryPromotionDecision",
    "FoundryTrainingJob",
    "ModelArtifact",
    "ModelArtifactKind",
    "ModelDevelopmentStrategy",
    "ModelFoundryError",
    "ModelFoundryPromotionBlocked",
    "ModelRecipe",
    "PromotionRequest",
    "ScratchModelSpec",
    "SourceCardRef",
    "TokenizerKind",
    "TokenizerSpec",
    "TrainingJobStatus",
    "build_model_artifact",
    "estimate_feasibility",
    "evaluate_promotion",
    "plan_training_job",
    "promote_or_raise",
    "recipe_blockers",
    "route_eligibility_blockers",
    "to_jsonable",
]
