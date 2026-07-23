"""Knowledge validator — compare predictions vs actual performance, auto-correct on divergence.

Periodically validates that knowledge files (YAML) match actual observed model
performance data. When divergence exceeds 0.15, triggers auto-correction:
updates the knowledge cache and logs corrections for review.

Pipeline role: Knowledge Maintenance — keeps knowledge files honest.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.learning.atomic_writers import write_json_atomic
from vetinari.security.fail_closed import assert_closed_schema, sanitize_untrusted_text

logger = logging.getLogger(__name__)


# -- Configuration --
_DIVERGENCE_THRESHOLD = 0.15  # Trigger auto-correction when abs(predicted - actual) exceeds this


# -- Data structures --


@dataclass(frozen=True, slots=True)
class CorrectionRecord:
    """An immutable record of a single auto-correction applied to a knowledge entry.

    Attributes:
        model_id: Identifier of the model whose entry was corrected.
        metric: Name of the metric that diverged (e.g., 'accuracy', 'latency').
        old_value: The previously predicted/stored value.
        new_value: The observed actual value that replaced it.
        divergence: Signed divergence (new_value - old_value).
        timestamp: ISO-8601 UTC timestamp when the correction was applied.
    """

    model_id: str
    metric: str
    old_value: float
    new_value: float
    divergence: float
    timestamp: str  # ISO-8601 UTC

    def __repr__(self) -> str:
        return f"CorrectionRecord(model_id={self.model_id!r}, metric={self.metric!r}, divergence={self.divergence:.3f})"


@dataclass(slots=True)
class ValidationReport:
    """Summary of a single validation pass across all model entries.

    Attributes:
        corrections: List of CorrectionRecords applied during this pass.
        checked_models: Number of model entries examined.
        timestamp: ISO-8601 UTC timestamp when this report was generated.
    """

    corrections: list[CorrectionRecord] = field(default_factory=list)
    checked_models: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return f"ValidationReport(corrections={len(self.corrections)}, checked_models={self.checked_models})"


# -- Validator --


class KnowledgeValidator:
    """Compares knowledge-file predictions against actual model performance data.

    When divergence on any metric exceeds the threshold (0.15), the validator
    mutates the ``knowledge_data`` dict in place for divergent metrics and
    persists a CorrectionRecord to the corrections JSON file for offline review.

    The corrections file is append-safe: it reads the existing list, appends
    new records, and writes the full list back atomically via a rename-safe
    write pattern (read-modify-write on the same path).
    """

    def __init__(self, corrections_path: Path | None = None) -> None:
        """Set up the validator with an optional custom corrections file path.

        Args:
            corrections_path: Path to the JSON file that accumulates correction
                records. Defaults to ``.vetinari/knowledge_corrections.json``
                relative to the working directory.
        """
        self._corrections_path: Path = (
            corrections_path if corrections_path is not None else Path(".vetinari") / "knowledge_corrections.json"
        )

    def validate(
        self,
        knowledge_data: dict[str, Any],
        actual_data: dict[str, Any],
    ) -> ValidationReport:
        """Compare knowledge predictions against actual performance data.

        Iterates over every model entry present in both ``knowledge_data`` and
        ``actual_data``. For each shared metric, checks whether
        ``abs(predicted - actual) > 0.15``. When it does, calls
        ``_auto_correct()`` and records the result in the report.

        Both dicts are expected to be keyed by model_id at the top level, with
        per-model dicts of ``{metric: value}`` underneath. Entries or metrics
        absent from either side are silently skipped.

        Args:
            knowledge_data: Predicted performance data from knowledge YAML,
                structured as ``{model_id: {metric: predicted_value, ...}, ...}``.
            actual_data: Observed performance data from runtime,
                structured as ``{model_id: {metric: actual_value, ...}, ...}``.

        Returns:
            A ValidationReport with all corrections applied during this pass
            and a count of models that were checked.
        """
        report = ValidationReport(timestamp=datetime.now(timezone.utc).isoformat())

        assert_closed_schema(knowledge_data, allowed_keys=knowledge_data.keys())
        assert_closed_schema(actual_data, allowed_keys=actual_data.keys())
        # Only examine models present in both datasets
        shared_models = set(knowledge_data.keys()) & set(actual_data.keys())

        for model_id in sorted(shared_models):
            safe_model_id = sanitize_untrusted_text(model_id, max_length=200)
            predicted_metrics: dict[str, Any] = knowledge_data[model_id]
            actual_metrics: dict[str, Any] = actual_data[model_id]

            if not isinstance(predicted_metrics, dict) or not isinstance(actual_metrics, dict):
                logger.warning(
                    "Skipping model %r — expected dict entries in both datasets, got %s / %s",
                    model_id,
                    type(predicted_metrics).__name__,
                    type(actual_metrics).__name__,
                )
                continue

            report.checked_models += 1
            shared_metrics = set(predicted_metrics.keys()) & set(actual_metrics.keys())

            for metric in sorted(shared_metrics):
                safe_metric = sanitize_untrusted_text(metric, max_length=120)
                try:
                    predicted = float(predicted_metrics[metric])
                    actual = float(actual_metrics[metric])
                except (TypeError, ValueError):
                    logger.warning(
                        "Skipping metric %r for model %r — values are not numeric (%r vs %r)",
                        metric,
                        model_id,
                        predicted_metrics[metric],
                        actual_metrics[metric],
                    )
                    continue

                divergence = actual - predicted
                if abs(divergence) > _DIVERGENCE_THRESHOLD:
                    record = self._auto_correct(
                        model_id=safe_model_id,
                        metric=safe_metric,
                        old_value=predicted,
                        new_value=actual,
                    )
                    report.corrections.append(record)
                    predicted_metrics[metric] = actual

        return report

    def _auto_correct(
        self,
        model_id: str,
        metric: str,
        old_value: float,
        new_value: float,
    ) -> CorrectionRecord:
        """Persist a single auto-correction and log the divergence.

        Appends the new CorrectionRecord to the corrections JSON file (creating
        the file and any parent directories if needed). Logs a WARNING so the
        correction is visible in operational logs without requiring a manual
        file inspection.

        Args:
            model_id: Model identifier whose metric is being corrected.
            metric: Name of the metric that diverged.
            old_value: Previously predicted value from the knowledge file.
            new_value: Observed actual value from runtime data.

        Returns:
            The CorrectionRecord that was created and persisted.
        """
        model_id = sanitize_untrusted_text(model_id, max_length=200)
        metric = sanitize_untrusted_text(metric, max_length=120)
        divergence = new_value - old_value
        record = CorrectionRecord(
            model_id=model_id,
            metric=metric,
            old_value=old_value,
            new_value=new_value,
            divergence=divergence,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        logger.warning(
            "Knowledge divergence for model %r metric %r:"
            " predicted=%.4f actual=%.4f divergence=%.4f"
            " — auto-correction recorded in %s",
            model_id,
            metric,
            old_value,
            new_value,
            divergence,
            self._corrections_path,
        )

        self._append_correction(record)
        return record

    def _append_correction(self, record: CorrectionRecord) -> None:
        """Read the existing corrections file, append the record, and write back.

        Creates parent directories and the file itself if they do not yet exist.
        Logs a warning and discards the record rather than raising if the file
        cannot be read or written.

        Args:
            record: The CorrectionRecord to persist.
        """
        existing: list[dict[str, Any]] = []

        if self._corrections_path.exists():
            try:
                raw = self._corrections_path.read_text(encoding="utf-8")
                existing = json.loads(raw)
                if not isinstance(existing, list):
                    logger.warning(
                        "Corrections file %s has unexpected format — resetting to empty list",
                        self._corrections_path,
                    )
                    existing = []
            except (OSError, json.JSONDecodeError):
                logger.exception(
                    "Could not read corrections file %s — starting fresh list for this write",
                    self._corrections_path,
                )
                existing = []

        existing.append({
            "model_id": record.model_id,
            "metric": record.metric,
            "old_value": record.old_value,
            "new_value": record.new_value,
            "divergence": record.divergence,
            "timestamp": record.timestamp,
        })

        try:
            write_json_atomic(self._corrections_path, existing)
        except OSError:
            logger.exception(
                "Could not write corrections to %s — correction for model %r metric %r discarded",
                self._corrections_path,
                record.model_id,
                record.metric,
            )

    def load_corrections(self) -> list[CorrectionRecord]:
        """Read and deserialise all past corrections from the corrections file.

        Returns an empty list (rather than raising) when the file does not exist
        or cannot be parsed, so callers can safely call this at any time.

        Returns:
            List of CorrectionRecord instances, oldest first, in the order
            they were appended to the file.
        """
        if not self._corrections_path.exists():
            return []

        try:
            raw = self._corrections_path.read_text(encoding="utf-8")
            items = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            logger.exception(
                "Could not read corrections file %s — returning empty list",
                self._corrections_path,
            )
            return []

        if not isinstance(items, list):
            logger.warning(
                "Corrections file %s has unexpected format (expected list, got %s) — returning empty list",
                self._corrections_path,
                type(items).__name__,
            )
            return []

        records: list[CorrectionRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                records.append(
                    CorrectionRecord(
                        model_id=str(item["model_id"]),
                        metric=str(item["metric"]),
                        old_value=float(item["old_value"]),
                        new_value=float(item["new_value"]),
                        divergence=float(item["divergence"]),
                        timestamp=str(item["timestamp"]),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning(
                    "Skipping malformed correction entry in %s: %r",
                    self._corrections_path,
                    item,
                )

        return records
