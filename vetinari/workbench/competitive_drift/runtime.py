"""Runtime contracts for competitive drift evidence and proposals."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.workbench.competitive_drift.errors import CompetitiveDriftError

BLOCKER_BASELINE_NOT_ALLOWED = "baseline_not_allowed"
BLOCKER_SOURCE_STALE = "source_stale"
BLOCKER_NO_LOSS = "no_capability_gap"
SCHEMA_VERSION = "1.0"


class DriftProposalKind(str, Enum):
    """Destinations for capability-gap proposals."""

    MODEL_ACQUISITION = "model_acquisition"
    PROMPT_OR_METHOD_CHANGE = "prompt_or_method_change"
    TRAINING_RECIPE = "training_recipe"
    ROUTE_POLICY = "route_policy"
    DOCUMENTED_LIMITATION = "documented_limitation"


@dataclass(frozen=True, slots=True)
class SourceFreshness:
    """External fact source with explicit freshness metadata."""

    source_id: str
    title: str
    source_url: str
    observed_at: str
    stale_after: str
    provenance_ref: str

    def __post_init__(self) -> None:
        for field_name in ("source_id", "title", "source_url", "observed_at", "stale_after", "provenance_ref"):
            _require_text(getattr(self, field_name), field_name)
        if _parse_iso(self.stale_after, "stale_after") <= _parse_iso(self.observed_at, "observed_at"):
            raise CompetitiveDriftError("stale_after must be after observed_at")

    def assert_fresh_for(self, run_date: str) -> None:
        """Fail closed when a source is stale for the run date.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        _require_text(run_date, "run_date")
        if _parse_iso(run_date, "run_date") > _parse_iso(self.stale_after, "stale_after"):
            raise CompetitiveDriftError(BLOCKER_SOURCE_STALE)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SourceFreshness(source_id={self.source_id!r}, title={self.title!r})"


@dataclass(frozen=True, slots=True)
class CompetitiveBaseline:
    """Allowed frontier or competitor baseline for one task suite."""

    baseline_id: str
    name: str
    allowed: bool
    source: SourceFreshness
    authority_ref: str
    safety_ref: str
    budget_ref: str

    def __post_init__(self) -> None:
        for field_name in ("baseline_id", "name", "authority_ref", "safety_ref", "budget_ref"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.source, SourceFreshness):
            raise CompetitiveDriftError("source must be SourceFreshness")

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["source"] = self.source.to_dict()
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CompetitiveBaseline(baseline_id={self.baseline_id!r}, name={self.name!r}, allowed={self.allowed!r})"


@dataclass(frozen=True, slots=True)
class TaskSuiteResult:
    """Comparable score for local/default route or baseline route."""

    result_id: str
    task_suite_id: str
    route_ref: str
    score: float
    evidence_ref: str

    def __post_init__(self) -> None:
        for field_name in ("result_id", "task_suite_id", "route_ref", "evidence_ref"):
            _require_text(getattr(self, field_name), field_name)
        if self.score < 0 or self.score > 1:
            raise CompetitiveDriftError("score must be between 0 and 1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TaskSuiteResult(result_id={self.result_id!r}, task_suite_id={self.task_suite_id!r}, route_ref={self.route_ref!r})"


@dataclass(frozen=True, slots=True)
class CompetitiveGapEvidence:
    """Evidence that a local/default route lost to an allowed baseline."""

    gap_id: str
    baseline: CompetitiveBaseline
    local_result: TaskSuiteResult
    baseline_result: TaskSuiteResult
    delta: float
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.gap_id, "gap_id")
        if not isinstance(self.baseline, CompetitiveBaseline):
            raise CompetitiveDriftError("baseline must be CompetitiveBaseline")
        if not isinstance(self.local_result, TaskSuiteResult) or not isinstance(self.baseline_result, TaskSuiteResult):
            raise CompetitiveDriftError("results must be TaskSuiteResult")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)
        if self.delta < 0:
            raise CompetitiveDriftError("delta must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "baseline": self.baseline.to_dict(),
            "local_result": self.local_result.to_dict(),
            "baseline_result": self.baseline_result.to_dict(),
            "delta": self.delta,
            "blockers": list(self.blockers),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CompetitiveGapEvidence(gap_id={self.gap_id!r}, baseline={self.baseline!r}, local_result={self.local_result!r})"


@dataclass(frozen=True, slots=True)
class DriftProposal:
    """Evidence-backed proposal or honest limitation record."""

    proposal_id: str
    kind: DriftProposalKind
    gap: CompetitiveGapEvidence
    change_default: bool
    support_boundary: bool
    recommendation_ref: str
    approval_ref: str

    def __post_init__(self) -> None:
        _require_text(self.proposal_id, "proposal_id")
        if not isinstance(self.kind, DriftProposalKind):
            raise CompetitiveDriftError("kind must be DriftProposalKind")
        if not isinstance(self.gap, CompetitiveGapEvidence):
            raise CompetitiveDriftError("gap must be CompetitiveGapEvidence")
        for field_name in ("recommendation_ref", "approval_ref"):
            _require_text(getattr(self, field_name), field_name)
        if self.change_default and self.support_boundary:
            raise CompetitiveDriftError("proposal cannot both change defaults and document a support boundary")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "proposal_id": self.proposal_id,
            "kind": self.kind.value,
            "gap": self.gap.to_dict(),
            "change_default": self.change_default,
            "support_boundary": self.support_boundary,
            "recommendation_ref": self.recommendation_ref,
            "approval_ref": self.approval_ref,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DriftProposal(proposal_id={self.proposal_id!r}, kind={self.kind!r}, gap={self.gap!r})"


def record_competitive_gap(
    *,
    baseline: CompetitiveBaseline,
    local_result: TaskSuiteResult,
    baseline_result: TaskSuiteResult,
    run_date: str,
    minimum_delta: float = 0.05,
) -> CompetitiveGapEvidence:
    """Record a capability gap only when the baseline is allowed and source-fresh.

    Returns:
        Outcome produced by record_competitive_gap().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not baseline.allowed:
        raise CompetitiveDriftError(BLOCKER_BASELINE_NOT_ALLOWED)
    baseline.source.assert_fresh_for(run_date)
    if local_result.task_suite_id != baseline_result.task_suite_id:
        raise CompetitiveDriftError("task suite ids must match")
    delta = baseline_result.score - local_result.score
    if delta < minimum_delta:
        raise CompetitiveDriftError(BLOCKER_NO_LOSS)
    return CompetitiveGapEvidence(
        gap_id=f"gap:{baseline.baseline_id}:{local_result.task_suite_id}",
        baseline=baseline,
        local_result=local_result,
        baseline_result=baseline_result,
        delta=round(delta, 6),
        blockers=(),
    )


def create_drift_proposal(
    gap: CompetitiveGapEvidence,
    *,
    kind: DriftProposalKind | str,
    recommendation_ref: str,
    approval_ref: str,
) -> DriftProposal:
    """Route a gap into a product, model, training, policy, or limitation proposal.

    Returns:
        Newly constructed drift proposal value.
    """
    proposal_kind = DriftProposalKind(kind)
    return DriftProposal(
        proposal_id=f"proposal:{gap.gap_id}:{proposal_kind.value}",
        kind=proposal_kind,
        gap=gap,
        change_default=proposal_kind
        in {
            DriftProposalKind.MODEL_ACQUISITION,
            DriftProposalKind.PROMPT_OR_METHOD_CHANGE,
            DriftProposalKind.TRAINING_RECIPE,
            DriftProposalKind.ROUTE_POLICY,
        },
        support_boundary=proposal_kind is DriftProposalKind.DOCUMENTED_LIMITATION,
        recommendation_ref=recommendation_ref,
        approval_ref=approval_ref,
    )


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CompetitiveDriftError(f"{field_name} must be non-empty")


def _parse_iso(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CompetitiveDriftError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise CompetitiveDriftError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise CompetitiveDriftError(f"{field_name} must contain non-empty strings")


__all__ = [
    "BLOCKER_BASELINE_NOT_ALLOWED",
    "BLOCKER_NO_LOSS",
    "BLOCKER_SOURCE_STALE",
    "CompetitiveBaseline",
    "CompetitiveDriftError",
    "CompetitiveGapEvidence",
    "DriftProposal",
    "DriftProposalKind",
    "SourceFreshness",
    "TaskSuiteResult",
    "create_drift_proposal",
    "record_competitive_gap",
]
