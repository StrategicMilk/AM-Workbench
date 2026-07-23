"""Training Curriculum Module.

Determines what the system should train on next by evaluating the current
state of skill gaps, defect patterns, available data, and benchmark staleness.
Produces a prioritized TrainingActivity that drives the training pipeline.
"""

from __future__ import annotations

import threading

from .curriculum_candidates import CurriculumCandidateMixin
from .curriculum_execution import CurriculumExecutionMixin
from .curriculum_status import CurriculumStatusMixin
from .curriculum_types import (
    BENCHMARK_STALENESS_DAYS,
    DEFAULT_DURATION_MINUTES,
    DEFAULT_VRAM_GB,
    DEFECT_THRESHOLD,
    DISTILLATION_QUALITY_THRESHOLD,
    MIN_REASONING_EPISODES,
    MIN_RLEF_TRACES,
    WEAK_SKILL_THRESHOLD,
    CurriculumPhase,
    TrainingActivity,
    TrainingActivityType,
    _require_module,
    logger,
)


class TrainingCurriculum(CurriculumStatusMixin, CurriculumExecutionMixin, CurriculumCandidateMixin):
    """Determines what training activity should run next.

    Evaluates the current system state - skill gaps, defect patterns,
    available data, pending A/B tests, and benchmark freshness - and
    returns the highest-priority TrainingActivity.

    Late imports are used for all vetinari submodules so that training
    dependencies remain optional at import time.
    """

    def __init__(self) -> None:
        self._candidates: list[TrainingActivity] = []


# ---------------------------------------------------------------------------
# Module-level get_training_curriculum singleton
# ---------------------------------------------------------------------------
# Exposes the canonical TrainingCurriculum instance so all callers share one
# curriculum state and avoid redundant candidate computation on every request.

_curriculum_instance: TrainingCurriculum | None = None
_curriculum_instance_lock: threading.Lock = threading.Lock()


def get_training_curriculum() -> TrainingCurriculum:
    """Return the canonical TrainingCurriculum singleton.

    Uses double-checked locking so the first call creates the instance and
    all subsequent calls return the same object with no lock contention.

    Returns:
        The shared TrainingCurriculum instance.
    """
    global _curriculum_instance
    if _curriculum_instance is not None:
        return _curriculum_instance
    with _curriculum_instance_lock:
        if _curriculum_instance is not None:
            return _curriculum_instance
        _curriculum_instance = TrainingCurriculum()
    logger.debug("get_training_curriculum: created new singleton")
    return _curriculum_instance


__all__ = [
    "BENCHMARK_STALENESS_DAYS",
    "DEFAULT_DURATION_MINUTES",
    "DEFAULT_VRAM_GB",
    "DEFECT_THRESHOLD",
    "DISTILLATION_QUALITY_THRESHOLD",
    "MIN_REASONING_EPISODES",
    "MIN_RLEF_TRACES",
    "WEAK_SKILL_THRESHOLD",
    "CurriculumPhase",
    "TrainingActivity",
    "TrainingActivityType",
    "TrainingCurriculum",
    "_require_module",
    "get_training_curriculum",
]
