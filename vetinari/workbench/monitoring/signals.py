"""Typed production AI monitoring signals and fail-closed assessment."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from math import isfinite
from typing import Any

logger = logging.getLogger(__name__)


class MonitoringSignalKind(str, Enum):
    """Canonical production AI monitoring categories."""

    DATA_DRIFT = "data_drift"
    EMBEDDING_SHIFT = "embedding_shift"
    QUALITY_REGRESSION = "quality_regression"
    HALLUCINATION = "hallucination"
    TOXICITY = "toxicity"
    PII_PHI = "pii_phi"
    PROMPT_CHANGE = "prompt_change"
    MODEL_CHANGE = "model_change"
    PROVIDER_CHANGE = "provider_change"
    ENDPOINT_SLO = "endpoint_slo"
    TOOL_CALL_FAILURE = "tool_call_failure"
    RETRIEVAL_FAILURE = "retrieval_failure"
    AGENT_STATE_ANOMALY = "agent_state_anomaly"


class MonitoringSignalSeverity(str, Enum):
    """Severity vocabulary used by monitoring assessment and UI summaries."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class MonitoringAssessmentReason(str, Enum):
    """Machine-readable reasons a monitoring signal could not be trusted."""

    UNKNOWN_KIND = "unknown_kind"
    UNKNOWN_SEVERITY = "unknown_severity"
    MISSING_EVIDENCE = "missing_evidence"
    MISSING_THRESHOLD = "missing_threshold"
    UNREADABLE_SCORE = "unreadable_score"
    UNREADABLE_CAPTURE_TIME = "unreadable_capture_time"
    STALE_PROVENANCE = "stale_provenance"


@dataclass(frozen=True, slots=True)
class MonitoringSignal:
    """One production AI monitoring signal captured from runtime evidence."""

    signal_id: str
    kind: MonitoringSignalKind | str
    project_id: str
    run_id: str
    endpoint_id: str
    asset_id: str
    severity: MonitoringSignalSeverity | str
    score: float | int | str | None
    threshold: float | int | str | None
    evidence_refs: tuple[str, ...]
    captured_at_utc: str
    routing_hint: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MonitoringSignal(signal_id={self.signal_id!r}, kind={self.kind!r}, project_id={self.project_id!r})"


@dataclass(frozen=True, slots=True)
class MonitoringSignalAssessment:
    """Assessment of whether a signal is trustworthy and alert-worthy."""

    signal_id: str
    kind: MonitoringSignalKind | None
    severity: MonitoringSignalSeverity | None
    passed: bool
    degraded: bool
    alerting: bool
    score: float | None
    threshold: float | None
    blockers: tuple[MonitoringAssessmentReason, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"MonitoringSignalAssessment(signal_id={self.signal_id!r}, kind={self.kind!r}, severity={self.severity!r})"
        )


def assess_signal(signal: MonitoringSignal, *, now_utc: datetime | None = None) -> MonitoringSignalAssessment:
    """Assess a monitoring signal, failing closed for missing or stale evidence.

    Returns:
        MonitoringSignalAssessment value produced by assess_signal().
    """
    blockers: list[MonitoringAssessmentReason] = []
    kind = _coerce_kind(signal.kind, blockers)
    severity = _coerce_severity(signal.severity, blockers)
    score = _coerce_number(signal.score, MonitoringAssessmentReason.UNREADABLE_SCORE, blockers)
    threshold = _coerce_number(signal.threshold, MonitoringAssessmentReason.MISSING_THRESHOLD, blockers)

    if not tuple(ref for ref in signal.evidence_refs if str(ref).strip()):
        blockers.append(MonitoringAssessmentReason.MISSING_EVIDENCE)
    captured_at = _parse_utc(signal.captured_at_utc)
    if captured_at is None:
        blockers.append(MonitoringAssessmentReason.UNREADABLE_CAPTURE_TIME)
    else:
        current = now_utc if now_utc is not None else datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if captured_at < current - timedelta(hours=24) or captured_at > current + timedelta(minutes=5):
            blockers.append(MonitoringAssessmentReason.STALE_PROVENANCE)

    if blockers:
        return MonitoringSignalAssessment(
            signal_id=signal.signal_id,
            kind=kind,
            severity=severity,
            passed=False,
            degraded=True,
            alerting=False,
            score=score,
            threshold=threshold,
            blockers=tuple(blockers),
        )

    if score is None or threshold is None:
        if score is None:
            blockers.append(MonitoringAssessmentReason.UNREADABLE_SCORE)
        if threshold is None:
            blockers.append(MonitoringAssessmentReason.MISSING_THRESHOLD)
        return MonitoringSignalAssessment(
            signal_id=signal.signal_id,
            kind=kind,
            severity=severity,
            passed=False,
            degraded=True,
            alerting=False,
            score=score,
            threshold=threshold,
            blockers=tuple(blockers),
        )
    alerting = score >= threshold or severity is MonitoringSignalSeverity.CRITICAL
    return MonitoringSignalAssessment(
        signal_id=signal.signal_id,
        kind=kind,
        severity=severity,
        passed=True,
        degraded=False,
        alerting=alerting,
        score=score,
        threshold=threshold,
        blockers=(),
    )


def _coerce_kind(
    value: MonitoringSignalKind | str, blockers: list[MonitoringAssessmentReason]
) -> MonitoringSignalKind | None:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return value if isinstance(value, MonitoringSignalKind) else MonitoringSignalKind(raw_value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        blockers.append(MonitoringAssessmentReason.UNKNOWN_KIND)
        return None


def _coerce_severity(
    value: MonitoringSignalSeverity | str,
    blockers: list[MonitoringAssessmentReason],
) -> MonitoringSignalSeverity | None:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return value if isinstance(value, MonitoringSignalSeverity) else MonitoringSignalSeverity(raw_value)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        blockers.append(MonitoringAssessmentReason.UNKNOWN_SEVERITY)
        return None


def _coerce_number(
    value: Any,
    reason: MonitoringAssessmentReason,
    blockers: list[MonitoringAssessmentReason],
) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        blockers.append(reason)
        return None
    if not isfinite(number):
        blockers.append(reason)
        return None
    return number


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "MonitoringAssessmentReason",
    "MonitoringSignal",
    "MonitoringSignalAssessment",
    "MonitoringSignalKind",
    "MonitoringSignalSeverity",
    "assess_signal",
]
