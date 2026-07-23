"""Vetinari Training Data Collector — public re-export surface.

Records every agent execution to a JSONL file, enabling:

1. **SFT (Supervised Fine-Tuning)** — export high-quality completions
   (score >= threshold) as training pairs for local model fine-tuning.
2. **DPO (Direct Preference Optimization)** — export preference pairs
   (best vs. worst response for the same task type) for alignment training.
3. **Prompt dataset** — export prompt variant performance for DSPy optimization.

Implementation is split across two submodules:
- ``training_record``: TrainingRecord frozen dataclass
- ``training_collector``: TrainingDataCollector class + get_training_collector singleton

All public names are re-exported here so existing callers do not need to change.

Usage::

    from vetinari.learning.training_data import get_training_collector

    collector = get_training_collector()
    collector.record(
        task=task_description,
        prompt=system_prompt + user_prompt,
        response=llm_output,
        score=0.85,
        model_id="qwen3-vl-32b-gemini-heretic-uncensored-thinking",
        task_type="coding",
        latency_ms=4200,
        tokens_used=1024,
    )

    # Export for fine-tuning
    sft_data = collector.export_sft_dataset(min_score=0.8)
    dpo_data = collector.export_dpo_dataset()
"""

from __future__ import annotations

import logging
from typing import Any

from .training_collector import TrainingDataCollector, get_training_collector
from .training_record import TrainingRecord

logger = logging.getLogger(__name__)


__all__ = [
    "TrainingDataCollector",
    "TrainingRecord",
    "check_training_data_ready",
    "get_training_collector",
]

# Minimum records required before training data is considered actionable.
# Below this threshold SFT/DPO exports are unlikely to generalize.
_MIN_RECORDS_FOR_TRAINING = 50


def check_training_data_ready(min_records: int = _MIN_RECORDS_FOR_TRAINING) -> dict[str, Any]:
    """Check whether enough training data has accumulated for fine-tuning.

    Queries the collector's stats and returns a readiness summary so callers
    can gate expensive training pipelines on data availability.

    Args:
        min_records: Minimum total records required to be considered ready.

    Returns:
        Dict with ``ready`` (bool), ``total``, ``sft_eligible``, and
        ``shortfall`` (how many more records are needed, 0 if ready).
    """
    collector = get_training_collector()
    stats = collector.get_stats()
    total = stats.get("total", 0)
    sft_eligible = stats.get("sft_eligible", 0)
    is_ready = total >= min_records
    if not is_ready:
        logger.info(
            "[TrainingData] Not ready: %d/%d records collected (need %d more)",
            total,
            min_records,
            min_records - total,
        )
    return {
        "ready": is_ready,
        "total": total,
        "sft_eligible": sft_eligible,
        "shortfall": max(0, min_records - total),
    }
