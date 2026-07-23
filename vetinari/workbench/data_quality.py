"""Data quality reports for connector-fed dataset revisions.

The module is side-effect free. All state is caller-owned immutable value
objects; no files are read or written here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DataQualityError(ValueError):
    """Raised when data quality evidence cannot be trusted."""


class DataQualityCheckKind(str, Enum):
    """Required quality dimensions for connector-fed datasets."""

    FRESHNESS = "freshness"
    SCHEMA = "schema"
    COMPLETENESS = "completeness"
    INTEGRITY = "integrity"
    DISTRIBUTION = "distribution"
    SENSITIVE_DATA = "sensitive_data"


class DataQualitySeverity(str, Enum):
    """Severity of a quality result."""

    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class DataQualitySignal:
    """One branch-discriminating quality check result."""

    check_kind: DataQualityCheckKind
    passed: bool
    severity: DataQualitySeverity
    evidence_ref: str
    message: str

    def __post_init__(self) -> None:
        if not isinstance(self.check_kind, DataQualityCheckKind):
            raise DataQualityError("check_kind must be a DataQualityCheckKind")
        if not isinstance(self.severity, DataQualitySeverity):
            raise DataQualityError("severity must be a DataQualitySeverity")
        _require_non_empty(self.evidence_ref, "evidence_ref")
        _require_non_empty(self.message, "message")
        if not self.passed and self.severity is DataQualitySeverity.INFO:
            raise DataQualityError("failed signals cannot use info severity")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DataQualitySignal(check_kind={self.check_kind!r}, passed={self.passed!r}, severity={self.severity!r})"


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    """Quality evidence attached to one dataset revision."""

    quality_report_id: str
    dataset_revision_id: str
    connector_id: str
    source_card_id: str
    captured_at_utc: str
    signals: tuple[DataQualitySignal, ...]
    policy_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.quality_report_id, "quality_report_id")
        _require_non_empty(self.dataset_revision_id, "dataset_revision_id")
        _require_non_empty(self.connector_id, "connector_id")
        _require_non_empty(self.source_card_id, "source_card_id")
        _require_non_empty(self.captured_at_utc, "captured_at_utc")
        _require_non_empty(self.policy_ref, "policy_ref")
        if not isinstance(self.signals, tuple) or not self.signals:
            raise DataQualityError("signals must be a non-empty tuple")
        for signal in self.signals:
            if not isinstance(signal, DataQualitySignal):
                raise DataQualityError("signals must contain DataQualitySignal instances")

    @property
    def trusted(self) -> bool:
        """Return whether the report can be consumed as trusted."""
        verdict = evaluate_data_quality_report(self)
        return verdict.passed

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DataQualityReport(quality_report_id={self.quality_report_id!r}, dataset_revision_id={self.dataset_revision_id!r}, connector_id={self.connector_id!r})"


@dataclass(frozen=True, slots=True)
class DataQualityVerdict:
    """Fail-closed decision for consuming dataset data."""

    passed: bool
    trusted_dataset_revision_id: str | None
    rejection_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.passed:
            if not self.trusted_dataset_revision_id:
                raise DataQualityError("passed verdict requires trusted_dataset_revision_id")
            if self.rejection_reasons:
                raise DataQualityError("passed verdict cannot include rejection_reasons")
        elif not self.rejection_reasons:
            raise DataQualityError("failed verdict requires rejection_reasons")


REQUIRED_QUALITY_CHECKS: tuple[DataQualityCheckKind, ...] = (
    DataQualityCheckKind.FRESHNESS,
    DataQualityCheckKind.SCHEMA,
    DataQualityCheckKind.COMPLETENESS,
    DataQualityCheckKind.INTEGRITY,
    DataQualityCheckKind.DISTRIBUTION,
    DataQualityCheckKind.SENSITIVE_DATA,
)


def evaluate_data_quality_report(report: DataQualityReport) -> DataQualityVerdict:
    """Fail closed unless every required check has passing evidence.

    Returns:
        DataQualityVerdict value produced by evaluate_data_quality_report().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(report, DataQualityReport):
        raise DataQualityError("report must be a DataQualityReport")
    reasons: list[str] = []
    by_kind = {signal.check_kind: signal for signal in report.signals}
    missing = tuple(kind.value for kind in REQUIRED_QUALITY_CHECKS if kind not in by_kind)
    if missing:
        reasons.append(f"missing required quality checks: {', '.join(missing)}")
    failed = tuple(signal for signal in report.signals if not signal.passed)
    if failed:
        reasons.extend(f"{signal.check_kind.value} failed: {signal.message}" for signal in failed)
    blockers = tuple(signal for signal in report.signals if signal.severity is DataQualitySeverity.BLOCKER)
    if blockers:
        reasons.extend(f"{signal.check_kind.value} blocker: {signal.message}" for signal in blockers)
    if reasons:
        return DataQualityVerdict(passed=False, trusted_dataset_revision_id=None, rejection_reasons=tuple(reasons))
    return DataQualityVerdict(
        passed=True,
        trusted_dataset_revision_id=report.dataset_revision_id,
        rejection_reasons=(),
    )


def require_trusted_dataset_revision(report: DataQualityReport, *, dataset_revision_id: str) -> None:
    """Raise unless a quality report blesses the requested revision.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    _require_non_empty(dataset_revision_id, "dataset_revision_id")
    if report.dataset_revision_id != dataset_revision_id:
        raise DataQualityError(
            f"quality report {report.quality_report_id!r} is for {report.dataset_revision_id!r}, "
            f"not {dataset_revision_id!r}"
        )
    verdict = evaluate_data_quality_report(report)
    if not verdict.passed:
        raise DataQualityError("; ".join(verdict.rejection_reasons))


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise DataQualityError(f"{field_name} must be non-empty")


__all__ = [
    "REQUIRED_QUALITY_CHECKS",
    "DataQualityCheckKind",
    "DataQualityError",
    "DataQualityReport",
    "DataQualitySeverity",
    "DataQualitySignal",
    "DataQualityVerdict",
    "evaluate_data_quality_report",
    "require_trusted_dataset_revision",
]
