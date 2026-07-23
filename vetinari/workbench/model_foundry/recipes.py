"""Recipe helpers for the Workbench model foundry."""

from __future__ import annotations

from vetinari.workbench.model_foundry.contracts import (
    BLOCKER_BUDGET_UNAVAILABLE,
    BLOCKER_FAILED_EVAL,
    BLOCKER_INCOMPATIBLE_LICENSE,
    BLOCKER_MISSING_CONSENT,
    BLOCKER_MISSING_EVALS,
    BLOCKER_MISSING_RECEIPT,
    BLOCKER_MISSING_ROLLBACK,
    BLOCKER_MISSING_SOURCE_CARD,
    BLOCKER_PII_TAINT,
    BLOCKER_ROUTE_NOT_ELIGIBLE,
    ModelDevelopmentStrategy,
    ModelRecipe,
)


def recipe_blockers(recipe: ModelRecipe) -> tuple[str, ...]:
    """Return deterministic fail-closed blockers for a recipe.

    Returns:
        tuple[str, ...] value produced by recipe_blockers().
    """
    blockers: list[str] = []
    if not recipe.budget_ref.strip():
        blockers.append(BLOCKER_BUDGET_UNAVAILABLE)
    if not recipe.receipt_refs:
        blockers.append(BLOCKER_MISSING_RECEIPT)
    if not recipe.rollback_target_ref.strip():
        blockers.append(BLOCKER_MISSING_ROLLBACK)
    if not recipe.source_cards:
        blockers.append(BLOCKER_MISSING_SOURCE_CARD)
    for dataset in recipe.dataset_revisions:
        if not dataset.consent_ref.strip():
            blockers.append(BLOCKER_MISSING_CONSENT)
        if not dataset.license_ref.strip() or dataset.license_ref.lower().startswith(("deny", "incompatible")):
            blockers.append(BLOCKER_INCOMPATIBLE_LICENSE)
        if dataset.pii_taint:
            blockers.append(BLOCKER_PII_TAINT)
    if not recipe.eval_gates:
        blockers.append(BLOCKER_MISSING_EVALS)
    if any(not gate.passed for gate in recipe.eval_gates):
        blockers.append(BLOCKER_FAILED_EVAL)
    if recipe.strategy is ModelDevelopmentStrategy.ROUTE_ELIGIBILITY and not recipe.route_eligible:
        blockers.append(BLOCKER_ROUTE_NOT_ELIGIBLE)
    return tuple(dict.fromkeys(blockers))


def route_eligibility_blockers(recipe: ModelRecipe) -> tuple[str, ...]:
    """Return blockers specific to route eligibility.

    Returns:
        Outcome produced by route_eligibility_blockers().
    """
    blockers = list(recipe_blockers(recipe))
    if not recipe.route_eligible:
        blockers.append(BLOCKER_ROUTE_NOT_ELIGIBLE)
    return tuple(dict.fromkeys(blockers))
