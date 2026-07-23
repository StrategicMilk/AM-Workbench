"""Deterministic Workbench persona cohesion evaluator."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vetinari.workbench.improvement_engine.contracts import (
    DependencyContractRefs,
    EvidenceRef,
    EvidenceRole,
    ImprovementCandidate,
    ImprovementSignal,
    ImprovementSignalKind,
    PromotionLifecycle,
    PromotionTarget,
)
from vetinari.workbench.persona_cohesion.contracts import (
    BLOCKER_CASE_SET_MALFORMED,
    BLOCKER_DIMENSION_REGRESSION,
    BLOCKER_FAILING_ANTI_SYCOPHANCY_GATE,
    BLOCKER_MISSING_ANTI_SYCOPHANCY_GATE,
    BLOCKER_MISSING_BASELINE_OBSERVATION,
    BLOCKER_MISSING_CANDIDATE_OBSERVATION,
    BLOCKER_MISSING_DEPENDENCY_REF,
    BLOCKER_MISSING_FIXTURE_COVERAGE,
    BLOCKER_POSITIVE_FEEDBACK_UNGOVERNED,
    BLOCKER_REFERENCE_ONLY_EVIDENCE,
    SCHEMA_VERSION,
    CohesionCaseSetError,
    CohesionDecisionStatus,
    CohesionDependencyRefs,
    CohesionDimension,
    CohesionDimensionScore,
    CohesionEvalCase,
    CohesionEvalResult,
    CohesionObservation,
    FeedbackKind,
    SurfaceContext,
)

DEFAULT_CASE_SET_PATH = Path(__file__).parent / "fixtures" / "cohesion_cases.json"


def load_cohesion_eval_cases(path: str | Path = DEFAULT_CASE_SET_PATH) -> tuple[CohesionEvalCase, ...]:
    """Load checked-in cohesion cases on explicit call only.

    Returns:
        Resolved cohesion eval cases value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    case_path = Path(path)
    try:
        payload = json.loads(case_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CohesionCaseSetError("cohesion case set is missing", reason="missing") from exc
    except OSError as exc:
        raise CohesionCaseSetError("cohesion case set is unreadable", reason="unreadable") from exc
    except json.JSONDecodeError as exc:
        raise CohesionCaseSetError("cohesion case set is corrupt", reason="corrupt") from exc

    try:
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise CohesionCaseSetError("cohesion case set has wrong version", reason="wrong_version")
        raw_cases = payload["cases"]
        if not isinstance(raw_cases, list) or not raw_cases:
            raise CohesionCaseSetError("cohesion case set is empty", reason="empty")
        cases = tuple(_case_from_mapping(raw_case) for raw_case in raw_cases)
    except CohesionCaseSetError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CohesionCaseSetError("cohesion case set is malformed", reason="malformed") from exc

    coverage_blockers = _coverage_blockers(cases)
    if coverage_blockers:
        raise CohesionCaseSetError("cohesion case set is incomplete", reason="incomplete")
    return cases


def blocked_result_from_loader_error(
    run_id: str,
    error: CohesionCaseSetError,
    dependency_refs: DependencyContractRefs,
) -> CohesionEvalResult:
    """Convert a loader failure into the normal fail-closed result shape.

    Args:
        run_id: Run identifier used to locate the workbench run.
        error: Error value consumed by blocked_result_from_loader_error().
        dependency_refs: Dependency refs value consumed by blocked_result_from_loader_error().

    Returns:
        CohesionEvalResult value produced by blocked_result_from_loader_error().
    """
    blocker = f"{BLOCKER_CASE_SET_MALFORMED}:{error.reason}"
    return CohesionEvalResult(
        run_id=run_id,
        status=CohesionDecisionStatus.LOADER_ERROR,
        blockers=(blocker,),
        dimension_scores=(),
        dependency_refs=dependency_refs,
        evidence={"schema_version": SCHEMA_VERSION, "loader_error": error.reason},
    )


def evaluate_cohesion_run(
    *,
    run_id: str,
    cases: tuple[CohesionEvalCase, ...],
    baseline_observations: tuple[CohesionObservation, ...],
    candidate_observations: tuple[CohesionObservation, ...],
    dependency_refs: DependencyContractRefs,
) -> CohesionEvalResult:
    """Approve only candidates that preserve every required cohesion dimension.

    Returns:
        CohesionEvalResult value produced by evaluate_cohesion_run().
    """
    blockers: list[str] = []
    dimension_scores: list[CohesionDimensionScore] = []
    blockers.extend(_coverage_blockers(cases))

    baseline_by_case, baseline_blockers = _index_observations(baseline_observations, "baseline")
    candidate_by_case, candidate_blockers = _index_observations(candidate_observations, "candidate")
    blockers.extend(baseline_blockers)
    blockers.extend(candidate_blockers)
    expected_case_ids = {case.case_id for case in cases}
    blockers.extend(_unknown_case_blockers("baseline", baseline_by_case, expected_case_ids))
    blockers.extend(_unknown_case_blockers("candidate", candidate_by_case, expected_case_ids))

    for case in cases:
        case_blockers, dimension_score = _score_case(case, baseline_by_case, candidate_by_case)
        blockers.extend(case_blockers)
        if dimension_score is not None:
            dimension_scores.append(dimension_score)

    unique_blockers = tuple(dict.fromkeys(blockers))
    return CohesionEvalResult(
        run_id=run_id,
        status=CohesionDecisionStatus.BLOCKED if unique_blockers else CohesionDecisionStatus.APPROVED,
        blockers=unique_blockers,
        dimension_scores=tuple(dimension_scores),
        dependency_refs=dependency_refs,
        evidence={
            "schema_version": SCHEMA_VERSION,
            "case_count": len(cases),
            "dimension_count": len({case.dimension for case in cases}),
            "surface_count": len({case.surface for case in cases}),
            "average_baseline_score": _average(score.baseline_score for score in dimension_scores),
            "average_candidate_score": _average(score.candidate_score for score in dimension_scores),
            "mutated_artifacts": [],
        },
    )


def _unknown_case_blockers(
    prefix: str,
    observations_by_case: dict[str, CohesionObservation],
    expected_case_ids: set[str],
) -> list[str]:
    return [
        f"{BLOCKER_CASE_SET_MALFORMED}:{prefix}_unknown_case:{case_id}"
        for case_id in sorted(set(observations_by_case) - expected_case_ids)
    ]


def _score_case(
    case: CohesionEvalCase,
    baseline_by_case: dict[str, CohesionObservation],
    candidate_by_case: dict[str, CohesionObservation],
) -> tuple[list[str], CohesionDimensionScore | None]:
    blockers = _dependency_blockers(case.dependency_refs, f"case:{case.case_id}")
    baseline = baseline_by_case.get(case.case_id)
    candidate = candidate_by_case.get(case.case_id)
    if baseline is None:
        blockers.append(f"{BLOCKER_MISSING_BASELINE_OBSERVATION}:{case.case_id}")
    if candidate is None:
        blockers.append(f"{BLOCKER_MISSING_CANDIDATE_OBSERVATION}:{case.case_id}")
    if baseline is None or candidate is None:
        return blockers, None

    case_blockers = _case_observation_blockers(case, baseline, candidate)
    blockers.extend(case_blockers)
    return blockers, CohesionDimensionScore(
        case_id=case.case_id,
        dimension=case.dimension,
        surface=case.surface,
        baseline_score=baseline.score,
        candidate_score=candidate.score,
        minimum_score=case.minimum_score,
        passed=not case_blockers,
        blockers=tuple(dict.fromkeys(case_blockers)),
    )


def _case_observation_blockers(
    case: CohesionEvalCase,
    baseline: CohesionObservation,
    candidate: CohesionObservation,
) -> list[str]:
    blockers = _observation_blockers(case, baseline, "baseline") + _observation_blockers(case, candidate, "candidate")
    if candidate.score < baseline.score or candidate.score < case.minimum_score:
        blockers.append(f"{BLOCKER_DIMENSION_REGRESSION}:{case.case_id}:{case.dimension.value}")
    if case.feedback_kind is FeedbackKind.POSITIVE and (
        not candidate.truthfulness_refs or not candidate.safety_refs or not candidate.anti_sycophancy_decision.approved
    ):
        blockers.append(f"{BLOCKER_POSITIVE_FEEDBACK_UNGOVERNED}:{case.case_id}")
    return blockers


def cohesion_result_to_improvement_candidate(
    result: CohesionEvalResult,
    *,
    now_utc: datetime | None = None,
) -> ImprovementCandidate:
    """Build an improvement-engine compatible candidate without persisting it.

    Returns:
        ImprovementCandidate value produced by cohesion_result_to_improvement_candidate().
    """
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    impacted = tuple(
        f"persona_cohesion:{score.dimension.value}:{score.surface.value}"
        for score in result.dimension_scores
        if not score.passed
    ) or ("persona_cohesion:blocked-run",)
    source_signals = (
        ImprovementSignal(
            signal_id=f"signal:{result.run_id}:cohesion-eval",
            kind=ImprovementSignalKind.MODEL_EVAL,
            summary="Persona cohesion eval produced blocked or degraded dimensions.",
            source_ref=f"cohesion-run:{result.run_id}",
            captured_at_utc=now.isoformat(),
            suggested_target=PromotionTarget.EVAL_CASE,
        ),
        ImprovementSignal(
            signal_id=f"signal:{result.run_id}:feedback-evidence",
            kind=ImprovementSignalKind.USER_CORRECTION if result.blockers else ImprovementSignalKind.USER_FEEDBACK,
            summary="Feedback remains evidence for governed proposals, not silent prompt mutation.",
            source_ref=f"cohesion-run:{result.run_id}:feedback",
            captured_at_utc=now.isoformat(),
            suggested_target=PromotionTarget.EVAL_CASE,
        ),
    )
    return ImprovementCandidate(
        candidate_id=f"candidate:{result.run_id}:persona-cohesion",
        target=PromotionTarget.EVAL_CASE,
        lifecycle=PromotionLifecycle.SHADOW,
        source_signals=source_signals,
        baseline_evidence=(_evidence_ref(result, EvidenceRole.BASELINE, now, "baseline persona-cohesion scores"),),
        candidate_evidence=(_evidence_ref(result, EvidenceRole.CANDIDATE, now, "candidate persona-cohesion scores"),),
        regression_checks=(
            _evidence_ref(result, EvidenceRole.REGRESSION_CHECK, now, "per-dimension regression checks"),
        ),
        negative_results=(
            _evidence_ref(result, EvidenceRole.NEGATIVE_RESULT, now, "blocked dimensions retained as negative results"),
        ),
        impacted_assets=impacted,
        risk="medium",
        rollback_target_ref=f"cohesion-baseline:{result.run_id}",
        expires_at_utc=(now + timedelta(days=30)).isoformat(),
        post_promotion_monitoring_refs=(f"monitoring:cohesion:{result.run_id}",),
        dependency_refs=result.dependency_refs,
    )


def _case_from_mapping(raw_case: dict[str, Any]) -> CohesionEvalCase:
    return CohesionEvalCase(
        case_id=str(raw_case["case_id"]),
        dimension=CohesionDimension(str(raw_case["dimension"])),
        surface=SurfaceContext(str(raw_case["surface"])),
        prompt_ref=str(raw_case["prompt_ref"]),
        expected_behavior_ref=str(raw_case["expected_behavior_ref"]),
        dependency_refs=_dependency_refs_from_mapping(dict(raw_case["dependency_refs"])),
        feedback_kind=FeedbackKind(str(raw_case.get("feedback_kind", FeedbackKind.NONE.value))),
        feedback_refs=tuple(str(value) for value in raw_case.get("feedback_refs", ())),
        minimum_score=float(raw_case.get("minimum_score", 0.75)),
        eval_label_only=bool(raw_case.get("eval_label_only", True)),
    )


def _dependency_refs_from_mapping(raw_refs: dict[str, Any]) -> CohesionDependencyRefs:
    return CohesionDependencyRefs(
        trace_eval_refs=tuple(str(value) for value in raw_refs["trace_eval_refs"]),
        memory_governance_refs=tuple(str(value) for value in raw_refs["memory_governance_refs"]),
        personalization_governance_refs=tuple(str(value) for value in raw_refs["personalization_governance_refs"]),
        project_preference_refs=tuple(str(value) for value in raw_refs["project_preference_refs"]),
        anti_sycophancy_gate_ref=str(raw_refs["anti_sycophancy_gate_ref"]),
    )


def _coverage_blockers(cases: tuple[CohesionEvalCase, ...]) -> list[str]:
    if not cases:
        return [f"{BLOCKER_MISSING_FIXTURE_COVERAGE}:empty_case_set"]
    blockers: list[str] = []
    dimensions = {case.dimension for case in cases}
    surfaces = {case.surface for case in cases}
    blockers.extend(
        f"{BLOCKER_MISSING_FIXTURE_COVERAGE}:dimension:{dimension.value}"
        for dimension in sorted(set(CohesionDimension) - dimensions, key=lambda item: item.value)
    )
    blockers.extend(
        f"{BLOCKER_MISSING_FIXTURE_COVERAGE}:surface:{surface.value}"
        for surface in sorted(set(SurfaceContext) - surfaces, key=lambda item: item.value)
    )
    return blockers


def _index_observations(
    observations: tuple[CohesionObservation, ...],
    prefix: str,
) -> tuple[dict[str, CohesionObservation], list[str]]:
    indexed: dict[str, CohesionObservation] = {}
    blockers: list[str] = []
    for observation in observations:
        if observation.case_id in indexed:
            blockers.append(f"{BLOCKER_CASE_SET_MALFORMED}:{prefix}:duplicate:{observation.case_id}")
        indexed[observation.case_id] = observation
    return indexed, blockers


def _observation_blockers(case: CohesionEvalCase, observation: CohesionObservation, prefix: str) -> list[str]:
    blockers: list[str] = []
    if observation.dimension is not case.dimension:
        blockers.append(f"{BLOCKER_CASE_SET_MALFORMED}:{prefix}:{case.case_id}:dimension_mismatch")
    if observation.surface is not case.surface:
        blockers.append(f"{BLOCKER_CASE_SET_MALFORMED}:{prefix}:{case.case_id}:surface_mismatch")
    blockers.extend(_dependency_blockers(observation.dependency_refs, f"{prefix}:{case.case_id}"))
    if observation.anti_sycophancy_decision is None:
        blockers.append(f"{BLOCKER_MISSING_ANTI_SYCOPHANCY_GATE}:{prefix}:{case.case_id}")
    elif not observation.anti_sycophancy_decision.approved:
        blockers.append(f"{BLOCKER_FAILING_ANTI_SYCOPHANCY_GATE}:{prefix}:{case.case_id}")
    if "ref-only" in observation.dependency_refs.anti_sycophancy_gate_ref:
        blockers.append(f"{BLOCKER_REFERENCE_ONLY_EVIDENCE}:{prefix}:{case.case_id}:anti_sycophancy")
    return blockers


def _dependency_blockers(refs: CohesionDependencyRefs, prefix: str) -> list[str]:
    blockers: list[str] = []
    fields = (
        ("trace_eval_refs", refs.trace_eval_refs),
        ("memory_governance_refs", refs.memory_governance_refs),
        ("personalization_governance_refs", refs.personalization_governance_refs),
        ("project_preference_refs", refs.project_preference_refs),
    )
    for field_name, values in fields:
        if not values:
            blockers.append(f"{BLOCKER_MISSING_DEPENDENCY_REF}:{prefix}:{field_name}")
    if not refs.anti_sycophancy_gate_ref.strip():
        blockers.append(f"{BLOCKER_MISSING_DEPENDENCY_REF}:{prefix}:anti_sycophancy_gate_ref")
    return blockers


def _evidence_ref(
    result: CohesionEvalResult,
    role: EvidenceRole,
    captured_at: datetime,
    summary: str,
) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=f"evidence:{result.run_id}:{role.value}",
        role=role,
        kind="persona_cohesion_eval",
        summary=summary,
        source_ref=f"cohesion-run:{result.run_id}",
        captured_at_utc=captured_at.isoformat(),
        confidence=0.91,
        provenance_ref=f"provenance:{result.run_id}:persona-cohesion",
    )


def _average(values: Any) -> float:
    collected = tuple(values)
    if not collected:
        return 0.0
    return round(sum(collected) / len(collected), 6)


__all__ = [
    "DEFAULT_CASE_SET_PATH",
    "blocked_result_from_loader_error",
    "cohesion_result_to_improvement_candidate",
    "evaluate_cohesion_run",
    "load_cohesion_eval_cases",
]
