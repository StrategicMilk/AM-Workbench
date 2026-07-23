import CostResourcePlannerPanel from './CostResourcePlannerPanel.svelte';

export { CostResourcePlannerPanel };

export function summarizeCostResourcePlan(plan) {
  const candidates = plan?.candidates ?? [];
  const finiteCosts = candidates
    .map((candidate) => Number(candidate.total_cost_usd))
    .filter((value) => Number.isFinite(value));
  return {
    planId: plan?.plan_id ?? '',
    approvalRequired: Boolean(plan?.approval_required),
    changedByCostPressure: Boolean(plan?.changed_by_cost_pressure),
    candidateCount: candidates.length,
    recommended: `${plan?.recommended_backend ?? ''}:${plan?.recommended_model_id ?? ''}`,
    lowestCostUsd: finiteCosts.length ? Math.min(...finiteCosts) : null,
  };
}
