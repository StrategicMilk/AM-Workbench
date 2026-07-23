"""Context distillation dataset builder for training.

This module turns high-quality episode memories into SFT JSONL data. It is the
dataset-building step used when the training system distills long-context work
records into smaller examples a local model can learn from.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from vetinari.privacy.envelope import PrivacyClass, privacy_receipt
from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)


@dataclass
class DistillationDatasetInfo:
    """Metadata about a context distillation dataset that was built.

    Attributes:
        output_path: Absolute path to the written JSONL file.
        num_examples: Number of training examples written.
        avg_quality: Mean quality_score across all included episodes.
        task_types: Distinct task_type values present in the dataset.
        created_at: ISO-8601 timestamp of dataset creation.
    """

    output_path: str
    num_examples: int
    avg_quality: float
    task_types: list[str]
    created_at: str

    def __repr__(self) -> str:
        return (
            f"DistillationDatasetInfo(num_examples={self.num_examples!r}, "
            f"avg_quality={self.avg_quality!r}, task_types={self.task_types!r})"
        )


class ContextDistillationDatasetBuilder:
    """Builds SFT training datasets from successful episodes for context distillation.

    Extracts instruction, input, and output triples from EpisodeMemory where
    ``quality_score`` is at least the configured threshold and success is true.
    The resulting JSONL is suitable for QLoRA/SFT fine-tuning.

    Args:
        min_quality: Minimum episode quality_score to include.
        min_pairs: Minimum number of episodes required to write a dataset. If
            fewer are found, ``build_dataset`` returns ``None``.
    """

    def __init__(
        self,
        min_quality: float = 0.8,
        min_pairs: int = 100,
    ) -> None:
        self._min_quality = min_quality
        self._min_pairs = min_pairs

    def build_dataset(
        self,
        output_path: str | Path,
        task_type: str | None = None,
    ) -> DistillationDatasetInfo | None:
        """Build a JSONL SFT dataset from high-quality episodes.

        Queries EpisodeMemory for successful episodes above the quality
        threshold. Returns ``None`` if fewer than ``min_pairs`` qualify.

        Args:
            output_path: File path for the output JSONL dataset.
            task_type: Filter episodes to a specific task type. Pass ``None``
                to include all task types.

        Returns:
            DistillationDatasetInfo on success, or ``None`` if insufficient data.
        """
        episodes = self._query_episodes(task_type)

        if len(episodes) < self._min_pairs:
            logger.warning(
                "[ContextDistillation] Only %d episodes meet quality>=%.2f criteria (need %d) - skipping dataset build",
                len(episodes),
                self._min_quality,
                self._min_pairs,
            )
            return None

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        records = [self._format_for_sft(ep) for ep in episodes]
        with output_path.open("w", encoding="utf-8") as fh:
            fh.writelines(json.dumps(record) + "\n" for record in records)

        avg_quality = sum(ep.quality_score for ep in episodes) / len(episodes)
        task_types = sorted({ep.task_type for ep in episodes})
        info = DistillationDatasetInfo(
            output_path=str(output_path),
            num_examples=len(records),
            avg_quality=round(avg_quality, 4),
            task_types=task_types,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        logger.info(
            "[ContextDistillation] Wrote %d examples to %s (avg_quality=%.3f)",
            info.num_examples,
            output_path,
            info.avg_quality,
        )
        return info

    def _query_episodes(self, task_type: str | None) -> list[Any]:
        """Query EpisodeMemory for successful high-quality episodes.

        Args:
            task_type: Optional filter for task type.

        Returns:
            Episode-like objects meeting the quality criteria, or an empty list
            if EpisodeMemory is unavailable.
        """
        try:
            from vetinari.learning.episode_memory import EpisodeMemory

            memory = EpisodeMemory.get_instance()
            episodes = memory.recall(
                query="",
                k=10000,
                min_score=self._min_quality,
                task_type=task_type,
                successful_only=True,
            )
        except (ImportError, RuntimeError, OSError, AttributeError, TypeError, ValueError) as exc:
            logger.warning(
                "[ContextDistillation] EpisodeMemory query failed - dataset build will return None: %s",
                exc,
            )
            episodes = []

        return cast(list[Any], episodes)

    @staticmethod
    def _format_for_sft(episode: Any) -> dict[str, Any]:
        """Convert an Episode to Alpaca-style SFT training format.

        Args:
            episode: An Episode-like object with task_summary, output_summary,
                quality_score, agent_type, and model_id fields.

        Returns:
            Dictionary with instruction, input, output, and metadata keys.
        """
        instruction = redact_text(str(episode.task_summary))
        output = redact_text(str(episode.output_summary))
        return {
            "instruction": instruction,
            "input": "",
            "output": output,
            "metadata": {
                "quality": episode.quality_score,
                "agent_type": episode.agent_type,
                "model_id": episode.model_id,
                "privacy_receipt": privacy_receipt(
                    privacy_class=PrivacyClass.SUBJECT_DATA.value,
                    subject_id=str(getattr(episode, "task_type", "distillation") or "distillation"),
                    source="training.context_distillation",
                    redaction_applied=True,
                ),
            },
        }


__all__ = ["ContextDistillationDatasetBuilder", "DistillationDatasetInfo"]
