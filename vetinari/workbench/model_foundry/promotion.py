"""Promotion gates for model foundry artifacts."""

from __future__ import annotations

from vetinari.workbench.model_foundry.contracts import (
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
    FoundryPromotionDecision,
    ModelFoundryPromotionBlocked,
    PromotionRequest,
    TrainingJobStatus,
)
from vetinari.workbench.model_foundry.recipes import recipe_blockers


def evaluate_promotion(request: PromotionRequest) -> FoundryPromotionDecision:
    """Evaluate a foundry artifact promotion without mutating registry state.

    Returns:
        FoundryPromotionDecision value produced by evaluate_promotion().
    """
    blockers = list(recipe_blockers(request.recipe))
    if request.job.status != TrainingJobStatus.COMPLETED:
        blockers.append(BLOCKER_JOB_NOT_COMPLETE)
    if request.job.recipe_id != request.recipe.recipe_id:
        blockers.append(BLOCKER_MISSING_PROVENANCE)
    if request.artifact.recipe_id != request.recipe.recipe_id or request.artifact.job_id != request.job.job_id:
        blockers.append(BLOCKER_MISSING_PROVENANCE)
    if not request.artifact.source_card_ids:
        blockers.append(BLOCKER_MISSING_SOURCE_CARD)
    if not request.artifact.receipt_refs:
        blockers.append(BLOCKER_MISSING_RECEIPT)
    if not request.artifact.rollback_target_ref.strip():
        blockers.append(BLOCKER_MISSING_ROLLBACK)
    if not request.artifact.eval_gates:
        blockers.append(BLOCKER_MISSING_EVALS)
    if any(not gate.passed for gate in request.artifact.eval_gates):
        blockers.append(BLOCKER_FAILED_EVAL)
    if not request.artifact.route_eligible:
        blockers.append(BLOCKER_ROUTE_NOT_ELIGIBLE)
    for dataset in request.recipe.dataset_revisions:
        if not dataset.consent_ref.strip():
            blockers.append(BLOCKER_MISSING_CONSENT)
        if not dataset.license_ref.strip() or dataset.license_ref.lower().startswith(("deny", "incompatible")):
            blockers.append(BLOCKER_INCOMPATIBLE_LICENSE)
        if dataset.pii_taint:
            blockers.append(BLOCKER_PII_TAINT)
    unique_blockers = tuple(dict.fromkeys(blockers))
    return FoundryPromotionDecision(
        request_id=request.request_id,
        approved=not unique_blockers,
        blockers=unique_blockers,
        evidence={
            "artifact_id": request.artifact.artifact_id,
            "recipe_id": request.recipe.recipe_id,
            "job_id": request.job.job_id,
            "target_ref": request.target_ref,
            "eval_count": len(request.artifact.eval_gates),
            "source_card_count": len(request.artifact.source_card_ids),
            "receipt_count": len(request.artifact.receipt_refs),
        },
    )


def promote_or_raise(request: PromotionRequest) -> FoundryPromotionDecision:
    """Return the promotion decision or raise with concrete blockers.

    Returns:
        FoundryPromotionDecision value produced by promote_or_raise().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    decision = evaluate_promotion(request)
    if not decision.approved:
        raise ModelFoundryPromotionBlocked(decision.blockers)
    return decision
