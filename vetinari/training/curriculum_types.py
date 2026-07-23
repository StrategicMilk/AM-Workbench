"""Shared data types and constants for training curriculum selection."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from enum import Enum
from importlib.util import find_spec
from typing import Any

logger = logging.getLogger("vetinari.training.curriculum")


def _require_module(module_name: str) -> None:
    """Raise ModuleNotFoundError when a lazy curriculum dependency is absent."""
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


# Minimum episodes before self-play reasoning is worth scheduling
MIN_REASONING_EPISODES = 50

# Minimum execution traces before RLEF training is worth scheduling
MIN_RLEF_TRACES = 30

# Minimum defect occurrences before it becomes a training priority
DEFECT_THRESHOLD = 5

# Score below which a skill is considered weak and needs targeted training
WEAK_SKILL_THRESHOLD = 0.7

# Score threshold for distillation; only distill from high-quality cloud outputs
DISTILLATION_QUALITY_THRESHOLD = 0.85

# Days after which benchmarks are considered stale
BENCHMARK_STALENESS_DAYS = 7

# Default calibration activity estimated values
DEFAULT_DURATION_MINUTES = 30
DEFAULT_VRAM_GB = 4.0


class TrainingActivityType(Enum):
    """The category of training work to be performed."""

    FINE_TUNE_WEAK_SKILL = "fine_tune_weak_skill"
    SELF_PLAY_REASONING = "self_play_reasoning"
    EXTERNAL_DATA_TRAINING = "external_data_training"
    PROMPT_EVOLUTION = "prompt_evolution"
    DISTILLATION = "distillation"
    BENCHMARK_PRACTICE = "benchmark_practice"
    RLEF_CODE_EXECUTION = "rlef_code_execution"


class CurriculumPhase(Enum):
    """Broad phase of the training curriculum lifecycle."""

    CALIBRATION = "calibration"
    TARGETED_SKILL_BUILDING = "targeted_skill_building"
    CONTINUOUS_IMPROVEMENT = "continuous_improvement"


@dataclass
class TrainingActivity:
    """A concrete unit of training work with full scheduling metadata.

    Attributes:
        type: The category of training to perform.
        description: Human-readable description of the activity.
        hypothesis: What improvement is expected and why.
        metric: Name of the metric this activity is expected to move.
        baseline: Current measured value of the metric.
        target: Desired value after training completes.
        rollback_plan: How to revert if training degrades performance.
        estimated_duration_minutes: Wall-clock time estimate.
        estimated_vram_gb: GPU memory required during training.
        priority: Urgency score 0-1, higher is more urgent.
        metadata: Arbitrary extra data for pipeline consumers.
    """

    type: TrainingActivityType
    description: str
    hypothesis: str
    metric: str
    baseline: float
    target: float
    rollback_plan: str
    estimated_duration_minutes: int
    estimated_vram_gb: float
    priority: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"TrainingActivity(type={self.type.value!r}, metric={self.metric!r}, "
            f"baseline={self.baseline!r}, target={self.target!r}, priority={self.priority!r})"
        )
