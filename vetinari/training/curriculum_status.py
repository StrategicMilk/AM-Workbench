"""Status and scheduling API for the training curriculum."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .curriculum_types import CurriculumPhase, TrainingActivity, _require_module, logger


class CurriculumStatusMixin:
    """Expose curriculum status and next-activity selection."""

    if TYPE_CHECKING:
        _candidate_benchmark_practice: Any
        _candidate_defect_pattern: Any
        _candidate_distillation: Any
        _candidate_external_data: Any
        _candidate_prompt_evolution: Any
        _candidate_rlef: Any
        _candidate_self_play: Any
        _candidate_weak_skill: Any
        _candidates: Any
        _default_activity: Any

    def next_activity(self) -> TrainingActivity:
        """Determine the highest-priority training activity right now.

        Builds a candidate list from all available signal sources, sorts by
        priority, and returns the top candidate. Falls back to a calibration
        benchmark if no signals are actionable.

        Returns:
            The TrainingActivity with the highest priority score.
        """
        candidates: list[TrainingActivity] = []

        for builder in (
            self._candidate_weak_skill,
            self._candidate_defect_pattern,
            self._candidate_self_play,
            self._candidate_external_data,
            self._candidate_prompt_evolution,
            self._candidate_benchmark_practice,
            self._candidate_distillation,
            self._candidate_rlef,
        ):
            try:
                activity = builder()
                if activity is not None:
                    candidates.append(activity)
            except Exception:
                logger.warning("Candidate builder %s raised unexpectedly; skipping", builder.__name__, exc_info=True)

        self._candidates = candidates

        if not candidates:
            logger.info("No training candidates found; returning default calibration activity")
            return self._default_activity()

        candidates.sort(key=lambda a: a.priority, reverse=True)
        chosen = candidates[0]
        logger.info(
            "next_activity selected: type=%s priority=%.2f description=%s",
            chosen.type.value,
            chosen.priority,
            chosen.description,
        )
        return chosen

    def get_phase(self) -> CurriculumPhase:
        """Return the current curriculum phase based on training history.

        Phases:
            - CALIBRATION: fewer than 50 execution records exist
            - TARGETED_SKILL_BUILDING: records >= 50 and weakest skill < 0.7
            - CONTINUOUS_IMPROVEMENT: records >= 50 and all skills at threshold

        Returns:
            The current CurriculumPhase enum value.
        """
        record_count = 0
        try:
            _require_module("vetinari.learning.training_data")
            from vetinari.learning.training_data import get_training_collector

            collector = get_training_collector()
            record_count = collector.count_records()
        except ModuleNotFoundError:
            logger.debug("training_data collector not available; defaulting to CALIBRATION phase")
            return CurriculumPhase.CALIBRATION
        except Exception:
            logger.warning("Failed to count training records; defaulting to CALIBRATION", exc_info=True)
            return CurriculumPhase.CALIBRATION

        if record_count < 50:
            return CurriculumPhase.CALIBRATION

        weak_skill = self._candidate_weak_skill()
        if weak_skill is not None:
            return CurriculumPhase.TARGETED_SKILL_BUILDING

        return CurriculumPhase.CONTINUOUS_IMPROVEMENT

    def get_status(self) -> dict[str, Any]:
        """Return a snapshot of the current curriculum state.

        Returns:
            Dictionary with keys:
                - ``phase``: string name of the current CurriculumPhase
                - ``candidate_count``: number of actionable candidates last computed
                - ``next_activity_description``: description of the top candidate
        """
        phase = self.get_phase()
        next_act = self.next_activity()
        return {
            "phase": phase.value,
            "candidate_count": len(self._candidates),
            "next_activity_description": next_act.description,
        }
