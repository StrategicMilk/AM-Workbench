"""Governed online feature context retrieval."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from vetinari.workbench.data_quality import DataQualityReport, require_trusted_dataset_revision
from vetinari.workbench.feature_store.definitions import ContextViewDefinition, FeatureDefinition
from vetinari.workbench.lineage import LineageGraph, assert_lineage_allows_dataset_consumption

logger = logging.getLogger(__name__)


class OnlineContextError(Exception):
    """Raised when online feature context cannot be returned safely."""


@dataclass(frozen=True, slots=True)
class OnlineFeatureValue:
    """One online feature value with freshness and governance evidence."""

    entity_id: str
    feature_id: str
    event_time_utc: str
    observed_at_utc: str
    freshness_deadline_utc: str
    value: Any
    dataset_revision_id: str
    quality_report_id: str
    lineage_graph_id: str
    source_evidence_ref: str
    rag_chunk_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.entity_id, "entity_id")
        _require_non_empty(self.feature_id, "feature_id")
        _parse_time(self.event_time_utc, "event_time_utc")
        _parse_time(self.observed_at_utc, "observed_at_utc")
        _parse_time(self.freshness_deadline_utc, "freshness_deadline_utc")
        _require_non_empty(self.dataset_revision_id, "dataset_revision_id")
        _require_non_empty(self.quality_report_id, "quality_report_id")
        _require_non_empty(self.lineage_graph_id, "lineage_graph_id")
        _require_non_empty(self.source_evidence_ref, "source_evidence_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"OnlineFeatureValue(entity_id={self.entity_id!r}, feature_id={self.feature_id!r}, event_time_utc={self.event_time_utc!r})"


@dataclass(frozen=True, slots=True)
class ContextRetrievalRequest:
    """Online retrieval request for one entity and context view."""

    entity_id: str
    context_view_id: str
    requested_feature_ids: tuple[str, ...]
    request_time_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.entity_id, "entity_id")
        _require_non_empty(self.context_view_id, "context_view_id")
        _require_non_empty_tuple(self.requested_feature_ids, "requested_feature_ids")
        _parse_time(self.request_time_utc, "request_time_utc")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ContextRetrievalRequest(entity_id={self.entity_id!r}, context_view_id={self.context_view_id!r}, requested_feature_ids={self.requested_feature_ids!r})"


@dataclass(frozen=True, slots=True)
class ContextRetrievalDecision:
    """Inclusion or rejection decision for a feature value."""

    feature_id: str
    included: bool
    reason: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.feature_id, "feature_id")
        _require_non_empty(self.reason, "reason")
        _require_non_empty(self.evidence_ref, "evidence_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ContextRetrievalDecision(feature_id={self.feature_id!r}, included={self.included!r}, reason={self.reason!r})"


@dataclass(frozen=True, slots=True)
class ContextRetrievalResult:
    """Online context retrieval result with included and rejected values."""

    entity_id: str
    context_view_id: str
    values: dict[str, Any]
    decisions: tuple[ContextRetrievalDecision, ...]
    rag_chunk_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.entity_id, "entity_id")
        _require_non_empty(self.context_view_id, "context_view_id")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ContextRetrievalResult(entity_id={self.entity_id!r}, context_view_id={self.context_view_id!r}, values={self.values!r})"


class OnlineContextStore:
    """Instance-owned in-memory index for explicit online feature values."""

    def __init__(
        self,
        *,
        feature_definitions: tuple[FeatureDefinition, ...],
        context_views: tuple[ContextViewDefinition, ...],
        values: tuple[OnlineFeatureValue, ...],
    ) -> None:
        if not feature_definitions:
            raise OnlineContextError("feature_definitions must be non-empty")
        if not context_views:
            raise OnlineContextError("context_views must be non-empty")
        self._feature_definitions = {definition.feature_id: definition for definition in feature_definitions}
        self._context_views = {view.context_view_id: view for view in context_views}
        self._values: dict[tuple[str, str], OnlineFeatureValue] = {}
        for value in values:
            if value.feature_id not in self._feature_definitions:
                raise OnlineContextError(f"online value {value.feature_id!r} has no feature definition")
            key = (value.entity_id, value.feature_id)
            current = self._values.get(key)
            if current is None or _parse_time(value.event_time_utc, "event_time_utc") > _parse_time(
                current.event_time_utc,
                "event_time_utc",
            ):
                self._values[key] = value

    def retrieve_context(
        self,
        request: ContextRetrievalRequest,
        *,
        quality_reports: dict[str, DataQualityReport],
        lineage_graphs: dict[str, LineageGraph],
    ) -> ContextRetrievalResult:
        """Return governed feature context or fail closed for required gaps.

        Returns:
            ContextRetrievalResult value produced by retrieve_context().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        view = self._context_views.get(request.context_view_id)
        if view is None:
            raise OnlineContextError(f"context view {request.context_view_id!r} is not registered")
        if request.entity_id == "":
            raise OnlineContextError("entity_id must be non-empty")
        requested = tuple(request.requested_feature_ids)
        unknown = tuple(feature_id for feature_id in requested if feature_id not in view.feature_ids)
        if unknown:
            raise OnlineContextError(f"requested features outside context view: {', '.join(unknown)}")

        request_time = _parse_time(request.request_time_utc, "request_time_utc")
        values: dict[str, Any] = {}
        decisions: list[ContextRetrievalDecision] = []
        rag_chunk_ids: list[str] = []
        errors: list[str] = []
        for feature_id in requested:
            feature = self._feature_definitions[feature_id]
            stored = self._values.get((request.entity_id, feature_id))
            if stored is None:
                decisions.append(ContextRetrievalDecision(feature_id, False, "missing_value", f"missing:{feature_id}"))
                errors.append(f"{feature_id}:missing_value")
                continue
            try:
                _validate_value_governance(stored, feature, quality_reports, lineage_graphs)
                if _parse_time(stored.freshness_deadline_utc, "freshness_deadline_utc") < request_time:
                    raise OnlineContextError("stale_value")
            except OnlineContextError as exc:
                logger.warning("Handled recoverable failure before fallback.", exc_info=True)
                decisions.append(ContextRetrievalDecision(feature_id, False, str(exc), stored.source_evidence_ref))
                errors.append(f"{feature_id}:{exc}")
                continue
            values[feature_id] = stored.value
            rag_chunk_ids.extend(stored.rag_chunk_ids)
            decisions.append(ContextRetrievalDecision(feature_id, True, "included", stored.source_evidence_ref))
        if errors and view.required:
            raise OnlineContextError("; ".join(errors))
        return ContextRetrievalResult(
            entity_id=request.entity_id,
            context_view_id=request.context_view_id,
            values=values,
            decisions=tuple(decisions),
            rag_chunk_ids=tuple(dict.fromkeys(rag_chunk_ids)),
        )


def _validate_value_governance(
    value: OnlineFeatureValue,
    feature: FeatureDefinition,
    quality_reports: dict[str, DataQualityReport],
    lineage_graphs: dict[str, LineageGraph],
) -> None:
    if value.dataset_revision_id != feature.lineage.dataset_revision_id:
        raise OnlineContextError("dataset_revision_id_mismatch")
    if value.quality_report_id != feature.lineage.quality_report_id:
        raise OnlineContextError("quality_report_id_mismatch")
    if value.lineage_graph_id != feature.lineage.lineage_graph_id:
        raise OnlineContextError("lineage_graph_id_mismatch")
    quality_report = quality_reports.get(value.quality_report_id)
    if quality_report is None:
        raise OnlineContextError("missing_quality_report")
    lineage_graph = lineage_graphs.get(value.lineage_graph_id)
    if lineage_graph is None:
        raise OnlineContextError("missing_lineage_graph")
    require_trusted_dataset_revision(quality_report, dataset_revision_id=value.dataset_revision_id)
    assert_lineage_allows_dataset_consumption(lineage_graph, dataset_revision_id=value.dataset_revision_id)


def _parse_time(value: str, field_name: str) -> datetime:
    _require_non_empty(value, field_name)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OnlineContextError(f"{field_name} must be ISO8601") from exc


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise OnlineContextError(f"{field_name} must be non-empty")


def _require_non_empty_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise OnlineContextError(f"{field_name} must be a non-empty tuple")
    for value in values:
        _require_non_empty(value, f"{field_name}[]")


__all__ = [
    "ContextRetrievalDecision",
    "ContextRetrievalRequest",
    "ContextRetrievalResult",
    "OnlineContextError",
    "OnlineContextStore",
    "OnlineFeatureValue",
]
