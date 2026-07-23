"""Artifact card helpers for completed model foundry jobs."""

from __future__ import annotations

from vetinari.workbench.model_foundry.contracts import (
    BLOCKER_JOB_NOT_COMPLETE,
    FoundryTrainingJob,
    ModelArtifact,
    ModelFoundryError,
    ModelRecipe,
    TrainingJobStatus,
)


def build_model_artifact(
    *,
    artifact_id: str,
    artifact_ref: str,
    recipe: ModelRecipe,
    job: FoundryTrainingJob,
) -> ModelArtifact:
    """Create an immutable artifact card only for completed jobs.

    Returns:
        Newly constructed model artifact value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if job.status != TrainingJobStatus.COMPLETED:
        raise ModelFoundryError(BLOCKER_JOB_NOT_COMPLETE)
    if job.recipe_id != recipe.recipe_id:
        raise ModelFoundryError("job recipe_id does not match recipe")
    return ModelArtifact(
        artifact_id=artifact_id,
        kind=recipe.output_kind,
        recipe_id=recipe.recipe_id,
        job_id=job.job_id,
        artifact_ref=artifact_ref,
        dataset_revision_ids=tuple(dataset.dataset_revision_id for dataset in recipe.dataset_revisions),
        source_card_ids=tuple(card.source_card_id for card in recipe.source_cards),
        eval_gates=recipe.eval_gates,
        receipt_refs=recipe.receipt_refs,
        rollback_target_ref=recipe.rollback_target_ref,
        route_eligible=recipe.route_eligible,
    )
