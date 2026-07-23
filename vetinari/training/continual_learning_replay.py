"""Replay-buffer implementation for Vetinari continual learning.

Replay buffers keep high-quality previous examples available for future
training runs, reducing catastrophic forgetting by mixing old examples into new
task-specific datasets.
"""

from __future__ import annotations

import json
import logging
import random
import threading
from pathlib import Path
from typing import Any

from vetinari.privacy.envelope import require_privacy_envelope
from vetinari.training.continual_learning_persistence import (
    _atomic_write_text,
    _default_replay_buffer_path,
    _move_corrupt_file,
)
from vetinari.utils import privacy_receipt

logger = logging.getLogger(__name__)

ReplayExample = dict[str, Any]


class ReplayBuffer:
    """Maintains diverse high-quality past examples for experience replay.

    Examples from previous tasks are stored on disk as JSONL and sampled into
    new training batches to reduce catastrophic forgetting. The buffer is
    size-capped; when it overflows, stratified sampling preserves diversity
    across ``domain`` and ``task_type`` fields.
    """

    def __init__(
        self,
        max_size: int = 5000,
        buffer_path: Path | None = None,
    ) -> None:
        """Initialise the replay buffer.

        Args:
            max_size: Maximum number of examples to retain. When exceeded,
                stratified sampling is applied to preserve diversity.
            buffer_path: Path for persisting the buffer as JSONL. Defaults to
                the configured Vetinari user directory.
        """
        self.max_size = max_size
        self.buffer_path = Path(buffer_path) if buffer_path is not None else _default_replay_buffer_path()
        self._examples: list[ReplayExample] = []
        self._lock = threading.Lock()

        if self.buffer_path.exists():
            self.load()

    def add(self, examples: list[ReplayExample]) -> int:
        """Add new examples to the buffer.

        If the buffer exceeds ``max_size`` after adding, stratified sampling is
        applied to trim it back while preserving domain diversity.

        Args:
            examples: Example records with ``text`` or ``prompt`` plus
                ``completion`` fields. ``task_type`` or ``domain`` fields
                enable stratified sampling.

        Returns:
            Number of examples accepted before any trimming.
        """
        added = len(examples)
        for example in examples:
            require_replay_example_evidence(example)
        with self._lock:
            self._examples.extend(examples)
            if len(self._examples) > self.max_size:
                self._examples = self._stratified_sample(self.max_size)
        logger.info(
            "ReplayBuffer: added %d examples, buffer size now %d",
            added,
            len(self._examples),
        )
        return added

    def get_replay_batch(self, batch_size: int) -> list[ReplayExample]:
        """Return a random sample from the replay buffer.

        Args:
            batch_size: Number of examples to return. Clamped to buffer size if
                larger than the available examples.

        Returns:
            Randomly sampled example records.
        """
        with self._lock:
            available = len(self._examples)
        if available == 0:
            logger.warning("ReplayBuffer is empty; returning empty batch")
            return []
        k = min(batch_size, available)
        with self._lock:
            return random.sample(self._examples, k)

    def create_mixed_dataset(
        self,
        new_data_path: Path,
        replay_ratio: float = 0.2,
        output_path: Path | None = None,
    ) -> Path:
        """Create a mixed training dataset from new and replay examples.

        The output contains ``1 - replay_ratio`` proportion of new data and
        ``replay_ratio`` proportion of replay buffer examples, shuffled together.

        Args:
            new_data_path: Path to JSONL file with new training examples.
            replay_ratio: Fraction of the mixed dataset to fill from the replay
                buffer. Must be in ``[0.0, 1.0]``.
            output_path: Output JSONL path. Defaults to a sibling file of
                ``new_data_path`` with ``_mixed`` appended to the stem.

        Returns:
            Path to the written mixed dataset JSONL file.

        Raises:
            ValueError: If replay_ratio is outside ``[0.0, 1.0]``.
        """
        if not 0.0 <= replay_ratio <= 1.0:
            raise ValueError(f"replay_ratio must be in [0.0, 1.0], got {replay_ratio}")

        new_examples = self._load_new_examples(new_data_path)
        total_new = len(new_examples)
        if total_new == 0:
            logger.warning("No examples loaded from %s", new_data_path)

        replay_count = self._replay_count(total_new, replay_ratio)
        replay_examples = self.get_replay_batch(replay_count) if replay_count > 0 else []

        mixed = new_examples + replay_examples
        random.shuffle(mixed)

        if output_path is None:
            output_path = new_data_path.with_stem(new_data_path.stem + "_mixed")

        _atomic_write_text(
            Path(output_path),
            "".join(json.dumps(example) + "\n" for example in mixed),
        )

        logger.info(
            "Mixed dataset written: new=%d, replay=%d, total=%d -> %s",
            total_new,
            len(replay_examples),
            len(mixed),
            output_path,
        )
        return output_path

    def save(self) -> None:
        """Persist the replay buffer to disk as JSONL.

        Creates parent directories if they do not exist.
        """
        with self._lock:
            examples_snapshot = list(self._examples)

        _atomic_write_text(
            Path(self.buffer_path),
            "".join(json.dumps(example) + "\n" for example in examples_snapshot),
        )

        logger.info(
            "ReplayBuffer saved: %d examples -> %s",
            len(examples_snapshot),
            self.buffer_path,
        )

    def load(self) -> None:
        """Load the replay buffer from disk.

        Replaces in-memory examples with persisted data. Malformed JSONL marks
        the file damaged, moves it aside, and fails closed.

        Raises:
            OSError: If the buffer file exists but cannot be read.
            ValueError: If any persisted JSONL line is malformed.
        """
        if not self.buffer_path.exists():
            logger.info(
                "ReplayBuffer file not found at %s; starting with empty buffer",
                self.buffer_path,
            )
            return

        examples: list[ReplayExample] = []
        malformed_lines = 0
        with Path(self.buffer_path).open(encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                line = line.strip()
                if line:
                    try:
                        loaded = json.loads(line)
                    except json.JSONDecodeError:
                        logger.warning(
                            "ReplayBuffer file %s contains malformed JSONL at line %d",
                            self.buffer_path,
                            line_number,
                        )
                        malformed_lines += 1
                        continue
                    if isinstance(loaded, dict):
                        try:
                            require_replay_example_evidence(loaded)
                        except ValueError as exc:
                            logger.warning(
                                "ReplayBuffer file %s contains invalid replay evidence at line %d: %s",
                                self.buffer_path,
                                line_number,
                                exc,
                            )
                            malformed_lines += 1
                            continue
                        else:
                            examples.append(loaded)
                    else:
                        logger.warning(
                            "ReplayBuffer file %s contains non-object JSONL at line %d",
                            self.buffer_path,
                            line_number,
                        )
                        malformed_lines += 1

        if malformed_lines:
            corrupt_path = _move_corrupt_file(Path(self.buffer_path))
            raise ValueError(
                f"ReplayBuffer file {self.buffer_path} contains {malformed_lines} malformed JSONL line(s); "
                f"moved aside to {corrupt_path}"
            )

        with self._lock:
            self._examples = examples

        logger.info(
            "ReplayBuffer loaded: %d examples from %s",
            len(examples),
            self.buffer_path,
        )

    def __len__(self) -> int:
        """Return the current number of examples in the buffer.

        Returns:
            Count of stored examples.
        """
        with self._lock:
            return len(self._examples)

    def _stratified_sample(self, target_size: int) -> list[ReplayExample]:
        """Sample examples while preserving proportional task diversity."""
        if target_size >= len(self._examples):
            return list(self._examples)

        groups: dict[str, list[ReplayExample]] = {}
        for example in self._examples:
            key = example.get("domain") or example.get("task_type") or "_unknown"
            groups.setdefault(key, []).append(example)

        total = len(self._examples)
        sampled: list[ReplayExample] = []

        for group_examples in groups.values():
            proportion = len(group_examples) / total
            group_count = max(1, round(proportion * target_size))
            group_count = min(group_count, len(group_examples))
            sampled.extend(random.sample(group_examples, group_count))

        if len(sampled) > target_size:
            sampled = random.sample(sampled, target_size)
        elif len(sampled) < target_size:
            remaining = [example for example in self._examples if example not in sampled]
            gap = target_size - len(sampled)
            sampled.extend(random.sample(remaining, min(gap, len(remaining))))

        return sampled

    @staticmethod
    def _load_new_examples(new_data_path: Path) -> list[ReplayExample]:
        """Load valid JSONL training examples from a new-data file."""
        new_examples: list[ReplayExample] = []
        with Path(new_data_path).open(encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if line:
                    try:
                        new_examples.append(json.loads(line))
                        require_replay_example_evidence(new_examples[-1])
                    except json.JSONDecodeError:
                        logger.warning("Skipping malformed line in new data")
        return new_examples

    @staticmethod
    def _replay_count(total_new: int, replay_ratio: float) -> int:
        """Compute replay count needed to reach the requested mixed ratio."""
        if replay_ratio > 0.0 and total_new > 0:
            return int(total_new * replay_ratio / max(1.0 - replay_ratio, 1e-9))
        return 0


def require_replay_example_evidence(example: ReplayExample) -> None:
    """Require privacy and provenance evidence before replay persistence.

    Raises:
        ValueError: If the example lacks a privacy receipt or provenance marker.
    """
    if not isinstance(example, dict):
        raise ValueError("replay example must be a dict")
    if "_privacy_envelope" in example:
        require_privacy_envelope(example)
        return
    metadata = example.get("metadata")
    receipt = metadata.get("privacy_receipt") if isinstance(metadata, dict) else None
    if not isinstance(receipt, dict):
        raise ValueError("replay example lacks privacy receipt")
    privacy_receipt(
        privacy_class=str(receipt.get("privacy_class", "")),
        subject_id=receipt.get("subject_id"),
        retention_days=int(receipt.get("retention_days", 0)),
        source=str(receipt.get("source", "")),
        erasure_token=receipt.get("erasure_token"),
        redaction_applied=bool(receipt.get("redaction_applied", False)),
    )
    if not (metadata.get("dataset_revision") or metadata.get("source_dataset") or metadata.get("provenance")):
        raise ValueError("replay example lacks provenance source or dataset revision")


__all__ = ["ReplayBuffer", "ReplayExample", "require_replay_example_evidence"]
