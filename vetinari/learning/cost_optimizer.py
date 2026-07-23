"""Cost Optimizer - Vetinari Self-Improvement Subsystem.

Analyzes cost data to automatically route tasks to the cheapest
adequate model, respecting quality thresholds.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CostEfficiency:
    """Quality-per-dollar metric for a model+task_type pair."""

    model_id: str
    task_type: str
    avg_quality: float
    avg_cost_usd: float
    quality_per_dollar: float
    total_uses: int
    data_adequate: bool = True
    missing_fields: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return f"CostEfficiency(model_id={self.model_id!r}, task_type={self.task_type!r}, quality_per_dollar={self.quality_per_dollar!r})"


class CostOptimizer:
    """Selects the cheapest model that meets a quality threshold.

    Integrates with the CostTracker analytics module to make
    evidence-based routing decisions.
    """

    DEFAULT_MIN_QUALITY = 0.65  # Minimum acceptable quality
    LOCAL_COST = 0.0  # Local models are free

    def __init__(self):
        self._cost_tracker = None

    def _get_cost_tracker(self):
        if self._cost_tracker is None:
            try:
                from vetinari.analytics.cost import get_cost_tracker

                self._cost_tracker = get_cost_tracker()
            except Exception:
                logger.warning("Failed to initialize cost tracker", exc_info=True)
        return self._cost_tracker

    def select_cheapest_adequate(
        self,
        task_type: str,
        candidate_models: list[str],
        min_quality: float | None = None,
        max_cost_usd: float | None = None,
    ) -> str:
        """Select the cheapest model that meets the quality threshold.

        Args:
            task_type: The task type.
            candidate_models: List of model IDs to consider.
            min_quality: Minimum acceptable quality (default 0.65).
            max_cost_usd: Maximum cost per call in USD.

        Returns:
            The selected model ID.
        """
        min_quality = self.DEFAULT_MIN_QUALITY if min_quality is None else min_quality
        efficiencies = self._get_efficiencies(task_type, candidate_models)

        for efficiency in (e for e in efficiencies if not e.data_adequate):
            logger.warning(
                "Cost optimizer ignoring %s for %s because telemetry is incomplete: %s",
                efficiency.model_id,
                task_type,
                ", ".join(efficiency.missing_fields),
            )

        # Filter by telemetry adequacy and quality threshold.
        adequate = [e for e in efficiencies if e.data_adequate and e.avg_quality >= min_quality]

        # Further filter by max cost
        if max_cost_usd is not None:
            adequate = [e for e in adequate if e.avg_cost_usd <= max_cost_usd]

        if not adequate:
            # Missing telemetry is not adequate routing evidence. Preserve the
            # caller's first model as the conservative default instead of making
            # a cost-optimized switch from priors.
            if efficiencies and all(not e.data_adequate for e in efficiencies):
                return candidate_models[0] if candidate_models else "default"
            adequate_efficiencies = [e for e in efficiencies if e.data_adequate]
            if adequate_efficiencies:
                return max(adequate_efficiencies, key=lambda e: e.avg_quality).model_id
            return candidate_models[0] if candidate_models else "default"

        # Among adequate models, pick the cheapest
        cheapest = min(adequate, key=lambda e: e.avg_cost_usd)
        logger.info(
            f"[CostOptimizer] Selected {cheapest.model_id} for {task_type}: "
            f"quality={cheapest.avg_quality:.2f} cost=${cheapest.avg_cost_usd:.4f}",
        )
        return cheapest.model_id

    def _get_efficiencies(self, task_type: str, models: list[str]) -> list[CostEfficiency]:
        """Compute quality-per-dollar for each model."""
        tracker = self._get_cost_tracker()
        efficiencies: list[CostEfficiency] = []

        for model_id in models:
            cost = 0.0
            quality = 0.7
            cost_seen = False
            quality_seen = False

            # Get cost from analytics — CostReport.by_model maps "provider:model" -> USD
            if tracker:
                try:
                    report = tracker.get_report()
                    for key, model_cost in report.by_model.items():
                        key_model_id = str(key).rsplit(":", 1)[-1]
                        if key_model_id == model_id:
                            cost = float(model_cost)
                            cost_seen = True
                            break
                except Exception:
                    logger.warning(
                        "Failed to get cost data for model %s",
                        model_id,
                        exc_info=True,
                    )
            else:
                logger.warning(
                    "CostTracker unavailable — cost optimization running without cost data for %s",
                    model_id,
                )

            # Get quality from Thompson Sampling selector
            try:
                from vetinari.learning.model_selector import get_thompson_selector

                arm_state = get_thompson_selector().get_arm_state(model_id, task_type)
                if "mean" in arm_state:
                    quality = float(arm_state["mean"])
                    quality_seen = True
            except Exception:
                logger.warning("Failed to get Thompson Sampling quality for model %s", model_id, exc_info=True)

            missing_fields = tuple(
                field for field, present in (("cost", cost_seen), ("quality", quality_seen)) if not present
            )
            qpd = quality / max(cost, 0.001)  # Avoid division by zero
            efficiencies.append(
                CostEfficiency(
                    model_id=model_id,
                    task_type=task_type,
                    avg_quality=quality,
                    avg_cost_usd=cost,
                    quality_per_dollar=qpd,
                    total_uses=0,
                    data_adequate=not missing_fields,
                    missing_fields=missing_fields,
                ),
            )

        return efficiencies

    def get_budget_forecast(
        self,
        planned_tasks: int,
        task_types: list[str],
        models: list[str],
    ) -> dict[str, Any]:
        """Estimate total cost for a planned set of tasks.

        Returns:
            Dict with estimated_cost_usd, breakdown_by_type, warnings.

        Args:
            planned_tasks: The planned tasks.
            task_types: The task types.
            models: The models.
        """
        total_cost = 0.0
        breakdown: dict[str, float] = {}
        self._get_cost_tracker()

        for i, task_type in enumerate(task_types):
            if i >= planned_tasks:
                break
            efficiencies = self._get_efficiencies(task_type, models)
            if efficiencies:
                avg = sum(e.avg_cost_usd for e in efficiencies) / len(efficiencies)
            else:
                avg = 0.0
            breakdown[task_type] = avg
            total_cost += avg

        warnings: list[str] = []
        if total_cost > 1.0:
            warnings.append(f"Estimated cost ${total_cost:.2f} exceeds $1.00 threshold")

        return {
            "estimated_cost_usd": round(total_cost, 4),
            "breakdown_by_type": breakdown,
            "warnings": warnings,
        }


_cost_optimizer: CostOptimizer | None = None
_cost_optimizer_lock = threading.Lock()


def get_cost_optimizer() -> CostOptimizer:
    """Get cost optimizer.

    Returns:
        The CostOptimizer result.
    """
    global _cost_optimizer
    if _cost_optimizer is None:
        with _cost_optimizer_lock:
            if _cost_optimizer is None:
                _cost_optimizer = CostOptimizer()
    return _cost_optimizer
