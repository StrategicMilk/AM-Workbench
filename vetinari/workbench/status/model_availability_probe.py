"""Probe: reports CRITICAL when a required model is unavailable for serving."""

from __future__ import annotations

import logging

from vetinari.workbench.spine_consumers import record_run_failed
from vetinari.workbench.status.contracts import ProbeResult

logger = logging.getLogger(__name__)


def model_availability_probe(
    project_id: str,
    model_id: str,
    is_available: bool,
    error_message: str = "",
) -> ProbeResult:
    """Check whether a required model is currently available.

    Args:
        project_id: Workbench project requiring this model.
        model_id: Identifier of the model to check.
        is_available: True if the model is loaded and serving.
        error_message: Optional error details when unavailable.

    Returns:
        Critical when unavailable, otherwise ok.
    """
    if not is_available:
        logger.warning(
            "status probe model_availability project_id=%s model_id=%s critical: %s",
            project_id,
            model_id,
            error_message or "unavailable",
        )
        record_run_failed(
            run_id=f"model-probe-{project_id}-{model_id}",
            kind="model_availability",
            project_id=project_id,
            error=error_message or f"Model '{model_id}' unavailable",
        )
        return ProbeResult(
            status="critical",
            message=f"Model '{model_id}' is not available: {error_message or 'no details'}",
            value=0.0,
        )
    return ProbeResult(status="ok", message=f"Model '{model_id}' is available", value=1.0)
