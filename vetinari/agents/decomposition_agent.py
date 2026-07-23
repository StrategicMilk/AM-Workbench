"""Decomposition Agent Interface.

==============================
Provides the decomposition_agent singleton and RECURSION_KNOBS used by
the Decomposition Lab API endpoints.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from vetinari.constants import (
    DECOMP_BREADTH_FIRST_WEIGHT,
    DECOMP_DEFAULT_MAX_DEPTH,
    DECOMP_MAX_MAX_DEPTH,
    DECOMP_MAX_SUBTASKS_PER_LEVEL,
    DECOMP_MIN_MAX_DEPTH,
    DECOMP_MIN_SUBTASK_WORDS,
    DECOMP_QUALITY_THRESHOLD,
    DECOMP_SEED_MIX,
    DECOMP_SEED_RATE,
)

logger = logging.getLogger(__name__)


# Recursion control knobs
RECURSION_KNOBS = {
    "breadth_first_weight": DECOMP_BREADTH_FIRST_WEIGHT,  # Prefer breadth over depth
    "min_subtask_words": DECOMP_MIN_SUBTASK_WORDS,  # Min words in subtask description
    "max_subtasks_per_level": DECOMP_MAX_SUBTASKS_PER_LEVEL,  # Max subtasks per decomposition level
    "quality_threshold": DECOMP_QUALITY_THRESHOLD,  # Minimum quality score to accept a subtask
}

SEED_RATE = DECOMP_SEED_RATE
SEED_MIX = DECOMP_SEED_MIX
DEFAULT_MAX_DEPTH = DECOMP_DEFAULT_MAX_DEPTH
MIN_MAX_DEPTH = DECOMP_MIN_MAX_DEPTH
MAX_MAX_DEPTH = DECOMP_MAX_MAX_DEPTH


def _prompt_ref(prompt: str) -> str:
    digest = hashlib.sha256(prompt.encode("utf-8", errors="replace")).hexdigest()
    return f"[REDACTED_PROMPT sha256={digest}]"


class DecompositionAgent:
    """Agent responsible for recursively decomposing plans via the ForemanAgent.

    Provides the `decompose_from_prompt` interface used by the Decomp Lab.
    """

    def decompose_from_prompt(self, plan: Any, prompt: str) -> dict[str, Any]:
        """Decompose a prompt in the context of an existing plan.

        Returns a dict with tasks, depth info, and metadata.

        Args:
            plan: The plan.
            prompt: The prompt.

        Returns:
            Dictionary with keys ``status`` (``"ok"`` or ``"error"``),
            ``plan_id``, ``prompt_ref``, ``subtasks`` (list),
            ``subtask_count``, and ``knobs``.  On failure, contains only ``status`` and
            ``error``.
        """
        try:
            from vetinari.planning.decomposition import decomposition_engine

            subtasks = decomposition_engine.decompose_task(
                task_prompt=prompt,
                parent_task_id=getattr(plan, "plan_id", "root"),
                depth=0,
                max_depth=DEFAULT_MAX_DEPTH,
                plan_id=getattr(plan, "plan_id", "default"),
            )
            return {
                "status": "ok",
                "plan_id": getattr(plan, "plan_id", "unknown"),
                "prompt_ref": _prompt_ref(prompt),
                "subtasks": subtasks,
                "subtask_count": len(subtasks),
                "knobs": RECURSION_KNOBS,
            }
        except Exception:
            logger.exception("DecompositionAgent.decompose_from_prompt failed")
            return {"status": "error", "error": "Decomposition failed — check server logs for details"}


# Module-level singleton
decomposition_agent = DecompositionAgent()
