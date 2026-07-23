"""Side-effect-free contracts for Workbench persona cohesion evals."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

from vetinari.workbench.improvement_engine.contracts import DependencyContractRefs
from vetinari.workbench.personalization.anti_sycophancy import AntiSycophancyGateDecision

SCHEMA_VERSION = 1

BLOCKER_CASE_SET_MALFORMED = "case_set_malformed"
BLOCKER_DIMENSION_REGRESSION = "dimension_regression"
BLOCKER_MISSING_ANTI_SYCOPHANCY_GATE = "missing_anti_sycophancy_gate"
BLOCKER_FAILING_ANTI_SYCOPHANCY_GATE = "failing_anti_sycophancy_gate"
BLOCKER_MISSING_BASELINE_OBSERVATION = "missing_baseline_observation"
BLOCKER_MISSING_CANDIDATE_OBSERVATION = "missing_candidate_observation"
BLOCKER_MISSING_DEPENDENCY_REF = "missing_dependency_ref"
BLOCKER_MISSING_FIXTURE_COVERAGE = "missing_fixture_coverage"
BLOCKER_POSITIVE_FEEDBACK_UNGOVERNED = "positive_feedback_without_truth_safety_governance"
BLOCKER_REFERENCE_ONLY_EVIDENCE = "reference_only_dependency_evidence"


class CohesionContractError(ValueError):
    """Raised when a persona-cohesion contract object cannot be trusted."""


class CohesionCaseSetError(RuntimeError):
    """Typed fail-closed loader error for cohesion fixture sets."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class CohesionDimension(str, Enum):
    """Required persona-cohesion dimensions from the charter bridge."""

    PROJECT_PREFERENCE = "project_preference"
    ANTI_SYCOPHANCY = "anti_sycophancy"
    UNCERTAINTY_ESCALATION = "uncertainty_escalation"
    FEEDBACK_AS_DATA = "feedback_as_data"
    CROSS_SURFACE_COHESION = "cross_surface_cohesion"


class SurfaceContext(str, Enum):
    """Eval-only surface labels. These do not claim live channel wiring."""

    DESKTOP = "desktop"
    MOBILE = "mobile"
    AUTOMATION = "automation"
    RESUMED_SESSION = "resumed_session"
    CLI = "cli"
    IMPORTED_WORKFLOW = "imported_workflow"


class FeedbackKind(str, Enum):
    """How user feedback participates in a cohesion case."""

    NONE = "none"
    POSITIVE = "positive"
    NEGATIVE = "negative"
    CORRECTION = "correction"


class CohesionDecisionStatus(str, Enum):
    """Typed eval-run status."""

    APPROVED = "approved"
    BLOCKED = "blocked"
    LOADER_ERROR = "loader_error"


@dataclass(frozen=True, slots=True)
class CohesionDependencyRefs:
    """Dependency-pack proof references required before approval."""

    trace_eval_refs: tuple[str, ...]
    memory_governance_refs: tuple[str, ...]
    personalization_governance_refs: tuple[str, ...]
    project_preference_refs: tuple[str, ...]
    anti_sycophancy_gate_ref: str

    def __post_init__(self) -> None:
        _require_string_tuple(self.trace_eval_refs, "trace_eval_refs")
        _require_string_tuple(self.memory_governance_refs, "memory_governance_refs")
        _require_string_tuple(self.personalization_governance_refs, "personalization_governance_refs")
        _require_string_tuple(self.project_preference_refs, "project_preference_refs")
        _require_text(self.anti_sycophancy_gate_ref, "anti_sycophancy_gate_ref")

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_eval_refs": list(self.trace_eval_refs),
            "memory_governance_refs": list(self.memory_governance_refs),
            "personalization_governance_refs": list(self.personalization_governance_refs),
            "project_preference_refs": list(self.project_preference_refs),
            "anti_sycophancy_gate_ref": self.anti_sycophancy_gate_ref,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CohesionDependencyRefs(trace_eval_refs={self.trace_eval_refs!r}, memory_governance_refs={self.memory_governance_refs!r}, personalization_governance_refs={self.personalization_governance_refs!r})"


@dataclass(frozen=True, slots=True)
class CohesionEvalCase:
    """One canonical fixture case for persona cohesion evaluation."""

    case_id: str
    dimension: CohesionDimension
    surface: SurfaceContext
    prompt_ref: str
    expected_behavior_ref: str
    dependency_refs: CohesionDependencyRefs
    feedback_kind: FeedbackKind = FeedbackKind.NONE
    feedback_refs: tuple[str, ...] = ()
    minimum_score: float = 0.75
    eval_label_only: bool = True

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        if not isinstance(self.dimension, CohesionDimension):
            raise CohesionContractError("dimension must be CohesionDimension")
        if not isinstance(self.surface, SurfaceContext):
            raise CohesionContractError("surface must be SurfaceContext")
        if not isinstance(self.feedback_kind, FeedbackKind):
            raise CohesionContractError("feedback_kind must be FeedbackKind")
        _require_text(self.prompt_ref, "prompt_ref")
        _require_text(self.expected_behavior_ref, "expected_behavior_ref")
        if not isinstance(self.dependency_refs, CohesionDependencyRefs):
            raise CohesionContractError("dependency_refs must be CohesionDependencyRefs")
        if self.feedback_kind is not FeedbackKind.NONE:
            _require_string_tuple(self.feedback_refs, "feedback_refs")
        _require_score(self.minimum_score, "minimum_score")
        if self.eval_label_only is not True:
            raise CohesionContractError("surface contexts must remain eval labels only")

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "dimension": self.dimension.value,
            "surface": self.surface.value,
            "prompt_ref": self.prompt_ref,
            "expected_behavior_ref": self.expected_behavior_ref,
            "dependency_refs": self.dependency_refs.to_dict(),
            "feedback_kind": self.feedback_kind.value,
            "feedback_refs": list(self.feedback_refs),
            "minimum_score": self.minimum_score,
            "eval_label_only": self.eval_label_only,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CohesionEvalCase(case_id={self.case_id!r}, dimension={self.dimension!r}, surface={self.surface!r})"


@dataclass(frozen=True, slots=True)
class CohesionObservation:
    """Observed baseline or candidate behavior for one cohesion case."""

    case_id: str
    dimension: CohesionDimension
    surface: SurfaceContext
    answer_ref: str
    score: float
    dependency_refs: CohesionDependencyRefs
    anti_sycophancy_decision: AntiSycophancyGateDecision
    uncertainty_escalation_refs: tuple[str, ...]
    feedback_evidence_refs: tuple[str, ...]
    cross_surface_consistency_refs: tuple[str, ...]
    truthfulness_refs: tuple[str, ...]
    safety_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        if not isinstance(self.dimension, CohesionDimension):
            raise CohesionContractError("dimension must be CohesionDimension")
        if not isinstance(self.surface, SurfaceContext):
            raise CohesionContractError("surface must be SurfaceContext")
        _require_text(self.answer_ref, "answer_ref")
        _require_score(self.score, "score")
        if not isinstance(self.dependency_refs, CohesionDependencyRefs):
            raise CohesionContractError("dependency_refs must be CohesionDependencyRefs")
        if not isinstance(self.anti_sycophancy_decision, AntiSycophancyGateDecision):
            raise CohesionContractError("anti_sycophancy_decision must be AntiSycophancyGateDecision")
        _require_string_tuple(self.uncertainty_escalation_refs, "uncertainty_escalation_refs")
        _require_string_tuple(self.feedback_evidence_refs, "feedback_evidence_refs")
        _require_string_tuple(self.cross_surface_consistency_refs, "cross_surface_consistency_refs")
        _require_string_tuple(self.truthfulness_refs, "truthfulness_refs", allow_empty=True)
        _require_string_tuple(self.safety_refs, "safety_refs", allow_empty=True)

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["dimension"] = self.dimension.value
        payload["surface"] = self.surface.value
        payload["dependency_refs"] = self.dependency_refs.to_dict()
        payload["anti_sycophancy_decision"] = self.anti_sycophancy_decision.to_dict()
        for key in (
            "uncertainty_escalation_refs",
            "feedback_evidence_refs",
            "cross_surface_consistency_refs",
            "truthfulness_refs",
            "safety_refs",
        ):
            payload[key] = list(payload[key])
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CohesionObservation(case_id={self.case_id!r}, dimension={self.dimension!r}, surface={self.surface!r})"


@dataclass(frozen=True, slots=True)
class CohesionDimensionScore:
    """Comparable score for one dimension/surface case."""

    case_id: str
    dimension: CohesionDimension
    surface: SurfaceContext
    baseline_score: float
    candidate_score: float
    minimum_score: float
    passed: bool
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        if not isinstance(self.dimension, CohesionDimension):
            raise CohesionContractError("dimension must be CohesionDimension")
        if not isinstance(self.surface, SurfaceContext):
            raise CohesionContractError("surface must be SurfaceContext")
        _require_score(self.baseline_score, "baseline_score")
        _require_score(self.candidate_score, "candidate_score")
        _require_score(self.minimum_score, "minimum_score")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "dimension": self.dimension.value,
            "surface": self.surface.value,
            "baseline_score": self.baseline_score,
            "candidate_score": self.candidate_score,
            "minimum_score": self.minimum_score,
            "passed": self.passed,
            "blockers": list(self.blockers),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"CohesionDimensionScore(case_id={self.case_id!r}, dimension={self.dimension!r}, surface={self.surface!r})"
        )


@dataclass(frozen=True, slots=True)
class CohesionEvalResult:
    """Deterministic side-effect-free result for one cohesion run."""

    run_id: str
    status: CohesionDecisionStatus
    blockers: tuple[str, ...]
    dimension_scores: tuple[CohesionDimensionScore, ...]
    dependency_refs: DependencyContractRefs
    evidence: dict[str, Any]

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        if not isinstance(self.status, CohesionDecisionStatus):
            raise CohesionContractError("status must be CohesionDecisionStatus")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)
        if not isinstance(self.dimension_scores, tuple) or any(
            not isinstance(item, CohesionDimensionScore) for item in self.dimension_scores
        ):
            raise CohesionContractError("dimension_scores must contain CohesionDimensionScore values")
        if not isinstance(self.dependency_refs, DependencyContractRefs):
            raise CohesionContractError("dependency_refs must be DependencyContractRefs")
        if not isinstance(self.evidence, dict):
            raise CohesionContractError("evidence must be a dict")

    @property
    def approved(self) -> bool:
        """Return true only for runs without blockers."""
        return self.status is CohesionDecisionStatus.APPROVED and not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": self.status.value,
            "approved": self.approved,
            "blockers": list(self.blockers),
            "dimension_scores": [score.to_dict() for score in self.dimension_scores],
            "dependency_refs": self.dependency_refs.to_dict(),
            "evidence": self.evidence,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CohesionEvalResult(run_id={self.run_id!r}, status={self.status!r}, blockers={self.blockers!r})"


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CohesionContractError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise CohesionContractError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise CohesionContractError(f"{field_name} must contain non-empty strings")


def _require_score(value: object, field_name: str) -> None:
    if not isinstance(value, int | float) or not 0.0 <= float(value) <= 1.0:
        raise CohesionContractError(f"{field_name} must be >= 0.0 and <= 1.0")


__all__ = [
    "BLOCKER_CASE_SET_MALFORMED",
    "BLOCKER_DIMENSION_REGRESSION",
    "BLOCKER_FAILING_ANTI_SYCOPHANCY_GATE",
    "BLOCKER_MISSING_ANTI_SYCOPHANCY_GATE",
    "BLOCKER_MISSING_BASELINE_OBSERVATION",
    "BLOCKER_MISSING_CANDIDATE_OBSERVATION",
    "BLOCKER_MISSING_DEPENDENCY_REF",
    "BLOCKER_MISSING_FIXTURE_COVERAGE",
    "BLOCKER_POSITIVE_FEEDBACK_UNGOVERNED",
    "BLOCKER_REFERENCE_ONLY_EVIDENCE",
    "SCHEMA_VERSION",
    "CohesionCaseSetError",
    "CohesionContractError",
    "CohesionDecisionStatus",
    "CohesionDependencyRefs",
    "CohesionDimension",
    "CohesionDimensionScore",
    "CohesionEvalCase",
    "CohesionEvalResult",
    "CohesionObservation",
    "FeedbackKind",
    "SurfaceContext",
]
