"""Workbench training-set builders for governed feature data."""

from __future__ import annotations

from dataclasses import dataclass

from vetinari.workbench.data_quality import DataQualityReport
from vetinari.workbench.feature_store.training import (
    EntityLabelRow,
    FeatureObservation,
    PointInTimeTrainingSet,
    build_point_in_time_training_set,
)
from vetinari.workbench.lineage import LineageGraph


@dataclass(frozen=True, slots=True)
class GovernedFeatureTrainingSetRequest:
    """Inputs required to build a point-in-time feature training set."""

    dataset_revision_id: str
    label_rows: tuple[EntityLabelRow, ...]
    observations: tuple[FeatureObservation, ...]
    required_feature_ids: tuple[str, ...]
    quality_report: DataQualityReport
    lineage_graph: LineageGraph

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            "GovernedFeatureTrainingSetRequest("
            f"dataset_revision_id={self.dataset_revision_id!r}, "
            f"label_rows={len(self.label_rows)}, observations={len(self.observations)})"
        )


def build_governed_feature_training_set(
    request: GovernedFeatureTrainingSetRequest,
) -> PointInTimeTrainingSet:
    """Build a governed Workbench feature training set.

    Args:
        request: Validated request carrying labels, observations, data
            quality evidence, and lineage evidence.

    Returns:
        Point-in-time feature training set with future observations rejected.
    """
    return build_point_in_time_training_set(
        dataset_revision_id=request.dataset_revision_id,
        label_rows=request.label_rows,
        observations=request.observations,
        required_feature_ids=request.required_feature_ids,
        quality_report=request.quality_report,
        lineage_graph=request.lineage_graph,
    )


__all__ = [
    "GovernedFeatureTrainingSetRequest",
    "build_governed_feature_training_set",
]
