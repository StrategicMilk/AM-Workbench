"""Side-effect-free cohesion canaries for idle and finalizer checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
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
from vetinari.workbench.personalization.anti_sycophancy import AntiSycophancyGateDecision

SCHEMA_VERSION = 1


class CanaryContractError(ValueError):
    """Raised when cohesion canary evidence cannot be trusted."""


class CanaryTrigger(str, Enum):
    """Checkpoint moments where lightweight cohesion canaries may run."""

    IDLE = "idle"
    TASK_FINALIZATION = "finalizer"


class CanaryDimension(str, Enum):
    """Safety-critical cohesion dimensions checked by the canary."""

    GOAL_PRESERVATION = "goal_preservation"
    FEEDBACK_PRESERVATION = "feedback_preservation"
    OVERCLAIM_DETECTION = "overclaim_detection"
    UNCERTAINTY_SURFACING = "uncertainty_surfacing"
    TRACE_LINKAGE = "trace_linkage"
    TRUTH_SAFETY_PRECEDENCE = "truth_safety_precedence"


class CanarySignalKind(str, Enum):
    """Evidence signal types the canary can record without persisting."""

    GOAL_PRESERVED = "goal_preserved"
    POSITIVE_FEEDBACK = "positive_feedback"
    NEGATIVE_FEEDBACK = "negative_feedback"
    CORRECTION = "correction"
    OVERCLAIM_CHECK = "overclaim_check"
    UNCERTAINTY_SURFACED = "uncertainty_surfaced"
    TRACE_LINKED = "trace_linked"


class CanaryBlocker(str, Enum):
    """Named fail-closed blockers for every canary branch."""

    MISSING_TRACE_REF = "missing_trace_ref"
    MISSING_GOAL_PRESERVATION_REF = "missing_goal_preservation_ref"
    MISSING_FEEDBACK_EVIDENCE = "missing_feedback_evidence"
    OVERCLAIMED_VERIFICATION = "overclaimed_verification"
    MISSING_UNCERTAINTY_SURFACING = "missing_uncertainty_surfacing"
    MISSING_ANTI_SYCOPHANCY_DECISION = "missing_anti_sycophancy_decision"
    FAILING_ANTI_SYCOPHANCY_DECISION = "failing_anti_sycophancy_decision"
    TRUTH_OR_SAFETY_MISSING = "truth_or_safety_missing"
    PROMPT_MUTATION_FORBIDDEN = "prompt_mutation_forbidden"
    MEMORY_MUTATION_FORBIDDEN = "memory_mutation_forbidden"
    MUTATED_ARTIFACT_FORBIDDEN = "mutated_artifact_forbidden"


@dataclass(frozen=True, slots=True)
class CanarySignal:
    """One trace-linked signal captured for a cohesion canary."""

    kind: CanarySignalKind
    summary: str
    trace_refs: tuple[str, ...]
    source_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CanarySignalKind):
            raise CanaryContractError("kind must be CanarySignalKind")
        _require_text(self.summary, "summary")
        _require_string_tuple(self.trace_refs, "trace_refs")
        _require_string_tuple(self.source_refs, "source_refs", allow_empty=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "summary": self.summary,
            "trace_refs": list(self.trace_refs),
            "source_refs": list(self.source_refs),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CanarySignal(kind={self.kind!r}, summary={self.summary!r}, trace_refs={self.trace_refs!r})"


@dataclass(frozen=True, slots=True)
class CanaryResult:
    """A lightweight checkpoint record that can feed evals and proposals."""

    run_id: str
    trigger: CanaryTrigger
    approved: bool
    signals: tuple[CanarySignal, ...]
    blockers: tuple[CanaryBlocker, ...]
    trace_refs: tuple[str, ...]
    goal_preservation_refs: tuple[str, ...]
    feedback_refs: tuple[str, ...]
    uncertainty_refs: tuple[str, ...]
    truth_refs: tuple[str, ...]
    safety_refs: tuple[str, ...]
    anti_sycophancy_decision: AntiSycophancyGateDecision | None
    mutated_artifacts: tuple[str, ...] = ()
    evidence: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_text(self.run_id, "run_id")
        if not isinstance(self.trigger, CanaryTrigger):
            raise CanaryContractError("trigger must be CanaryTrigger")
        _require_tuple_type(self.signals, CanarySignal, "signals", allow_empty=True)
        _require_tuple_type(self.blockers, CanaryBlocker, "blockers", allow_empty=True)
        for field_name in (
            "trace_refs",
            "goal_preservation_refs",
            "feedback_refs",
            "uncertainty_refs",
            "truth_refs",
            "safety_refs",
            "mutated_artifacts",
        ):
            _require_string_tuple(getattr(self, field_name), field_name, allow_empty=True)
        if self.approved and self.blockers:
            raise CanaryContractError("approved canary results cannot include blockers")
        if self.approved and self.mutated_artifacts:
            raise CanaryContractError("approved canary results cannot include mutated_artifacts")
        if self.anti_sycophancy_decision is not None and not isinstance(
            self.anti_sycophancy_decision,
            AntiSycophancyGateDecision,
        ):
            raise CanaryContractError("anti_sycophancy_decision must be AntiSycophancyGateDecision")
        if self.evidence is not None and not isinstance(self.evidence, dict):
            raise CanaryContractError("evidence must be a dict")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "trigger": self.trigger.value,
            "approved": self.approved,
            "signals": [signal.to_dict() for signal in self.signals],
            "blockers": [blocker.name for blocker in self.blockers],
            "trace_refs": list(self.trace_refs),
            "goal_preservation_refs": list(self.goal_preservation_refs),
            "feedback_refs": list(self.feedback_refs),
            "uncertainty_refs": list(self.uncertainty_refs),
            "truth_refs": list(self.truth_refs),
            "safety_refs": list(self.safety_refs),
            "anti_sycophancy_decision": (
                self.anti_sycophancy_decision.to_dict() if self.anti_sycophancy_decision else None
            ),
            "mutated_artifacts": list(self.mutated_artifacts),
            "evidence": dict(self.evidence or {}),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CanaryResult(run_id={self.run_id!r}, trigger={self.trigger!r}, approved={self.approved!r})"


def run_cohesion_canary(
    *,
    run_id: str,
    trigger: CanaryTrigger | str,
    trace_refs: tuple[str, ...],
    goal_preservation_refs: tuple[str, ...],
    feedback_refs: tuple[str, ...],
    uncertainty_refs: tuple[str, ...],
    anti_sycophancy_decision: AntiSycophancyGateDecision | None,
    truth_refs: tuple[str, ...],
    safety_refs: tuple[str, ...],
    verification_claim_refs: tuple[str, ...] = (),
    verified_trace_refs: tuple[str, ...] = (),
    positive_feedback_refs: tuple[str, ...] = (),
    negative_feedback_refs: tuple[str, ...] = (),
    correction_refs: tuple[str, ...] = (),
    prompt_mutation_refs: tuple[str, ...] = (),
    memory_mutation_refs: tuple[str, ...] = (),
    mutated_artifacts: tuple[str, ...] = (),
) -> CanaryResult:
    """Run a fail-closed cohesion checkpoint without mutating any artifact.

    Returns:
        Outcome produced by run_cohesion_canary().
    """
    parsed_trigger = trigger if isinstance(trigger, CanaryTrigger) else CanaryTrigger(str(trigger))
    all_trace_refs = tuple(dict.fromkeys(trace_refs + verified_trace_refs))
    blockers = _canary_blockers(
        trace_refs=trace_refs,
        goal_preservation_refs=goal_preservation_refs,
        feedback_refs=feedback_refs,
        uncertainty_refs=uncertainty_refs,
        anti_sycophancy_decision=anti_sycophancy_decision,
        truth_refs=truth_refs,
        safety_refs=safety_refs,
        verification_claim_refs=verification_claim_refs,
        verified_trace_refs=verified_trace_refs,
        positive_feedback_refs=positive_feedback_refs,
        prompt_mutation_refs=prompt_mutation_refs,
        memory_mutation_refs=memory_mutation_refs,
        mutated_artifacts=mutated_artifacts,
    )
    signals = _signals_from_evidence(
        trace_refs=trace_refs,
        goal_preservation_refs=goal_preservation_refs,
        feedback_refs=feedback_refs,
        uncertainty_refs=uncertainty_refs,
        positive_feedback_refs=positive_feedback_refs,
        negative_feedback_refs=negative_feedback_refs,
        correction_refs=correction_refs,
    )
    unique_blockers = tuple(dict.fromkeys(blockers))
    return CanaryResult(
        run_id=run_id,
        trigger=parsed_trigger,
        approved=not unique_blockers,
        signals=signals,
        blockers=unique_blockers,
        trace_refs=all_trace_refs,
        goal_preservation_refs=goal_preservation_refs,
        feedback_refs=feedback_refs,
        uncertainty_refs=uncertainty_refs,
        truth_refs=truth_refs,
        safety_refs=safety_refs,
        anti_sycophancy_decision=anti_sycophancy_decision,
        mutated_artifacts=mutated_artifacts,
        evidence={
            "schema_version": SCHEMA_VERSION,
            "verification_claim_refs": list(verification_claim_refs),
            "verified_trace_refs": list(verified_trace_refs),
            "prompt_mutation_refs": list(prompt_mutation_refs),
            "memory_mutation_refs": list(memory_mutation_refs),
        },
    )


def _canary_blockers(
    *,
    trace_refs: tuple[str, ...],
    goal_preservation_refs: tuple[str, ...],
    feedback_refs: tuple[str, ...],
    uncertainty_refs: tuple[str, ...],
    anti_sycophancy_decision: AntiSycophancyGateDecision | None,
    truth_refs: tuple[str, ...],
    safety_refs: tuple[str, ...],
    verification_claim_refs: tuple[str, ...],
    verified_trace_refs: tuple[str, ...],
    positive_feedback_refs: tuple[str, ...],
    prompt_mutation_refs: tuple[str, ...],
    memory_mutation_refs: tuple[str, ...],
    mutated_artifacts: tuple[str, ...],
) -> list[CanaryBlocker]:
    blockers: list[CanaryBlocker] = []
    _append_if_empty(blockers, trace_refs, CanaryBlocker.MISSING_TRACE_REF)
    _append_if_empty(blockers, goal_preservation_refs, CanaryBlocker.MISSING_GOAL_PRESERVATION_REF)
    _append_if_empty(blockers, feedback_refs, CanaryBlocker.MISSING_FEEDBACK_EVIDENCE)
    _append_if_empty(blockers, uncertainty_refs, CanaryBlocker.MISSING_UNCERTAINTY_SURFACING)
    if verification_claim_refs and not set(verification_claim_refs) <= set(verified_trace_refs):
        blockers.append(CanaryBlocker.OVERCLAIMED_VERIFICATION)
    if anti_sycophancy_decision is None:
        blockers.append(CanaryBlocker.MISSING_ANTI_SYCOPHANCY_DECISION)
    elif not anti_sycophancy_decision.approved:
        blockers.append(CanaryBlocker.FAILING_ANTI_SYCOPHANCY_DECISION)
    if positive_feedback_refs and (not truth_refs or not safety_refs):
        blockers.append(CanaryBlocker.TRUTH_OR_SAFETY_MISSING)
    if prompt_mutation_refs:
        blockers.append(CanaryBlocker.PROMPT_MUTATION_FORBIDDEN)
    if memory_mutation_refs:
        blockers.append(CanaryBlocker.MEMORY_MUTATION_FORBIDDEN)
    if mutated_artifacts:
        blockers.append(CanaryBlocker.MUTATED_ARTIFACT_FORBIDDEN)
    return blockers


def cohesion_canary_to_improvement_candidate(
    result: CanaryResult,
    *,
    dependency_refs: DependencyContractRefs | None = None,
    now_utc: datetime | None = None,
) -> ImprovementCandidate:
    """Return a shadow improvement candidate without writing it anywhere.

    Returns:
        ImprovementCandidate value produced by cohesion_canary_to_improvement_candidate().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(result, CanaryResult):
        raise CanaryContractError("result must be CanaryResult")
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    deps = dependency_refs or _dependency_refs_for_result(result)
    signal_kind = ImprovementSignalKind.USER_CORRECTION if result.blockers else ImprovementSignalKind.USER_FEEDBACK
    source_signals = (
        ImprovementSignal(
            signal_id=f"signal:{result.run_id}:cohesion-canary",
            kind=ImprovementSignalKind.MODEL_EVAL,
            summary="Cohesion canary result is available for eval-case promotion.",
            source_ref=f"cohesion-canary:{result.run_id}",
            captured_at_utc=now.isoformat(),
            suggested_target=PromotionTarget.EVAL_CASE,
        ),
        ImprovementSignal(
            signal_id=f"signal:{result.run_id}:feedback",
            kind=signal_kind,
            summary="Feedback is recorded as evidence and cannot override truth or safety blockers.",
            source_ref=f"cohesion-canary:{result.run_id}:feedback",
            captured_at_utc=now.isoformat(),
            suggested_target=PromotionTarget.EVAL_CASE,
        ),
    )
    return ImprovementCandidate(
        candidate_id=f"candidate:{result.run_id}:cohesion-canary",
        target=PromotionTarget.EVAL_CASE,
        lifecycle=PromotionLifecycle.SHADOW,
        source_signals=source_signals,
        baseline_evidence=(_evidence_ref(result, EvidenceRole.BASELINE, now, "baseline canary trace links"),),
        candidate_evidence=(_evidence_ref(result, EvidenceRole.CANDIDATE, now, "candidate canary outcome"),),
        regression_checks=(_evidence_ref(result, EvidenceRole.REGRESSION_CHECK, now, "truth and safety blockers"),),
        negative_results=(_evidence_ref(result, EvidenceRole.NEGATIVE_RESULT, now, "retained canary blockers"),),
        impacted_assets=("persona_cohesion:eval_case",),
        risk="medium",
        rollback_target_ref=f"cohesion-canary-baseline:{result.run_id}",
        expires_at_utc=(now + timedelta(days=30)).isoformat(),
        post_promotion_monitoring_refs=(f"monitoring:cohesion-canary:{result.run_id}",),
        dependency_refs=deps,
    )


def _signals_from_evidence(
    *,
    trace_refs: tuple[str, ...],
    goal_preservation_refs: tuple[str, ...],
    feedback_refs: tuple[str, ...],
    uncertainty_refs: tuple[str, ...],
    positive_feedback_refs: tuple[str, ...],
    negative_feedback_refs: tuple[str, ...],
    correction_refs: tuple[str, ...],
) -> tuple[CanarySignal, ...]:
    signals: list[CanarySignal] = []
    if trace_refs:
        signals.append(CanarySignal(CanarySignalKind.TRACE_LINKED, "Canary evidence has trace links.", trace_refs))
    if trace_refs and goal_preservation_refs:
        signals.append(
            CanarySignal(
                CanarySignalKind.GOAL_PRESERVED,
                "User goal preservation evidence is present.",
                trace_refs,
                goal_preservation_refs,
            )
        )
    if trace_refs and feedback_refs:
        signals.append(
            CanarySignal(
                CanarySignalKind.POSITIVE_FEEDBACK,
                "Feedback evidence is preserved as data.",
                trace_refs,
                feedback_refs,
            )
        )
    if trace_refs and uncertainty_refs:
        signals.append(
            CanarySignal(
                CanarySignalKind.UNCERTAINTY_SURFACED,
                "Remaining uncertainty was surfaced.",
                trace_refs,
                uncertainty_refs,
            )
        )
    if trace_refs:
        signals.extend(_feedback_signals(trace_refs, CanarySignalKind.POSITIVE_FEEDBACK, positive_feedback_refs))
        signals.extend(_feedback_signals(trace_refs, CanarySignalKind.NEGATIVE_FEEDBACK, negative_feedback_refs))
        signals.extend(_feedback_signals(trace_refs, CanarySignalKind.CORRECTION, correction_refs))
    return tuple(signals)


def _feedback_signals(
    trace_refs: tuple[str, ...],
    kind: CanarySignalKind,
    source_refs: tuple[str, ...],
) -> tuple[CanarySignal, ...]:
    if not source_refs:
        return ()
    return (CanarySignal(kind, f"{kind.value} evidence recorded.", trace_refs, source_refs),)


def _dependency_refs_for_result(result: CanaryResult) -> DependencyContractRefs:
    trace_ref = result.trace_refs[0] if result.trace_refs else f"cohesion-canary:{result.run_id}:missing-trace"
    return DependencyContractRefs(
        self_improvement_proposal_ref=f"cohesion-canary:{result.run_id}:proposal-shadow",
        trace_eval_case_ref=trace_ref,
        sweep_experiment_ref=f"cohesion-canary:{result.run_id}:not-a-sweep",
        model_foundry_ref=f"cohesion-canary:{result.run_id}:not-model-training",
        tuning_data_source_ref=f"cohesion-canary:{result.run_id}:not-tuning-data",
        agent_run_harness_ref=f"cohesion-canary:{result.run_id}:checkpoint",
    )


def _evidence_ref(result: CanaryResult, role: EvidenceRole, captured_at: datetime, summary: str) -> EvidenceRef:
    return EvidenceRef(
        evidence_id=f"evidence:{result.run_id}:{role.value}",
        role=role,
        kind="cohesion_canary",
        summary=summary,
        source_ref=f"cohesion-canary:{result.run_id}",
        captured_at_utc=captured_at.isoformat(),
        confidence=0.9,
        provenance_ref=f"provenance:{result.run_id}:cohesion-canary",
    )


def _append_if_empty(blockers: list[CanaryBlocker], refs: tuple[str, ...], blocker: CanaryBlocker) -> None:
    if not refs:
        blockers.append(blocker)


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CanaryContractError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise CanaryContractError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise CanaryContractError(f"{field_name} must contain non-empty strings")


def _require_tuple_type(
    values: tuple[object, ...],
    expected_type: type[object],
    field_name: str,
    *,
    allow_empty: bool = False,
) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise CanaryContractError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, expected_type) for value in values):
        raise CanaryContractError(f"{field_name} must contain {expected_type.__name__} values")


__all__ = [
    "SCHEMA_VERSION",
    "CanaryBlocker",
    "CanaryContractError",
    "CanaryDimension",
    "CanaryResult",
    "CanarySignal",
    "CanarySignalKind",
    "CanaryTrigger",
    "cohesion_canary_to_improvement_candidate",
    "run_cohesion_canary",
]
