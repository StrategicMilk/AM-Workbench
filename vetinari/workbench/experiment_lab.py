"""Controlled-comparison experiment lab for Workbench artifacts.

The lab records append-only comparisons across prompts, models, tools,
retrieval setups, workflows, policies, datasets, and runtimes. It is import
safe: storage is opened only when a service method is called.
"""

from __future__ import annotations

import json
import math
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Final

from vetinari.constants import OUTPUTS_DIR
from vetinari.workbench import experiment_lab_serialization as _serialization
from vetinari.workbench import spine_consumers

_PROJECT_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TRAVERSAL_MARKERS: Final[tuple[str, ...]] = ("/", "\\", "..", "\x00", " ", ";")
_DEFAULT_STORE_ROOT: Final[Path] = OUTPUTS_DIR / "workbench" / "experiment_lab"
_EXPERIMENT_STORE_LOCK: Final[threading.Lock] = threading.Lock()

ArtifactExists = Callable[[str, str], bool]


class ExperimentLabError(Exception):
    """Raised when experiment lab state cannot be safely served."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


class ExperimentValidationError(ExperimentLabError, ValueError):
    """Raised when an experiment request fails closed validation."""


class ExperimentStoreUnavailable(ExperimentLabError):
    """Raised when append-only storage is unreadable or corrupt."""


class ExperimentProjectIdRejected(ValueError):
    """Raised when a project id is not canonical."""

    def __init__(self, value: object) -> None:
        super().__init__(f"invalid project_id {value!r}; use [A-Za-z0-9_-] up to 64 characters")
        self.value = value


class ExperimentDecision(str, Enum):
    """Final controlled-comparison decision vocabulary."""

    REJECT = "reject"
    RETRY = "retry"
    PROMOTE = "promote"
    ROLLBACK = "rollback"
    DEFER = "defer"


@dataclass(frozen=True, slots=True)
class ExperimentArtifactRef:
    """Stable reference to an upstream workbench artifact."""

    artifact_id: str
    artifact_kind: str
    label: str = ""

    def __post_init__(self) -> None:
        if not self.artifact_id.strip():
            raise ExperimentValidationError("artifact-id-missing", "artifact_id is required")
        if not self.artifact_kind.strip():
            raise ExperimentValidationError("artifact-kind-missing", "artifact_kind is required")


@dataclass(frozen=True, slots=True)
class ExperimentSampleRef:
    """Dataset, trace, playground, or benchmark sample under comparison."""

    sample_id: str
    sample_kind: str
    source: str = ""

    def __post_init__(self) -> None:
        if not self.sample_id.strip():
            raise ExperimentValidationError("sample-id-missing", "sample_id is required")
        if not self.sample_kind.strip():
            raise ExperimentValidationError("sample-kind-missing", "sample_kind is required")


@dataclass(frozen=True, slots=True)
class MetricObservation:
    """One baseline-vs-candidate metric observation."""

    name: str
    baseline_value: float
    candidate_value: float
    unit: str = ""
    higher_is_better: bool = True

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ExperimentValidationError("metric-name-missing", "metric name is required")
        for field_name, value in (
            ("baseline_value", self.baseline_value),
            ("candidate_value", self.candidate_value),
        ):
            if not isinstance(value, int | float) or not math.isfinite(float(value)):
                raise ExperimentValidationError("metric-not-finite", f"{field_name} must be finite")

    @property
    def delta(self) -> float:
        """Return candidate minus baseline."""
        return float(self.candidate_value) - float(self.baseline_value)

    @property
    def improved(self) -> bool:
        """Return whether the candidate improved this metric."""
        return self.delta > 0 if self.higher_is_better else self.delta < 0

    @property
    def regressed(self) -> bool:
        """Return whether the candidate regressed this metric."""
        return self.delta < 0 if self.higher_is_better else self.delta > 0

    def __repr__(self) -> str:
        return (
            "MetricObservation("
            f"name={self.name!r}, baseline_value={self.baseline_value!r}, "
            f"candidate_value={self.candidate_value!r}, delta={self.delta!r})"
        )


@dataclass(frozen=True, slots=True)
class CostLatencySummary:
    """Measured latency and cost for the candidate run."""

    latency_ms: float
    cost_usd: float

    def __post_init__(self) -> None:
        for field_name, value in (("latency_ms", self.latency_ms), ("cost_usd", self.cost_usd)):
            if not isinstance(value, int | float) or not math.isfinite(float(value)) or float(value) < 0:
                raise ExperimentValidationError("cost-latency-invalid", f"{field_name} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ReviewNote:
    """Human review attached to a comparison."""

    reviewer: str
    summary: str

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ExperimentValidationError("review-missing", "human review summary is required")


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
    """Append-only controlled-comparison record."""

    experiment_id: str
    project_id: str
    hypothesis: str
    baseline: ExperimentArtifactRef
    candidate: ExperimentArtifactRef
    sample_ref: ExperimentSampleRef
    metrics: tuple[MetricObservation, ...]
    latency_ms: float
    cost_usd: float
    human_review: ReviewNote
    decision: ExperimentDecision
    rationale: str
    created_at_utc: str

    def __repr__(self) -> str:
        return (
            "ExperimentRecord("
            f"experiment_id={self.experiment_id!r}, project_id={self.project_id!r}, "
            f"decision={self.decision.value!r}, metric_count={len(self.metrics)})"
        )


def validate_project_id(value: str | None) -> str:
    """Return a canonical project id or reject traversal-bearing input.

    Args:
        value: Candidate project id.

    Returns:
        The canonical project id string.

    Raises:
        ExperimentProjectIdRejected: If the id is missing, too long, or unsafe.
    """
    if not isinstance(value, str):
        raise ExperimentProjectIdRejected(value)
    if not value or len(value) > 64 or _PROJECT_ID_RE.fullmatch(value) is None:
        raise ExperimentProjectIdRejected(value)
    if any(marker in value for marker in _TRAVERSAL_MARKERS):
        raise ExperimentProjectIdRejected(value)
    return value


def _coerce_decision(value: ExperimentDecision | str) -> ExperimentDecision:
    if isinstance(value, ExperimentDecision):
        return value
    try:
        return ExperimentDecision(getattr(value, "value", value))
    except ValueError as exc:
        raise ExperimentValidationError("decision-unknown", f"unknown experiment decision {value!r}") from exc


def _coerce_metrics(metrics: Iterable[MetricObservation | Mapping[str, Any]]) -> tuple[MetricObservation, ...]:
    rows: list[MetricObservation] = []
    for row in metrics:
        if isinstance(row, MetricObservation):
            rows.append(row)
        elif is_dataclass(row) and not isinstance(row, type):
            rows.append(MetricObservation(**asdict(row)))
        elif isinstance(row, Mapping):
            rows.append(MetricObservation(**dict(row)))
        else:
            raise ExperimentValidationError("metric-invalid", "metrics must be MetricObservation rows")
    if not rows:
        raise ExperimentValidationError("metrics-missing", "at least one metric is required")
    return tuple(rows)


def _artifact_from_mapping(value: ExperimentArtifactRef | Mapping[str, Any]) -> ExperimentArtifactRef:
    if isinstance(value, ExperimentArtifactRef):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return ExperimentArtifactRef(**asdict(value))
    if isinstance(value, Mapping):
        return ExperimentArtifactRef(**dict(value))
    raise ExperimentValidationError("artifact-invalid", "artifact references must be structured")


def _sample_from_mapping(value: ExperimentSampleRef | Mapping[str, Any]) -> ExperimentSampleRef:
    if isinstance(value, ExperimentSampleRef):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return ExperimentSampleRef(**asdict(value))
    if isinstance(value, Mapping):
        return ExperimentSampleRef(**dict(value))
    raise ExperimentValidationError("sample-invalid", "sample_ref must be structured")


def _review_from_value(value: ReviewNote | Mapping[str, Any] | str) -> ReviewNote:
    if isinstance(value, ReviewNote):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return ReviewNote(**asdict(value))
    if isinstance(value, Mapping):
        return ReviewNote(**dict(value))
    if isinstance(value, str):
        return ReviewNote(reviewer="", summary=value)
    raise ExperimentValidationError("review-invalid", "human_review must be structured")


_serialization.configure_serialization(
    {
        "ExperimentArtifactRef": ExperimentArtifactRef,
        "ExperimentSampleRef": ExperimentSampleRef,
        "MetricObservation": MetricObservation,
        "ReviewNote": ReviewNote,
        "ExperimentRecord": ExperimentRecord,
    },
    validate_project_id,
    _coerce_decision,
)
_record_to_json = _serialization._record_to_json
_record_from_json = _serialization._record_from_json


class ExperimentLab:
    """Append-only service for controlled workbench comparisons."""

    def __init__(
        self,
        *,
        store_root: Path | str = _DEFAULT_STORE_ROOT,
        artifact_exists: ArtifactExists | None = None,
    ) -> None:
        self._store_root = Path(store_root)
        self._artifact_exists = artifact_exists

    def record_experiment(
        self,
        *,
        project_id: str = "default",
        hypothesis: str,
        baseline: ExperimentArtifactRef | Mapping[str, Any],
        candidate: ExperimentArtifactRef | Mapping[str, Any],
        sample_ref: ExperimentSampleRef | Mapping[str, Any],
        metrics: Iterable[MetricObservation | Mapping[str, Any]],
        latency_ms: float,
        cost_usd: float,
        human_review: ReviewNote | Mapping[str, Any] | str,
        decision: ExperimentDecision | str,
        rationale: str,
    ) -> ExperimentRecord:
        """Validate and append one experiment record.

        Args:
            project_id: Project storage scope.
            hypothesis: Comparison hypothesis.
            baseline: Baseline artifact reference.
            candidate: Candidate artifact reference.
            sample_ref: Dataset, trace, playground, or benchmark sample reference.
            metrics: Measured baseline/candidate metric observations.
            latency_ms: Candidate latency in milliseconds.
            cost_usd: Candidate cost in USD.
            human_review: Human review note.
            decision: Final decision vocabulary value.
            rationale: Decision rationale.

        Returns:
            The persisted immutable experiment record.

        Raises:
            ExperimentValidationError: If the comparison is incomplete or unsafe.
            ExperimentStoreUnavailable: If storage cannot be written.
        """
        canonical_project = validate_project_id(project_id)
        hypothesis = hypothesis.strip()
        rationale = rationale.strip()
        if not hypothesis:
            raise ExperimentValidationError("hypothesis-missing", "hypothesis is required")
        if not rationale:
            raise ExperimentValidationError("rationale-missing", "rationale is required")
        baseline_ref = _artifact_from_mapping(baseline)
        candidate_ref = _artifact_from_mapping(candidate)
        sample = _sample_from_mapping(sample_ref)
        metric_rows = _coerce_metrics(metrics)
        review = _review_from_value(human_review)
        cost_latency = CostLatencySummary(latency_ms=latency_ms, cost_usd=cost_usd)
        final_decision = _coerce_decision(decision)
        self._validate_upstream_refs((baseline_ref, candidate_ref), sample)
        self._validate_decision(final_decision, metric_rows)
        record = ExperimentRecord(
            experiment_id=self._new_experiment_id(canonical_project, hypothesis, baseline_ref, candidate_ref),
            project_id=canonical_project,
            hypothesis=hypothesis,
            baseline=baseline_ref,
            candidate=candidate_ref,
            sample_ref=sample,
            metrics=metric_rows,
            latency_ms=cost_latency.latency_ms,
            cost_usd=cost_latency.cost_usd,
            human_review=review,
            decision=final_decision,
            rationale=rationale,
            created_at_utc=datetime.now(timezone.utc).isoformat(),
        )
        self._append_record(record)
        return record

    def list_experiments(self, *, project_id: str = "default") -> tuple[ExperimentRecord, ...]:
        """Return immutable experiment records for one project.

        Args:
            project_id: Project storage scope.

        Returns:
            Tuple of immutable experiment records.

        Raises:
            ExperimentProjectIdRejected: If the project id is unsafe.
            ExperimentStoreUnavailable: If storage is unreadable or corrupt.
        """
        canonical_project = validate_project_id(project_id)
        return tuple(record for record in self._read_all(canonical_project) if record.project_id == canonical_project)

    def get_experiment(self, experiment_id: str, *, project_id: str = "default") -> ExperimentRecord | None:
        """Return one experiment by id.

        Args:
            experiment_id: Stable experiment id.
            project_id: Project storage scope.

        Returns:
            The matching record, or ``None`` when absent.

        Raises:
            ExperimentValidationError: If the experiment id is empty.
            ExperimentStoreUnavailable: If storage is unreadable or corrupt.
        """
        if not experiment_id.strip():
            raise ExperimentValidationError("experiment-id-missing", "experiment_id is required")
        for record in self.list_experiments(project_id=project_id):
            if record.experiment_id == experiment_id:
                return record
        return None

    def record_decision(
        self,
        experiment_id: str,
        *,
        project_id: str = "default",
        decision: ExperimentDecision | str,
        rationale: str,
        human_review: ReviewNote | Mapping[str, Any] | str,
    ) -> ExperimentRecord:
        """Append a new decision record derived from an existing experiment.

        Args:
            experiment_id: Existing experiment id.
            project_id: Project storage scope.
            decision: Replacement decision vocabulary value.
            rationale: Decision rationale.
            human_review: Human review note.

        Returns:
            The new append-only decision record.

        Raises:
            ExperimentValidationError: If the experiment is absent or decision is invalid.
            ExperimentStoreUnavailable: If storage cannot be read or written.
        """
        existing = self.get_experiment(experiment_id, project_id=project_id)
        if existing is None:
            raise ExperimentValidationError("experiment-not-found", f"experiment {experiment_id!r} was not found")
        return self.record_experiment(
            project_id=existing.project_id,
            hypothesis=existing.hypothesis,
            baseline=existing.baseline,
            candidate=existing.candidate,
            sample_ref=existing.sample_ref,
            metrics=existing.metrics,
            latency_ms=existing.latency_ms,
            cost_usd=existing.cost_usd,
            human_review=human_review,
            decision=decision,
            rationale=rationale,
        )

    def _validate_upstream_refs(
        self,
        artifacts: tuple[ExperimentArtifactRef, ExperimentArtifactRef],
        sample: ExperimentSampleRef,
    ) -> None:
        if self._artifact_exists is None:
            return
        for artifact in artifacts:
            if not self._artifact_exists(artifact.artifact_kind, artifact.artifact_id):
                raise ExperimentValidationError(
                    "upstream-artifact-missing",
                    f"{artifact.artifact_kind} {artifact.artifact_id!r} is not present",
                )
        if not self._artifact_exists(sample.sample_kind, sample.sample_id):
            raise ExperimentValidationError(
                "upstream-sample-missing",
                f"{sample.sample_kind} {sample.sample_id!r} is not present",
            )

    @staticmethod
    def _validate_decision(decision: ExperimentDecision, metrics: tuple[MetricObservation, ...]) -> None:
        decision_value = getattr(decision, "value", decision)
        if decision_value == ExperimentDecision.PROMOTE.value and not any(metric.improved for metric in metrics):
            raise ExperimentValidationError(
                "promote-without-positive-evidence",
                "promote requires at least one measured positive candidate delta",
            )
        if decision_value == ExperimentDecision.ROLLBACK.value and not any(metric.regressed for metric in metrics):
            raise ExperimentValidationError(
                "rollback-without-regression-evidence",
                "rollback requires at least one measured candidate regression",
            )

    @staticmethod
    def _new_experiment_id(
        project_id: str,
        hypothesis: str,
        baseline: ExperimentArtifactRef,
        candidate: ExperimentArtifactRef,
    ) -> str:
        seed = "|".join((
            project_id,
            hypothesis,
            baseline.artifact_id,
            candidate.artifact_id,
            datetime.now(timezone.utc).isoformat(),
        ))
        return f"exp::{uuid.uuid5(uuid.NAMESPACE_URL, seed)}"

    def _store_path(self, project_id: str) -> Path:
        return self._store_root / f"{project_id}.jsonl"

    def _append_record(self, record: ExperimentRecord) -> None:
        path = self._store_path(record.project_id)
        try:
            with _EXPERIMENT_STORE_LOCK:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(_record_to_json(record))
                    handle.write("\n")
            spine_consumers.record_asset_written(
                asset_id=record.experiment_id,
                kind="eval_suite",
                project_id=record.project_id,
                path=str(path),
                redact_fields=["path"],
            )
        except OSError as exc:
            raise ExperimentStoreUnavailable("store-unwritable", f"experiment store is unwritable: {path}") from exc

    def _read_all(self, project_id: str, *, limit: int | None = None) -> tuple[ExperimentRecord, ...]:
        path = self._store_path(project_id)
        if not path.exists():
            return ()
        if limit is not None and limit < 0:
            raise ExperimentValidationError("limit-invalid", "limit must be non-negative")
        rows: list[ExperimentRecord] = []
        try:
            with _EXPERIMENT_STORE_LOCK, path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if limit is not None and len(rows) >= limit:
                        break
                    if not line.strip():
                        continue
                    try:
                        loaded = json.loads(line)
                        if not isinstance(loaded, dict):
                            raise TypeError("record row must be a mapping")
                        rows.append(_record_from_json(loaded))
                    except Exception as exc:
                        raise ExperimentStoreUnavailable(
                            "store-corrupt",
                            f"experiment store {path} row {line_number} is corrupt",
                        ) from exc
        except OSError as exc:
            raise ExperimentStoreUnavailable("store-unreadable", f"experiment store is unreadable: {path}") from exc
        return tuple(rows)


__all__ = [
    "CostLatencySummary",
    "ExperimentArtifactRef",
    "ExperimentDecision",
    "ExperimentLab",
    "ExperimentLabError",
    "ExperimentProjectIdRejected",
    "ExperimentRecord",
    "ExperimentSampleRef",
    "ExperimentStoreUnavailable",
    "ExperimentValidationError",
    "MetricObservation",
    "ReviewNote",
    "validate_project_id",
]
