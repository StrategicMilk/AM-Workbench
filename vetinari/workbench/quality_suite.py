"""Scoring model for the AM Workbench quality evaluation suite.

The suite intentionally treats missing, stale, skipped, blocked, and unknown
metrics as non-passing observations. That keeps the score from false-greening
when the product has not actually produced evidence for a claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.quality_suite_scoring import (
    _metric_by_id,
    _observation_from_value,
    _readiness_level,
    _required_str,
    _score_metric,
    _suite_from_mapping,
    _weighted_mean,
)

DEFAULT_QUALITY_SUITE_PATH: Final[Path] = PROJECT_ROOT / "config" / "workbench" / "workbench_quality_suite.yaml"
DEFAULT_BENCHMARK_SOURCES_PATH: Final[Path] = (
    PROJECT_ROOT / "config" / "workbench" / "workbench_quality_benchmark_sources.yaml"
)


class QualitySuiteError(ValueError):
    """Raised when suite configuration or observations are malformed."""


class MetricDirection(str, Enum):
    """Supported scoring directions."""

    GTE = "gte"
    LTE = "lte"


class ObservationStatus(str, Enum):
    """Truth-state for one metric observation."""

    MEASURED = "measured"
    MISSING = "missing"
    STALE = "stale"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


_READINESS_ORDER: Final[tuple[str, ...]] = ("beta", "pro", "team", "enterprise_signal")


@dataclass(frozen=True, slots=True)
class ReadinessLevel:
    """One named product-readiness threshold."""

    name: str
    minimum_score: float


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """Metric definition loaded from the suite catalog."""

    metric_id: str
    category_id: str
    weight: float
    direction: MetricDirection
    zero_score_at: float
    target: float
    excellent: float
    unit: str
    claim: str
    false_green_risk: str
    gate: str | None = None

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MetricSpec(metric_id={self.metric_id!r}, category_id={self.category_id!r}, weight={self.weight!r})"


@dataclass(frozen=True, slots=True)
class CategorySpec:
    """Weighted category of metrics."""

    category_id: str
    weight: float
    claim: str
    metrics: tuple[MetricSpec, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CategorySpec(category_id={self.category_id!r}, weight={self.weight!r}, claim={self.claim!r})"


@dataclass(frozen=True, slots=True)
class BenchmarkSourceSpec:
    """External benchmark catalog entry used only for calibration."""

    source_id: str
    calibration_only: bool
    source_type: str
    task_family: str

    def __repr__(self) -> str:
        return (
            "BenchmarkSourceSpec("
            f"source_id={self.source_id!r}, calibration_only={self.calibration_only!r}, "
            f"source_type={self.source_type!r})"
        )


@dataclass(frozen=True, slots=True)
class WorkbenchQualitySuite:
    """Full Workbench quality suite definition."""

    schema_version: int
    suite_id: str
    description: str
    readiness_levels: tuple[ReadinessLevel, ...]
    categories: tuple[CategorySpec, ...]
    benchmark_sources: tuple[BenchmarkSourceSpec, ...] = ()

    @property
    def metric_ids(self) -> tuple[str, ...]:
        """Return metric ids in catalog order."""
        return tuple(metric.metric_id for category in self.categories for metric in category.metrics)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchQualitySuite(schema_version={self.schema_version!r}, suite_id={self.suite_id!r}, description={self.description!r})"


@dataclass(frozen=True, slots=True)
class QualityMetricObservation:
    """One measured or non-measured metric value."""

    value: float | None
    status: ObservationStatus = ObservationStatus.MEASURED
    sample_size: int | None = None
    captured_at_utc: str | None = None
    evidence_ref: str = ""
    lineage_ref: str = ""
    note: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"QualityMetricObservation(value={self.value!r}, status={self.status!r}, sample_size={self.sample_size!r})"
        )


@dataclass(frozen=True, slots=True)
class MetricScore:
    """Score for one metric."""

    metric_id: str
    category_id: str
    value: float | None
    score: float
    target: float
    passed: bool
    status: ObservationStatus
    gate: str | None
    reason: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MetricScore(metric_id={self.metric_id!r}, category_id={self.category_id!r}, value={self.value!r})"


@dataclass(frozen=True, slots=True)
class CategoryScore:
    """Weighted score for one category."""

    category_id: str
    score: float
    weight: float
    metric_scores: tuple[MetricScore, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CategoryScore(category_id={self.category_id!r}, score={self.score!r}, weight={self.weight!r})"


@dataclass(frozen=True, slots=True)
class WorkbenchQualityReport:
    """Overall quality report for a set of Workbench observations."""

    suite_id: str
    overall_score: float
    readiness_level: str
    blocking_gates: tuple[str, ...]
    category_scores: tuple[CategoryScore, ...]
    missing_metric_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a stable JSON-serializable report."""
        return {
            "suite_id": self.suite_id,
            "overall_score": round(self.overall_score, 2),
            "readiness_level": self.readiness_level,
            "blocking_gates": list(self.blocking_gates),
            "missing_metric_ids": list(self.missing_metric_ids),
            "category_scores": [
                {
                    "category_id": category.category_id,
                    "score": round(category.score, 2),
                    "weight": category.weight,
                    "metric_scores": [
                        {
                            "metric_id": metric.metric_id,
                            "value": metric.value,
                            "score": round(metric.score, 2),
                            "target": metric.target,
                            "passed": metric.passed,
                            "status": metric.status.value,
                            "gate": metric.gate,
                            "reason": metric.reason,
                        }
                        for metric in category.metric_scores
                    ],
                }
                for category in self.category_scores
            ],
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchQualityReport(suite_id={self.suite_id!r}, overall_score={self.overall_score!r}, readiness_level={self.readiness_level!r})"


def load_workbench_quality_suite(path: Path | str = DEFAULT_QUALITY_SUITE_PATH) -> WorkbenchQualitySuite:
    """Load and validate the Workbench quality suite catalog.

    Returns:
        Resolved workbench quality suite value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    suite_path = Path(path)
    if not suite_path.exists():
        raise QualitySuiteError(f"quality suite file not found: {suite_path}")
    payload = yaml.safe_load(suite_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise QualitySuiteError("quality suite must be a mapping")
    return _suite_from_mapping(payload)


def load_benchmark_source_catalog(path: Path | str = DEFAULT_BENCHMARK_SOURCES_PATH) -> tuple[BenchmarkSourceSpec, ...]:
    """Load external benchmark sources and enforce calibration-only classification.

    Returns:
        Parsed calibration-only benchmark source entries.

    Raises:
        QualitySuiteError: if the catalog is missing required fields or tries
            to define a live quality signal.
    """
    catalog_path = Path(path)
    payload = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise QualitySuiteError("benchmark source catalog must be a mapping")
    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        raise QualitySuiteError("benchmark source catalog must define sources")
    parsed: list[BenchmarkSourceSpec] = []
    for index, raw_source in enumerate(sources):
        source = dict(raw_source)
        source_id = _required_str(source, "source_id")
        if "calibration_only" not in source:
            raise QualitySuiteError(f"benchmark source {source_id} missing calibration_only")
        calibration_only = bool(source["calibration_only"])
        if not calibration_only:
            raise QualitySuiteError(f"benchmark source {source_id} is not allowed as a live quality signal")
        parsed.append(
            BenchmarkSourceSpec(
                source_id=source_id,
                calibration_only=calibration_only,
                source_type=_required_str(source, "source_type"),
                task_family=_required_str(source, "task_family"),
            )
        )
        if not parsed[-1].source_id:
            raise QualitySuiteError(f"benchmark source at index {index} missing source_id")
    return tuple(parsed)


def load_metric_observations(path: Path | str) -> dict[str, QualityMetricObservation]:
    """Load metric observations from a JSON file.

        The expected shape is ``{"metrics": {"metric_id": 0.9}}`` or
        ``{"metrics": {"metric_id": {"value": 0.9, "status": "measured"}}}``.

    Returns:
        Resolved metric observations value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    observation_path = Path(path)
    payload = json.loads(observation_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("metrics"), dict):
        raise QualitySuiteError("observation file must contain a metrics mapping")
    return {str(metric_id): _observation_from_value(value) for metric_id, value in dict(payload["metrics"]).items()}


def score_workbench_quality(
    observations: dict[str, QualityMetricObservation | float | int | dict[str, Any]],
    suite: WorkbenchQualitySuite | None = None,
) -> WorkbenchQualityReport:
    """Score observations against the Workbench quality suite.

    Args:
        observations: Observations value consumed by score_workbench_quality().
        suite: Suite value consumed by score_workbench_quality().

    Returns:
        Scored workbench quality result.
    """
    suite = suite or load_workbench_quality_suite()
    normalized = {metric_id: _observation_from_value(observation) for metric_id, observation in observations.items()}
    category_scores: list[CategoryScore] = []
    missing_metric_ids: list[str] = []
    blocking_gates: list[str] = []

    for category in suite.categories:
        metric_scores: list[MetricScore] = []
        for metric in category.metrics:
            observation = normalized.get(metric.metric_id)
            if observation is None:
                observation = QualityMetricObservation(value=None, status=ObservationStatus.MISSING)
                missing_metric_ids.append(metric.metric_id)
            metric_score = _score_metric(metric, observation)
            metric_scores.append(metric_score)
            if metric.gate and not metric_score.passed:
                blocking_gates.append(f"{metric.gate}:{metric.metric_id}:{metric_score.reason}")

        category_score = _weighted_mean(
            (metric.score, _metric_by_id(category, metric.metric_id).weight) for metric in metric_scores
        )
        category_scores.append(
            CategoryScore(
                category_id=category.category_id,
                score=category_score,
                weight=category.weight,
                metric_scores=tuple(metric_scores),
            )
        )

    overall_score = _weighted_mean((category.score, category.weight) for category in category_scores)
    readiness_level = _readiness_level(overall_score, suite.readiness_levels, blocking_gates)
    return WorkbenchQualityReport(
        suite_id=suite.suite_id,
        overall_score=overall_score,
        readiness_level=readiness_level,
        blocking_gates=tuple(blocking_gates),
        category_scores=tuple(category_scores),
        missing_metric_ids=tuple(missing_metric_ids),
    )


def meets_readiness(report: WorkbenchQualityReport, minimum_level: str) -> bool:
    """Return whether ``report`` satisfies ``minimum_level``.

    Args:
        report: Report value consumed by meets_readiness().
        minimum_level: Minimum level value consumed by meets_readiness().

    Returns:
        bool value produced by meets_readiness().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if minimum_level not in _READINESS_ORDER:
        raise QualitySuiteError(f"unknown readiness level: {minimum_level}")
    if report.readiness_level == "not_ready":
        return False
    return _READINESS_ORDER.index(report.readiness_level) >= _READINESS_ORDER.index(minimum_level)


__all__ = [
    "DEFAULT_QUALITY_SUITE_PATH",
    "BenchmarkSourceSpec",
    "MetricDirection",
    "MetricScore",
    "ObservationStatus",
    "QualityMetricObservation",
    "QualitySuiteError",
    "WorkbenchQualityReport",
    "WorkbenchQualitySuite",
    "load_benchmark_source_catalog",
    "load_metric_observations",
    "load_workbench_quality_suite",
    "meets_readiness",
    "score_workbench_quality",
]
