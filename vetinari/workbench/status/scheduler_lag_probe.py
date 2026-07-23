"""Probe: reports CRITICAL when scheduler queue lag exceeds threshold."""

from __future__ import annotations

import logging

from vetinari.workbench.spine_consumers import record_run_failed
from vetinari.workbench.status.contracts import ProbeResult

logger = logging.getLogger(__name__)

_CRITICAL_LAG_MS = 5000.0
_DEGRADED_LAG_MS = 1000.0


def scheduler_lag_probe(project_id: str, queue_id: str, lag_ms: float) -> ProbeResult:
    """Check whether scheduler queue lag is within acceptable bounds.

    Args:
        project_id: Workbench project owning the scheduler queue.
        queue_id: Identifier for the scheduler queue being checked.
        lag_ms: Current queue lag in milliseconds.

    Returns:
        Critical, degraded, or ok depending on measured lag.
    """
    if lag_ms > _CRITICAL_LAG_MS:
        logger.warning(
            "status probe scheduler_lag project_id=%s queue_id=%s critical lag_ms=%.0f",
            project_id,
            queue_id,
            lag_ms,
        )
        record_run_failed(
            run_id=f"scheduler-lag-{project_id}-{queue_id}",
            kind="scheduler_lag",
            project_id=project_id,
            error=f"queue {queue_id} lag {lag_ms:.0f}ms exceeds {_CRITICAL_LAG_MS:.0f}ms",
        )
        return ProbeResult(
            status="critical",
            message=f"Scheduler lag {lag_ms:.0f}ms exceeds critical threshold {_CRITICAL_LAG_MS:.0f}ms",
            value=lag_ms,
        )
    if lag_ms > _DEGRADED_LAG_MS:
        logger.warning(
            "status probe scheduler_lag project_id=%s queue_id=%s degraded lag_ms=%.0f",
            project_id,
            queue_id,
            lag_ms,
        )
        return ProbeResult(
            status="degraded",
            message=f"Scheduler lag {lag_ms:.0f}ms above normal threshold {_DEGRADED_LAG_MS:.0f}ms",
            value=lag_ms,
        )
    return ProbeResult(status="ok", message=f"Scheduler lag {lag_ms:.0f}ms within bounds", value=lag_ms)
