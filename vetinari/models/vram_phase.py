"""Execution-phase policy for VRAM budgeting.

The local model manager uses this policy to recommend which resident models fit
the current Foreman, Worker, or Inspector stage without embedding the phase table
inside the manager class.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.models.vram_types import ExecutionPhase
from vetinari.ux import display_label_or_humanize

logger = logging.getLogger(__name__)

# Log format intentionally uses %-style arguments supplied by logger.info.
_PHASE_TRANSITION_LOG = "VRAM phase change: %s -> %s"

_PHASE_RECOMMENDATIONS: dict[str, dict[str, Any]] = {
    ExecutionPhase.PLANNING: {
        "load": ["foreman-32b", "embeddings", "draft-1.5b"],
        "unload": ["worker-moe", "sub-foreman-9b"],
        "estimated_vram_gb": 20.5,
    },
    ExecutionPhase.EXECUTION: {
        "load": ["worker-moe", "sub-foreman-9b", "embeddings", "draft-1.5b"],
        "unload": ["foreman-32b"],
        "estimated_vram_gb": 20.5,
    },
    ExecutionPhase.REVIEW: {
        "load": ["foreman-32b", "embeddings"],
        "unload": ["worker-moe", "sub-foreman-9b"],
        "estimated_vram_gb": 19.5,
    },
}


class _VRAMPhaseMixin:
    """Phase-oriented behavior mixed into ``VRAMManager``."""

    if TYPE_CHECKING:
        _current_phase: Any
        _gpu_total_gb: Any

    def set_phase(self, phase: str) -> None:
        """Set the current execution phase for VRAM budgeting decisions.

        Args:
            phase: One of ``ExecutionPhase.PLANNING``,
                ``ExecutionPhase.EXECUTION``, or ``ExecutionPhase.REVIEW``.
        """
        old = self._current_phase
        self._current_phase = phase
        if old != phase:
            logger.info(_PHASE_TRANSITION_LOG, old, phase)

    def get_phase_recommendation(self) -> dict[str, Any]:
        """Return recommended model configuration for the current phase.

        Returns:
            Mapping with phase name, recommended models to load or unload, and
            the estimated GPU memory budget.
        """
        recommendation = _PHASE_RECOMMENDATIONS.get(self._current_phase, {})
        return {
            "phase": self._current_phase,
            "phase_label": display_label_or_humanize(self._current_phase),
            "gpu_total_gb": self._gpu_total_gb,
            **recommendation,
        }
