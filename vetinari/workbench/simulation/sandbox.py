"""Counterfactual replay sandbox for Workbench self-improvement gates.

The sandbox is deliberately pure: callers provide historical trace/eval cases
and a candidate metric projection, and the sandbox returns a report. It does not
write state, mutate defaults, execute tools, or promote proposals.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from math import isfinite
from typing import Any
from uuid import uuid4


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise SimulationSandboxError(f"{field_name} must be non-empty")


def _require_non_empty_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not values:
        raise SimulationSandboxError(f"{field_name} must be non-empty")
    for value in values:
        _require_text(value, field_name)


def _coerce_metric(value: float | int, field_name: str, *, minimum: float | None = None) -> float:
    metric = float(value)
    if not isfinite(metric):
        raise SimulationSandboxError(f"{field_name} must be finite")
    if minimum is not None and metric < minimum:
        raise SimulationSandboxError(f"{field_name} must be >= {minimum}")
    return metric


class SimulationSandboxError(ValueError):
    """Raised when a counterfactual simulation cannot be trusted."""


class ChangeSurface(str, Enum):
    """Default surfaces whose changes can be replayed before promotion."""

    PROMPT = "prompt"
    MODEL = "model"
    ROUTE = "route"
    POLICY = "policy"
    TOOL = "tool"
    DATASET = "dataset"
    RECIPE = "recipe"
    AUTOMATION = "automation"
    RUNTIME = "runtime"


class SimulationImpact(str, Enum):
    """Promotion impact class used by governance gates."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class MetricVector:
    """Comparable quality, cost, latency, safety, retrieval, review, and compatibility metrics."""

    quality_score: float
    cost_usd: float
    latency_ms: float
    safety_incidents: int
    retrieval_score: float
    human_review_minutes: float
    compatibility_score: float

    def __post_init__(self) -> None:
        _coerce_metric(self.quality_score, "quality_score", minimum=0)
        _coerce_metric(self.cost_usd, "cost_usd", minimum=0)
        _coerce_metric(self.latency_ms, "latency_ms", minimum=0)
        _coerce_metric(self.safety_incidents, "safety_incidents", minimum=0)
        _coerce_metric(self.retrieval_score, "retrieval_score", minimum=0)
        _coerce_metric(self.human_review_minutes, "human_review_minutes", minimum=0)
        _coerce_metric(self.compatibility_score, "compatibility_score", minimum=0)

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MetricVector(quality_score={self.quality_score!r}, cost_usd={self.cost_usd!r}, latency_ms={self.latency_ms!r})"


@dataclass(frozen=True, slots=True)
class SimulationMetricDelta:
    """Candidate-minus-baseline aggregate deltas."""

    quality_score: float
    cost_usd: float
    latency_ms: float
    safety_incidents: int
    retrieval_score: float
    human_review_minutes: float
    compatibility_score: float

    def to_dict(self) -> dict[str, float | int]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SimulationMetricDelta(quality_score={self.quality_score!r}, cost_usd={self.cost_usd!r}, latency_ms={self.latency_ms!r})"


MetricDelta = SimulationMetricDelta


@dataclass(frozen=True, slots=True)
class SimulationEvidence:
    """Evidence and authority proof required before a report can be trusted."""

    evidence_refs: tuple[str, ...]
    provenance_ref: str
    authority_ref: str
    safety_review_ref: str
    confidence: float

    def __post_init__(self) -> None:
        _require_non_empty_tuple(self.evidence_refs, "evidence_refs")
        _require_text(self.provenance_ref, "provenance_ref")
        _require_text(self.authority_ref, "authority_ref")
        _require_text(self.safety_review_ref, "safety_review_ref")
        confidence = _coerce_metric(self.confidence, "confidence", minimum=0)
        if confidence > 1:
            raise SimulationSandboxError("confidence must be <= 1")

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SimulationEvidence(evidence_refs={self.evidence_refs!r}, provenance_ref={self.provenance_ref!r}, authority_ref={self.authority_ref!r})"


@dataclass(frozen=True, slots=True)
class HistoricalReplayCase:
    """One historical trace/eval case used for counterfactual replay."""

    case_id: str
    trace_ref: str
    eval_ref: str
    baseline_metrics: MetricVector
    tags: tuple[str, ...]
    rollback_ref: str

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_text(self.trace_ref, "trace_ref")
        _require_text(self.eval_ref, "eval_ref")
        _require_non_empty_tuple(self.tags, "tags")
        _require_text(self.rollback_ref, "rollback_ref")

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["baseline_metrics"] = self.baseline_metrics.to_dict()
        payload["tags"] = list(self.tags)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"HistoricalReplayCase(case_id={self.case_id!r}, trace_ref={self.trace_ref!r}, eval_ref={self.eval_ref!r})"
        )


@dataclass(frozen=True, slots=True)
class CounterfactualChange:
    """Candidate change under replay."""

    change_id: str
    surface: ChangeSurface
    baseline_ref: str
    candidate_ref: str
    description: str
    impact: SimulationImpact
    rollback_ref: str

    def __post_init__(self) -> None:
        _require_text(self.change_id, "change_id")
        _require_text(self.baseline_ref, "baseline_ref")
        _require_text(self.candidate_ref, "candidate_ref")
        _require_text(self.description, "description")
        _require_text(self.rollback_ref, "rollback_ref")

    def to_dict(self) -> dict[str, str]:
        return {
            "change_id": self.change_id,
            "surface": self.surface.value,
            "baseline_ref": self.baseline_ref,
            "candidate_ref": self.candidate_ref,
            "description": self.description,
            "impact": self.impact.value,
            "rollback_ref": self.rollback_ref,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CounterfactualChange(change_id={self.change_id!r}, surface={self.surface!r}, baseline_ref={self.baseline_ref!r})"


@dataclass(frozen=True, slots=True)
class CaseReplayResult:
    """Per-case replay result."""

    case_id: str
    baseline_metrics: MetricVector
    candidate_metrics: MetricVector
    delta: MetricDelta
    blockers: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return self.blockers == ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "baseline_metrics": self.baseline_metrics.to_dict(),
            "candidate_metrics": self.candidate_metrics.to_dict(),
            "delta": self.delta.to_dict(),
            "blockers": list(self.blockers),
            "passed": self.passed,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CaseReplayResult(case_id={self.case_id!r}, baseline_metrics={self.baseline_metrics!r}, candidate_metrics={self.candidate_metrics!r})"


@dataclass(frozen=True, slots=True)
class CounterfactualSimulationReport:
    """Promotion-gate report emitted by the sandbox."""

    simulation_id: str
    change: CounterfactualChange
    evidence: SimulationEvidence | None
    case_results: tuple[CaseReplayResult, ...]
    aggregate_delta: MetricDelta
    approved_for_promotion: bool
    blockers: tuple[str, ...]
    rollback_refs: tuple[str, ...]
    created_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "record_type": "counterfactual_simulation_report",
            "simulation_id": self.simulation_id,
            "change": self.change.to_dict(),
            "evidence": None if self.evidence is None else self.evidence.to_dict(),
            "case_results": [result.to_dict() for result in self.case_results],
            "aggregate_delta": self.aggregate_delta.to_dict(),
            "approved_for_promotion": self.approved_for_promotion,
            "blockers": list(self.blockers),
            "rollback_refs": list(self.rollback_refs),
            "created_at_utc": self.created_at_utc,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CounterfactualSimulationReport(simulation_id={self.simulation_id!r}, change={self.change!r}, evidence={self.evidence!r})"


MetricProjector = Callable[[HistoricalReplayCase, CounterfactualChange], MetricVector]


class CounterfactualSimulationSandbox:
    """Replay candidate changes against historical cases without mutating defaults."""

    def __init__(
        self,
        cases: Iterable[HistoricalReplayCase],
        *,
        min_confidence: float = 0.75,
        min_quality_delta: float = 0.0,
        max_cost_delta_usd: float = 0.25,
        max_latency_delta_ms: float = 250.0,
        max_review_delta_minutes: float = 5.0,
        min_compatibility_delta: float = -0.05,
    ) -> None:
        self._cases = tuple(cases)
        if not self._cases:
            raise SimulationSandboxError("historical replay cases must be non-empty")
        self._min_confidence = _coerce_metric(min_confidence, "min_confidence", minimum=0)
        if self._min_confidence > 1:
            raise SimulationSandboxError("min_confidence must be <= 1")
        self._min_quality_delta = float(min_quality_delta)
        self._max_cost_delta_usd = _coerce_metric(max_cost_delta_usd, "max_cost_delta_usd", minimum=0)
        self._max_latency_delta_ms = _coerce_metric(max_latency_delta_ms, "max_latency_delta_ms", minimum=0)
        self._max_review_delta_minutes = _coerce_metric(
            max_review_delta_minutes,
            "max_review_delta_minutes",
            minimum=0,
        )
        self._min_compatibility_delta = float(min_compatibility_delta)

    def replay(
        self,
        change: CounterfactualChange,
        projector: MetricProjector,
        *,
        evidence: SimulationEvidence | None,
    ) -> CounterfactualSimulationReport:
        """Evaluate one candidate against every historical case.

        Args:
            change: Change value consumed by replay().
            projector: Projector value consumed by replay().
            evidence: Evidence value consumed by replay().

        Returns:
            CounterfactualSimulationReport value produced by replay().
        """
        case_results = tuple(self._replay_case(case, change, projector) for case in self._cases)
        aggregate = _average_delta(result.delta for result in case_results)
        blockers = list(_evidence_blockers(evidence, min_confidence=self._min_confidence))
        blockers.extend(
            self._aggregate_blockers(
                change,
                aggregate,
                case_results,
            )
        )
        rollback_refs = tuple(dict.fromkeys([change.rollback_ref, *(case.rollback_ref for case in self._cases)]))
        approved = not blockers
        return CounterfactualSimulationReport(
            simulation_id=f"sim-run-{uuid4().hex[:16]}",
            change=change,
            evidence=evidence,
            case_results=case_results,
            aggregate_delta=aggregate,
            approved_for_promotion=approved,
            blockers=tuple(dict.fromkeys(blockers)),
            rollback_refs=rollback_refs,
            created_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    def _replay_case(
        self,
        case: HistoricalReplayCase,
        change: CounterfactualChange,
        projector: MetricProjector,
    ) -> CaseReplayResult:
        candidate = projector(case, change)
        delta = _delta(case.baseline_metrics, candidate)
        blockers: list[str] = []
        if delta.safety_incidents > 0:
            blockers.append("safety_regression")
        if delta.retrieval_score < 0:
            blockers.append("retrieval_regression")
        if delta.compatibility_score < self._min_compatibility_delta:
            blockers.append("compatibility_regression")
        return CaseReplayResult(
            case_id=case.case_id,
            baseline_metrics=case.baseline_metrics,
            candidate_metrics=candidate,
            delta=delta,
            blockers=tuple(blockers),
        )

    def _aggregate_blockers(
        self,
        change: CounterfactualChange,
        aggregate: MetricDelta,
        case_results: tuple[CaseReplayResult, ...],
    ) -> tuple[str, ...]:
        blockers: list[str] = []
        if any(not result.passed for result in case_results):
            blockers.append("case_level_regression")
        if aggregate.quality_score < self._min_quality_delta:
            blockers.append("quality_regression")
        if aggregate.cost_usd > self._max_cost_delta_usd:
            blockers.append("cost_regression")
        if aggregate.latency_ms > self._max_latency_delta_ms:
            blockers.append("latency_regression")
        if aggregate.human_review_minutes > self._max_review_delta_minutes:
            blockers.append("human_review_burden_regression")
        if aggregate.safety_incidents > 0:
            blockers.append("safety_regression")
        if aggregate.compatibility_score < self._min_compatibility_delta:
            blockers.append("compatibility_regression")
        if change.impact in {SimulationImpact.MEDIUM, SimulationImpact.HIGH} and not change.rollback_ref.strip():
            blockers.append("rollback_missing")
        return tuple(blockers)


def medium_or_high_impact_requires_simulation(
    *,
    impact: SimulationImpact,
    report: CounterfactualSimulationReport | None,
) -> tuple[bool, tuple[str, ...]]:
    """Return a fail-closed governance gate for promotion callers.

    Returns:
        tuple[bool, tuple[str, ...]] value produced by medium_or_high_impact_requires_simulation().
    """
    if impact is SimulationImpact.LOW:
        return True, ()
    if report is None:
        return False, ("simulation_delta_required",)
    if not isinstance(report, CounterfactualSimulationReport):
        return False, ("simulation_report_invalid",)
    if report.change.impact is not impact:
        return False, ("simulation_impact_mismatch",)
    if not report.approved_for_promotion:
        return False, ("simulation_not_approved", *report.blockers)
    return True, ()


def summarize_report_for_governance(report: CounterfactualSimulationReport) -> Mapping[str, Any]:
    """Expose the report fields promotion/governance code needs without mutable state."""
    return {
        "simulation_id": report.simulation_id,
        "change_id": report.change.change_id,
        "surface": report.change.surface.value,
        "impact": report.change.impact.value,
        "approved_for_promotion": report.approved_for_promotion,
        "blockers": report.blockers,
        "aggregate_delta": report.aggregate_delta.to_dict(),
        "rollback_refs": report.rollback_refs,
        "case_count": len(report.case_results),
    }


def _delta(baseline: MetricVector, candidate: MetricVector) -> MetricDelta:
    return MetricDelta(
        quality_score=candidate.quality_score - baseline.quality_score,
        cost_usd=candidate.cost_usd - baseline.cost_usd,
        latency_ms=candidate.latency_ms - baseline.latency_ms,
        safety_incidents=candidate.safety_incidents - baseline.safety_incidents,
        retrieval_score=candidate.retrieval_score - baseline.retrieval_score,
        human_review_minutes=candidate.human_review_minutes - baseline.human_review_minutes,
        compatibility_score=candidate.compatibility_score - baseline.compatibility_score,
    )


def _average_delta(deltas: Iterable[MetricDelta]) -> MetricDelta:
    values = tuple(deltas)
    if not values:
        raise SimulationSandboxError("cannot aggregate empty simulation deltas")
    count = len(values)
    return MetricDelta(
        quality_score=sum(delta.quality_score for delta in values) / count,
        cost_usd=sum(delta.cost_usd for delta in values) / count,
        latency_ms=sum(delta.latency_ms for delta in values) / count,
        safety_incidents=sum(delta.safety_incidents for delta in values),
        retrieval_score=sum(delta.retrieval_score for delta in values) / count,
        human_review_minutes=sum(delta.human_review_minutes for delta in values) / count,
        compatibility_score=sum(delta.compatibility_score for delta in values) / count,
    )


def _evidence_blockers(evidence: SimulationEvidence | None, *, min_confidence: float) -> tuple[str, ...]:
    if evidence is None:
        return (
            "missing_simulation_evidence",
            "missing_provenance",
            "missing_authority",
            "missing_safety_review",
            "confidence_below_threshold",
        )
    blockers: list[str] = []
    if evidence.confidence < min_confidence:
        blockers.append("confidence_below_threshold")
    return tuple(blockers)


__all__ = [
    "CaseReplayResult",
    "ChangeSurface",
    "CounterfactualChange",
    "CounterfactualSimulationReport",
    "CounterfactualSimulationSandbox",
    "HistoricalReplayCase",
    "MetricDelta",
    "MetricProjector",
    "MetricVector",
    "SimulationEvidence",
    "SimulationImpact",
    "SimulationSandboxError",
    "medium_or_high_impact_requires_simulation",
    "summarize_report_for_governance",
]
