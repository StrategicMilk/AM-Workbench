"""Fail-closed promotion readiness gate for Workbench experiments."""

from __future__ import annotations

import math
from dataclasses import dataclass

from vetinari.workbench.evals import EvalResult
from vetinari.workbench.experiments.model import PromotionReadiness, WorkbenchExperiment


@dataclass(frozen=True, slots=True)
class PromotionGateResult:
    """Structured promotion gate result."""

    readiness: PromotionReadiness
    reasons: tuple[str, ...]

    @property
    def can_promote(self) -> bool:
        return self.readiness is PromotionReadiness.ELIGIBLE


def evaluate_promotion_readiness(
    experiment: WorkbenchExperiment,
    *,
    eval_evidence: tuple[EvalResult, ...] = (),
    confidence: float | None = None,
    cost_usd: float | None = None,
    resource_observations: dict[str, float] | None = None,
    rollback_verified: bool = False,
) -> PromotionGateResult:
    """Evaluate promotion readiness without claiming promotion authority.

    Returns:
        PromotionGateResult value produced by evaluate_promotion_readiness().
    """
    reasons: list[str] = []
    if not isinstance(experiment, WorkbenchExperiment):
        return PromotionGateResult(PromotionReadiness.BLOCKED, ("experiment-unreadable",))
    if not eval_evidence:
        reasons.append("eval-evidence-missing")
    elif any(not isinstance(row, EvalResult) for row in eval_evidence):
        reasons.append("eval-evidence-unreadable")
    elif not all(score.passed for row in eval_evidence for score in row.scores):
        reasons.append("eval-evidence-failing")
    if confidence is None:
        reasons.append("confidence-missing")
    elif not math.isfinite(confidence):
        reasons.append("confidence-unreadable")
    elif confidence < experiment.confidence_threshold:
        reasons.append("confidence-below-threshold")
    if cost_usd is None:
        reasons.append("cost-data-missing")
    elif not math.isfinite(cost_usd):
        reasons.append("cost-data-unreadable")
    elif cost_usd < 0 or cost_usd > experiment.budget.max_cost_usd:
        reasons.append("cost-limit-failed")
    observations = resource_observations or {}
    if experiment.resource_limits and not observations:
        reasons.append("resource-data-missing")
    for name, limit in experiment.resource_limits.items():
        value = observations.get(name)
        if value is None:
            reasons.append(f"resource-{name}-missing")
        elif not math.isfinite(value):
            reasons.append(f"resource-{name}-unreadable")
        elif value > limit:
            reasons.append(f"resource-{name}-limit-failed")
    if experiment.rollback is None:
        reasons.append("rollback-metadata-missing")
    elif not rollback_verified:
        reasons.append("rollback-verification-missing")
    if reasons:
        return PromotionGateResult(PromotionReadiness.BLOCKED, tuple(reasons))
    return PromotionGateResult(PromotionReadiness.ELIGIBLE, ("eligible",))


__all__ = ["PromotionGateResult", "evaluate_promotion_readiness"]
