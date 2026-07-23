"""Job planning and feasibility dry-runs for model foundry recipes."""

from __future__ import annotations

from datetime import datetime, timezone

from vetinari.workbench.model_foundry.contracts import (
    FeasibilityEstimate,
    FoundryTrainingJob,
    ModelFoundryError,
    ModelRecipe,
    TrainingJobStatus,
)
from vetinari.workbench.model_foundry.recipes import recipe_blockers


def estimate_feasibility(
    recipe: ModelRecipe,
    *,
    available_hardware: str,
    available_storage_gb: float,
    requested_tokens: int,
) -> FeasibilityEstimate:
    """Estimate parameters, tokens, hardware, storage, cadence, and stop rules.

    Returns:
        Estimated feasibility result.
    """
    parameters = (
        recipe.scratch_spec.parameter_count if recipe.scratch_spec is not None else _derived_parameter_count(recipe)
    )
    storage_gb = max(1.0, (parameters * 2.0) / 1_000_000_000)
    wall_clock_hours = max(0.1, (parameters * requested_tokens) / 2_000_000_000_000)
    cadence = max(100, requested_tokens // 20)
    blockers: list[str] = []
    if available_storage_gb < storage_gb:
        blockers.append("storage_insufficient")
    if not available_hardware.strip():
        blockers.append("hardware_unavailable")
    stop_conditions = (
        "stop_on_eval_regression",
        "stop_on_budget_exhaustion",
        "stop_on_safety_blocker",
        "stop_on_checkpoint_corruption",
    )
    return FeasibilityEstimate(
        estimate_id=f"estimate:{recipe.recipe_id}",
        parameters=parameters,
        training_tokens=requested_tokens,
        hardware=available_hardware,
        storage_gb=storage_gb,
        checkpoint_cadence_steps=cadence,
        estimated_wall_clock_hours=wall_clock_hours,
        stop_conditions=stop_conditions,
        blockers=tuple(blockers),
    )


def plan_training_job(
    recipe: ModelRecipe,
    *,
    job_id: str,
    estimate: FeasibilityEstimate,
    created_at_utc: str | None = None,
) -> FoundryTrainingJob:
    """Create a scheduler-facing job plan without starting training.

    Returns:
        FoundryTrainingJob value produced by plan_training_job().
    """
    blockers = tuple(dict.fromkeys((*recipe_blockers(recipe), *estimate.blockers)))
    status = TrainingJobStatus.BLOCKED if blockers else TrainingJobStatus.READY
    return FoundryTrainingJob(
        job_id=job_id,
        recipe_id=recipe.recipe_id,
        strategy=recipe.strategy,
        status=status,
        estimate=estimate,
        blockers=blockers,
        receipt_refs=recipe.receipt_refs,
        created_at_utc=created_at_utc or datetime.now(timezone.utc).isoformat(),
    )


def _derived_parameter_count(recipe: ModelRecipe) -> int:
    if recipe.base_model_parameter_count is None:
        raise ModelFoundryError("base_model_parameter_count is required for non-scratch feasibility estimates")
    return recipe.base_model_parameter_count
