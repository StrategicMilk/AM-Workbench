"""Probe: reports CRITICAL when active project cost exceeds budget threshold."""

from __future__ import annotations

import logging

from vetinari.workbench.spine_consumers import record_run_failed
from vetinari.workbench.status.contracts import ProbeResult

logger = logging.getLogger(__name__)

_OVERRUN_THRESHOLD = 1.05


def cost_overrun_probe(project_id: str, budget: float, actual_cost: float) -> ProbeResult:
    """Check whether actual cost exceeds budget by more than the threshold.

    Args:
        project_id: Workbench project identifier.
        budget: Allocated budget for the project.
        actual_cost: Current accumulated project cost.

    Returns:
        Critical when over threshold, degraded for invalid budgets, otherwise ok.
    """
    if budget <= 0:
        logger.warning("status probe cost_overrun project_id=%s degraded: invalid budget", project_id)
        return ProbeResult(status="degraded", message="Budget is zero or negative; cannot assess overrun")
    ratio = actual_cost / budget
    if ratio > _OVERRUN_THRESHOLD:
        logger.warning("status probe cost_overrun project_id=%s critical ratio=%.3f", project_id, ratio)
        record_run_failed(
            run_id=f"cost-probe-{project_id}",
            kind="cost_probe",
            project_id=project_id,
            error=f"Cost overrun ratio={ratio:.3f} exceeds threshold={_OVERRUN_THRESHOLD}",
        )
        return ProbeResult(
            status="critical",
            message=f"Cost overrun: {ratio:.1%} of budget used (threshold {_OVERRUN_THRESHOLD:.0%})",
            value=ratio,
        )
    return ProbeResult(status="ok", message=f"Cost within budget: {ratio:.1%} used", value=ratio)
