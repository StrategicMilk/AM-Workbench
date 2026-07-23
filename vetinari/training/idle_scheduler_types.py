"""Shared data types and constants for idle-time training scheduling."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib.util import find_spec

from vetinari.types import StatusEnum

# Polling interval for the background scheduler loop
POLL_INTERVAL_SECONDS = 60

# Minimum free VRAM required before starting a training cycle (in GB)
MIN_FREE_VRAM_GB = 8.0

# Minimum number of training records required before starting a training cycle
MIN_TRAINING_RECORDS = 100

# Maximum age for scratch outputs before idle maintenance removes them.
DEFAULT_OUTPUTS_SCRATCH_TTL_DAYS = 14
OUTPUTS_SCRATCH_TTL_DAYS = DEFAULT_OUTPUTS_SCRATCH_TTL_DAYS


def get_outputs_scratch_ttl_days() -> int:
    """Return the scratch-output TTL from the environment at runtime.

    Returns:
        Number of days before scratch outputs expire.
    """
    from vetinari.utils.lazy_config import env_int

    return env_int("VETINARI_OUTPUTS_SCRATCH_TTL_DAYS", DEFAULT_OUTPUTS_SCRATCH_TTL_DAYS)


@dataclass
class IdleTrainingJob:
    """Represents a single idle-time training job.

    Attributes:
        job_id: Unique identifier for the job.
        status: Current status, one of "pending", "running", "paused",
            "completed", or "failed".
        activity_description: Human-readable description of the training activity.
        started_at: ISO-8601 UTC timestamp when the job started.
        task_type: Optional targeted task or skill type for this training run.
        progress: Fraction complete in [0.0, 1.0].
    """

    job_id: str
    status: StatusEnum
    activity_description: str
    started_at: str
    task_type: str | None = None
    progress: float = 0.0

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"TrainingJob(job_id={self.job_id!r}, status={self.status!r}, progress={self.progress!r})"


# Alias for backward compatibility
TrainingJob = IdleTrainingJob


def _require_module(module_name: str) -> None:
    """Raise ModuleNotFoundError when an idle-scheduler dependency is absent.

    Args:
        module_name: Fully qualified module name to verify.

    Raises:
        ModuleNotFoundError: If the module cannot be imported or is explicitly
            blocked in ``sys.modules``.
    """
    if module_name in sys.modules:
        if sys.modules[module_name] is None:
            raise ModuleNotFoundError(module_name)
        return
    try:
        available = find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError) as exc:
        raise ModuleNotFoundError(module_name) from exc
    if not available:
        raise ModuleNotFoundError(module_name)
