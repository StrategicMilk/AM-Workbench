"""Contracts for promoting Workbench evidence into the right artifact type."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1

BLOCKER_MISSING_BASELINE_EVIDENCE = "missing_baseline_evidence"
BLOCKER_MISSING_CANDIDATE_EVIDENCE = "missing_candidate_evidence"
BLOCKER_MISSING_NEGATIVE_RESULT = "missing_negative_result"
BLOCKER_MISSING_REGRESSION_CHECK = "missing_regression_check"
BLOCKER_MISSING_ROLLBACK_TARGET = "missing_rollback_target"
BLOCKER_MISSING_EXPIRY = "missing_expiry"
BLOCKER_EXPIRED_CANDIDATE = "expired_candidate"
BLOCKER_MISSING_MONITORING = "missing_post_promotion_monitoring"
BLOCKER_MISSING_IMPACTED_ASSET = "missing_impacted_asset"
BLOCKER_UNKNOWN_PROMOTION_TARGET = "unknown_promotion_target"
BLOCKER_STATE_CORRUPT = "state_corrupt"
BLOCKER_STATE_UNAVAILABLE = "state_unavailable"


class ImprovementEngineError(ValueError):
    """Raised when an improvement-engine contract cannot be trusted."""


class ImprovementSignalKind(str, Enum):
    """Observed inputs the improvement engine can convert into candidates."""

    TRACE = "trace"
    USER_FEEDBACK = "user_feedback"
    USER_CORRECTION = "user_correction"
    INSPECTOR_FINDING = "inspector_finding"
    ROUTE_OUTCOME = "route_outcome"
    COST_OBSERVATION = "cost_observation"
    LATENCY_OBSERVATION = "latency_observation"
    RUNTIME_OBSERVATION = "runtime_observation"
    RETRIEVAL_OBSERVATION = "retrieval_observation"
    TOOL_POLICY_OBSERVATION = "tool_policy_observation"
    MODEL_EVAL = "model_eval"


class PromotionTarget(str, Enum):
    """Artifact targets supported by evidence-to-artifact promotion."""

    MEMORY_PROFILE_CARD = "memory_profile_card"
    PROMPT = "prompt"
    ROUTE = "route"
    RETRIEVAL_RULE = "retrieval_rule"
    TOOL_POLICY = "tool_policy"
    SCHEDULER_DEFAULT = "scheduler_default"
    UI_DEFAULT = "ui_default"
    EVAL_CASE = "eval_case"
    MODEL_ADAPTER = "model_adapter"
    SPECIALIST_MODEL = "specialist_model"
    TRAINING_RECIPE = "training_recipe"


class EvidenceRole(str, Enum):
    """How one evidence reference participates in a candidate decision."""

    BASELINE = "baseline"
    CANDIDATE = "candidate"
    REGRESSION_CHECK = "regression_check"
    NEGATIVE_RESULT = "negative_result"
    OBSERVATION = "observation"


class PromotionLifecycle(str, Enum):
    """Reversible lifecycle stages before a change becomes the default."""

    SHADOW = "shadow"
    CANARY = "canary"
    DEFAULT = "default"


class CandidateStatus(str, Enum):
    """Typed outcome of evaluating or persisting an improvement candidate."""

    APPROVED = "approved"
    BLOCKED = "blocked"
    RECOVERY_NEEDED = "recovery_needed"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """One evidence artifact attached to a baseline, candidate, regression, or negative result."""

    evidence_id: str
    role: EvidenceRole
    kind: str
    summary: str
    source_ref: str
    captured_at_utc: str
    confidence: float
    provenance_ref: str

    def __post_init__(self) -> None:
        _require_text(self.evidence_id, "evidence_id")
        if not isinstance(self.role, EvidenceRole):
            raise ImprovementEngineError("role must be EvidenceRole")
        for field_name in ("kind", "summary", "source_ref", "provenance_ref"):
            _require_text(getattr(self, field_name), field_name)
        _parse_utc(self.captured_at_utc, "captured_at_utc")
        _require_confidence(self.confidence, "confidence")

    @property
    def negative_result(self) -> bool:
        """Return true when this artifact must be retained as a negative result."""
        return self.role is EvidenceRole.NEGATIVE_RESULT

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["role"] = self.role.value
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvidenceRef(evidence_id={self.evidence_id!r}, role={self.role!r}, kind={self.kind!r})"


@dataclass(frozen=True, slots=True)
class ImprovementSignal:
    """One trace, correction, finding, route outcome, or runtime observation."""

    signal_id: str
    kind: ImprovementSignalKind
    summary: str
    source_ref: str
    captured_at_utc: str
    suggested_target: PromotionTarget | None = None

    def __post_init__(self) -> None:
        _require_text(self.signal_id, "signal_id")
        if not isinstance(self.kind, ImprovementSignalKind):
            raise ImprovementEngineError("kind must be ImprovementSignalKind")
        for field_name in ("summary", "source_ref"):
            _require_text(getattr(self, field_name), field_name)
        _parse_utc(self.captured_at_utc, "captured_at_utc")
        if self.suggested_target is not None and not isinstance(self.suggested_target, PromotionTarget):
            raise ImprovementEngineError("suggested_target must be PromotionTarget")

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["kind"] = self.kind.value
        payload["suggested_target"] = self.suggested_target.value if self.suggested_target else None
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ImprovementSignal(signal_id={self.signal_id!r}, kind={self.kind!r}, summary={self.summary!r})"


@dataclass(frozen=True, slots=True)
class DependencyContractRefs:
    """References to dependency-pack contracts preserved by this bridge."""

    self_improvement_proposal_ref: str
    trace_eval_case_ref: str
    sweep_experiment_ref: str
    model_foundry_ref: str
    tuning_data_source_ref: str
    agent_run_harness_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "self_improvement_proposal_ref",
            "trace_eval_case_ref",
            "sweep_experiment_ref",
            "model_foundry_ref",
            "tuning_data_source_ref",
            "agent_run_harness_ref",
        ):
            _require_text(getattr(self, field_name), field_name)

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DependencyContractRefs(self_improvement_proposal_ref={self.self_improvement_proposal_ref!r}, trace_eval_case_ref={self.trace_eval_case_ref!r}, sweep_experiment_ref={self.sweep_experiment_ref!r})"


@dataclass(frozen=True, slots=True)
class ImprovementCandidate:
    """A governed candidate change before shadow, canary, or default promotion."""

    candidate_id: str
    target: PromotionTarget
    lifecycle: PromotionLifecycle
    source_signals: tuple[ImprovementSignal, ...]
    baseline_evidence: tuple[EvidenceRef, ...]
    candidate_evidence: tuple[EvidenceRef, ...]
    regression_checks: tuple[EvidenceRef, ...]
    negative_results: tuple[EvidenceRef, ...]
    impacted_assets: tuple[str, ...]
    risk: str
    rollback_target_ref: str
    expires_at_utc: str
    post_promotion_monitoring_refs: tuple[str, ...]
    dependency_refs: DependencyContractRefs

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        if not isinstance(self.target, PromotionTarget):
            raise ImprovementEngineError("target must be PromotionTarget")
        if not isinstance(self.lifecycle, PromotionLifecycle):
            raise ImprovementEngineError("lifecycle must be PromotionLifecycle")
        _require_tuple_type(self.source_signals, ImprovementSignal, "source_signals")
        _require_tuple_type(self.baseline_evidence, EvidenceRef, "baseline_evidence", allow_empty=True)
        _require_tuple_type(self.candidate_evidence, EvidenceRef, "candidate_evidence", allow_empty=True)
        _require_tuple_type(self.regression_checks, EvidenceRef, "regression_checks", allow_empty=True)
        _require_tuple_type(self.negative_results, EvidenceRef, "negative_results", allow_empty=True)
        _require_string_tuple(self.impacted_assets, "impacted_assets", allow_empty=True)
        _require_text(self.risk, "risk")
        _require_string_tuple(self.post_promotion_monitoring_refs, "post_promotion_monitoring_refs", allow_empty=True)
        if not isinstance(self.dependency_refs, DependencyContractRefs):
            raise ImprovementEngineError("dependency_refs must be DependencyContractRefs")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": self.candidate_id,
            "target": self.target.value,
            "lifecycle": self.lifecycle.value,
            "source_signals": [signal.to_dict() for signal in self.source_signals],
            "baseline_evidence": [item.to_dict() for item in self.baseline_evidence],
            "candidate_evidence": [item.to_dict() for item in self.candidate_evidence],
            "regression_checks": [item.to_dict() for item in self.regression_checks],
            "negative_results": [item.to_dict() for item in self.negative_results],
            "impacted_assets": list(self.impacted_assets),
            "risk": self.risk,
            "rollback_target_ref": self.rollback_target_ref,
            "expires_at_utc": self.expires_at_utc,
            "post_promotion_monitoring_refs": list(self.post_promotion_monitoring_refs),
            "dependency_refs": self.dependency_refs.to_dict(),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ImprovementCandidate(candidate_id={self.candidate_id!r}, target={self.target!r}, lifecycle={self.lifecycle!r})"


@dataclass(frozen=True, slots=True)
class ImprovementDecision:
    """Deterministic result for a candidate evaluation or state write."""

    candidate_id: str
    status: CandidateStatus
    target: PromotionTarget | None
    lifecycle: PromotionLifecycle | None
    blockers: tuple[str, ...]
    evidence: dict[str, Any]

    @property
    def approved(self) -> bool:
        """Return true only for clean decisions."""
        return self.status is CandidateStatus.APPROVED and not self.blockers

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "status": self.status.value,
            "target": self.target.value if self.target else None,
            "lifecycle": self.lifecycle.value if self.lifecycle else None,
            "approved": self.approved,
            "blockers": list(self.blockers),
            "evidence": self.evidence,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ImprovementDecision(candidate_id={self.candidate_id!r}, status={self.status!r}, target={self.target!r})"
        )


def classify_promotion_target(signals: tuple[ImprovementSignal, ...]) -> PromotionTarget:
    """Classify the most specific artifact target for the supplied signals.

    Returns:
        PromotionTarget value produced by classify_promotion_target().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    _require_tuple_type(signals, ImprovementSignal, "signals")
    explicit = tuple(signal.suggested_target for signal in signals if signal.suggested_target is not None)
    if explicit:
        return explicit[0]
    priority = (
        (ImprovementSignalKind.ROUTE_OUTCOME, PromotionTarget.ROUTE),
        (ImprovementSignalKind.RETRIEVAL_OBSERVATION, PromotionTarget.RETRIEVAL_RULE),
        (ImprovementSignalKind.TOOL_POLICY_OBSERVATION, PromotionTarget.TOOL_POLICY),
        (ImprovementSignalKind.COST_OBSERVATION, PromotionTarget.SCHEDULER_DEFAULT),
        (ImprovementSignalKind.LATENCY_OBSERVATION, PromotionTarget.SCHEDULER_DEFAULT),
        (ImprovementSignalKind.USER_FEEDBACK, PromotionTarget.MEMORY_PROFILE_CARD),
        (ImprovementSignalKind.USER_CORRECTION, PromotionTarget.PROMPT),
        (ImprovementSignalKind.INSPECTOR_FINDING, PromotionTarget.EVAL_CASE),
        (ImprovementSignalKind.TRACE, PromotionTarget.EVAL_CASE),
        (ImprovementSignalKind.MODEL_EVAL, PromotionTarget.MODEL_ADAPTER),
        (ImprovementSignalKind.RUNTIME_OBSERVATION, PromotionTarget.UI_DEFAULT),
    )
    signal_kinds = {signal.kind for signal in signals}
    for signal_kind, target in priority:
        if signal_kind in signal_kinds:
            return target
    raise ImprovementEngineError(BLOCKER_UNKNOWN_PROMOTION_TARGET)


def evaluate_candidate(
    candidate: ImprovementCandidate,
    *,
    now_utc: datetime | None = None,
) -> ImprovementDecision:
    """Evaluate a candidate without mutating any artifact or default.

    Returns:
        ImprovementDecision value produced by evaluate_candidate().
    """
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    blockers: list[str] = []

    if not candidate.baseline_evidence or not _has_role(candidate.baseline_evidence, EvidenceRole.BASELINE):
        blockers.append(BLOCKER_MISSING_BASELINE_EVIDENCE)
    if not candidate.candidate_evidence or not _has_role(candidate.candidate_evidence, EvidenceRole.CANDIDATE):
        blockers.append(BLOCKER_MISSING_CANDIDATE_EVIDENCE)
    if not candidate.negative_results or not all(item.negative_result for item in candidate.negative_results):
        blockers.append(BLOCKER_MISSING_NEGATIVE_RESULT)
    if not candidate.regression_checks or not _has_role(candidate.regression_checks, EvidenceRole.REGRESSION_CHECK):
        blockers.append(BLOCKER_MISSING_REGRESSION_CHECK)
    if not candidate.impacted_assets:
        blockers.append(BLOCKER_MISSING_IMPACTED_ASSET)
    if not candidate.rollback_target_ref.strip():
        blockers.append(BLOCKER_MISSING_ROLLBACK_TARGET)
    if not candidate.expires_at_utc.strip():
        blockers.append(BLOCKER_MISSING_EXPIRY)
    else:
        try:
            expires_at = _parse_utc(candidate.expires_at_utc, "expires_at_utc")
        except ImprovementEngineError:
            blockers.append(BLOCKER_MISSING_EXPIRY)
        else:
            if expires_at <= now:
                blockers.append(BLOCKER_EXPIRED_CANDIDATE)
    if not candidate.post_promotion_monitoring_refs:
        blockers.append(BLOCKER_MISSING_MONITORING)

    unique_blockers = tuple(dict.fromkeys(blockers))
    return ImprovementDecision(
        candidate_id=candidate.candidate_id,
        status=CandidateStatus.BLOCKED if unique_blockers else CandidateStatus.APPROVED,
        target=candidate.target,
        lifecycle=candidate.lifecycle,
        blockers=unique_blockers,
        evidence={
            "schema_version": SCHEMA_VERSION,
            "source_signal_count": len(candidate.source_signals),
            "baseline_evidence_count": len(candidate.baseline_evidence),
            "candidate_evidence_count": len(candidate.candidate_evidence),
            "negative_result_count": len(candidate.negative_results),
            "regression_check_count": len(candidate.regression_checks),
            "impacted_asset_count": len(candidate.impacted_assets),
            "monitoring_ref_count": len(candidate.post_promotion_monitoring_refs),
        },
    )


def recovery_needed_decision(candidate_id: str, blocker: str, message: str) -> ImprovementDecision:
    """Build a typed fail-closed state recovery result.

    Args:
        candidate_id: Candidate id value consumed by recovery_needed_decision().
        blocker: Blocker value consumed by recovery_needed_decision().
        message: Message value consumed by recovery_needed_decision().

    Returns:
        ImprovementDecision value produced by recovery_needed_decision().
    """
    _require_text(candidate_id, "candidate_id")
    _require_text(blocker, "blocker")
    _require_text(message, "message")
    return ImprovementDecision(
        candidate_id=candidate_id,
        status=CandidateStatus.RECOVERY_NEEDED,
        target=None,
        lifecycle=None,
        blockers=(blocker,),
        evidence={"message": message, "schema_version": SCHEMA_VERSION},
    )


def _has_role(items: tuple[EvidenceRef, ...], role: EvidenceRole) -> bool:
    return any(item.role is role for item in items)


def _parse_utc(value: str, field_name: str) -> datetime:
    _require_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ImprovementEngineError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ImprovementEngineError(f"{field_name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ImprovementEngineError(f"{field_name} must be non-empty")


def _require_confidence(value: object, field_name: str) -> None:
    if not isinstance(value, int | float) or not 0.0 < float(value) <= 1.0:
        raise ImprovementEngineError(f"{field_name} must be > 0.0 and <= 1.0")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise ImprovementEngineError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ImprovementEngineError(f"{field_name} must contain non-empty strings")


def _require_tuple_type(
    values: tuple[object, ...],
    expected_type: type[object],
    field_name: str,
    *,
    allow_empty: bool = False,
) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise ImprovementEngineError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, expected_type) for value in values):
        raise ImprovementEngineError(f"{field_name} must contain {expected_type.__name__} values")


__all__ = [
    "BLOCKER_EXPIRED_CANDIDATE",
    "BLOCKER_MISSING_BASELINE_EVIDENCE",
    "BLOCKER_MISSING_CANDIDATE_EVIDENCE",
    "BLOCKER_MISSING_EXPIRY",
    "BLOCKER_MISSING_IMPACTED_ASSET",
    "BLOCKER_MISSING_MONITORING",
    "BLOCKER_MISSING_NEGATIVE_RESULT",
    "BLOCKER_MISSING_REGRESSION_CHECK",
    "BLOCKER_MISSING_ROLLBACK_TARGET",
    "BLOCKER_STATE_CORRUPT",
    "BLOCKER_STATE_UNAVAILABLE",
    "BLOCKER_UNKNOWN_PROMOTION_TARGET",
    "SCHEMA_VERSION",
    "CandidateStatus",
    "DependencyContractRefs",
    "EvidenceRef",
    "EvidenceRole",
    "ImprovementCandidate",
    "ImprovementDecision",
    "ImprovementEngineError",
    "ImprovementSignal",
    "ImprovementSignalKind",
    "PromotionLifecycle",
    "PromotionTarget",
    "classify_promotion_target",
    "evaluate_candidate",
    "recovery_needed_decision",
]
