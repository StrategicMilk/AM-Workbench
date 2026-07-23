"""Point-in-time correct training-set extraction for Workbench features."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from vetinari.workbench.data_quality import DataQualityReport, require_trusted_dataset_revision
from vetinari.workbench.lineage import LineageGraph, assert_lineage_allows_dataset_consumption


class FeatureTrainingSetError(Exception):
    """Raised when point-in-time training extraction cannot be trusted."""


@dataclass(frozen=True, slots=True)
class FeatureObservation:
    """One observed feature value with governance evidence."""

    entity_id: str
    feature_id: str
    event_time_utc: str
    observed_at_utc: str
    value: Any
    dataset_revision_id: str
    quality_report_id: str
    lineage_graph_id: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.entity_id, "entity_id")
        _require_non_empty(self.feature_id, "feature_id")
        _parse_time(self.event_time_utc, "event_time_utc")
        _parse_time(self.observed_at_utc, "observed_at_utc")
        _require_non_empty(self.dataset_revision_id, "dataset_revision_id")
        _require_non_empty(self.quality_report_id, "quality_report_id")
        _require_non_empty(self.lineage_graph_id, "lineage_graph_id")
        _require_non_empty(self.evidence_ref, "evidence_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"FeatureObservation(entity_id={self.entity_id!r}, feature_id={self.feature_id!r}, event_time_utc={self.event_time_utc!r})"


@dataclass(frozen=True, slots=True)
class EntityLabelRow:
    """Label row that defines the as-of time for feature joins."""

    entity_id: str
    as_of_time_utc: str
    label: Any
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.entity_id, "entity_id")
        _parse_time(self.as_of_time_utc, "as_of_time_utc")
        _require_non_empty(self.evidence_ref, "evidence_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EntityLabelRow(entity_id={self.entity_id!r}, as_of_time_utc={self.as_of_time_utc!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class TrainingExample:
    """One point-in-time feature vector plus rejected candidate observations."""

    entity_id: str
    as_of_time_utc: str
    label: Any
    features: dict[str, Any]
    feature_evidence_refs: dict[str, str]
    dataset_revision_id: str
    rejected_observations: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.entity_id, "entity_id")
        _parse_time(self.as_of_time_utc, "as_of_time_utc")
        _require_non_empty(self.dataset_revision_id, "dataset_revision_id")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TrainingExample(entity_id={self.entity_id!r}, as_of_time_utc={self.as_of_time_utc!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class PointInTimeTrainingSet:
    """Extracted training set with governance and rejection details."""

    dataset_revision_id: str
    quality_report_id: str
    lineage_graph_id: str
    examples: tuple[TrainingExample, ...]
    rejected_observations: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.dataset_revision_id, "dataset_revision_id")
        _require_non_empty(self.quality_report_id, "quality_report_id")
        _require_non_empty(self.lineage_graph_id, "lineage_graph_id")
        if not isinstance(self.examples, tuple):
            raise FeatureTrainingSetError("examples must be a tuple")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PointInTimeTrainingSet(dataset_revision_id={self.dataset_revision_id!r}, quality_report_id={self.quality_report_id!r}, lineage_graph_id={self.lineage_graph_id!r})"


def build_point_in_time_training_set(
    *,
    dataset_revision_id: str,
    label_rows: tuple[EntityLabelRow, ...],
    observations: tuple[FeatureObservation, ...],
    required_feature_ids: tuple[str, ...],
    quality_report: DataQualityReport,
    lineage_graph: LineageGraph,
) -> PointInTimeTrainingSet:
    """Build feature vectors without future leakage or untrusted governance.

    Returns:
        Newly constructed point in time training set value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    _require_non_empty(dataset_revision_id, "dataset_revision_id")
    _require_non_empty_tuple(required_feature_ids, "required_feature_ids")
    if not label_rows:
        raise FeatureTrainingSetError("label_rows must be non-empty")
    require_trusted_dataset_revision(quality_report, dataset_revision_id=dataset_revision_id)
    assert_lineage_allows_dataset_consumption(lineage_graph, dataset_revision_id=dataset_revision_id)

    rejected_all: list[tuple[str, str]] = []
    examples: list[TrainingExample] = []
    for label_row in label_rows:
        as_of = _parse_time(label_row.as_of_time_utc, "as_of_time_utc")
        selected: dict[str, FeatureObservation] = {}
        rejected_for_example: list[tuple[str, str]] = []
        for observation in observations:
            _validate_observation_governance(
                observation,
                dataset_revision_id=dataset_revision_id,
                quality_report_id=quality_report.quality_report_id,
                lineage_graph_id=lineage_graph.graph_id,
            )
            if observation.entity_id != label_row.entity_id:
                continue
            if observation.feature_id not in required_feature_ids:
                continue
            event_time = _parse_time(observation.event_time_utc, "event_time_utc")
            if event_time > as_of:
                rejected_for_example.append((observation.evidence_ref, "future_leakage"))
                continue
            current = selected.get(observation.feature_id)
            if current is None or event_time > _parse_time(current.event_time_utc, "event_time_utc"):
                selected[observation.feature_id] = observation
        missing = tuple(feature_id for feature_id in required_feature_ids if feature_id not in selected)
        if missing:
            raise FeatureTrainingSetError(f"missing feature values: {', '.join(missing)}")
        examples.append(
            TrainingExample(
                entity_id=label_row.entity_id,
                as_of_time_utc=label_row.as_of_time_utc,
                label=label_row.label,
                features={feature_id: selected[feature_id].value for feature_id in required_feature_ids},
                feature_evidence_refs={
                    feature_id: selected[feature_id].evidence_ref for feature_id in required_feature_ids
                },
                dataset_revision_id=dataset_revision_id,
                rejected_observations=tuple(rejected_for_example),
            )
        )
        rejected_all.extend(rejected_for_example)
    return PointInTimeTrainingSet(
        dataset_revision_id=dataset_revision_id,
        quality_report_id=quality_report.quality_report_id,
        lineage_graph_id=lineage_graph.graph_id,
        examples=tuple(examples),
        rejected_observations=tuple(rejected_all),
    )


def _validate_observation_governance(
    observation: FeatureObservation,
    *,
    dataset_revision_id: str,
    quality_report_id: str,
    lineage_graph_id: str,
) -> None:
    if observation.dataset_revision_id != dataset_revision_id:
        raise FeatureTrainingSetError("observation dataset_revision_id mismatch")
    if observation.quality_report_id != quality_report_id:
        raise FeatureTrainingSetError("observation quality_report_id mismatch")
    if observation.lineage_graph_id != lineage_graph_id:
        raise FeatureTrainingSetError("observation lineage_graph_id mismatch")


def _parse_time(value: str, field_name: str) -> datetime:
    _require_non_empty(value, field_name)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FeatureTrainingSetError(f"{field_name} must be ISO8601") from exc


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise FeatureTrainingSetError(f"{field_name} must be non-empty")


def _require_non_empty_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise FeatureTrainingSetError(f"{field_name} must be a non-empty tuple")
    for value in values:
        _require_non_empty(value, f"{field_name}[]")


__all__ = [
    "EntityLabelRow",
    "FeatureObservation",
    "FeatureTrainingSetError",
    "PointInTimeTrainingSet",
    "TrainingExample",
    "build_point_in_time_training_set",
]
