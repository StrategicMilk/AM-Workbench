"""Evidence budget and value-accounting contracts for AM Workbench.

The module is intentionally import-safe: it does not read or write disk and
does not register background workers. Callers pass a complete accounting record
and receive a fail-closed verdict that can be surfaced by monitoring,
automation, eval, red-team, training, or governance adapters.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from math import isfinite
from typing import Any


class EvidenceBudgetError(ValueError):
    """Raised when an accounting record is malformed before evaluation."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(f"{reason}: {message}" if message else reason)
        self.reason = reason


class EvidenceMechanismKind(str, Enum):
    """Quality machinery types that must prove ongoing value."""

    MONITOR = "monitor"
    EVAL_SUITE = "eval_suite"
    RED_TEAM_PACK = "red_team_pack"
    GRAPH_RAG_PREPROCESSING = "graph_rag_preprocessing"
    SYNTHETIC_DATA_GENERATION = "synthetic_data_generation"
    TRAINING_RECIPE = "training_recipe"
    AUTOMATION = "automation"


class AdoptionOutcome(str, Enum):
    """Observed adoption state for the mechanism's outputs."""

    UNKNOWN = "unknown"
    UNUSED = "unused"
    PILOT = "pilot"
    ADOPTED = "adopted"
    RETIRED = "retired"


class EvidenceBudgetDecision(str, Enum):
    """Action produced by evidence budget evaluation."""

    APPROVE = "approve"
    BATCH = "batch"
    DOWNGRADE = "downgrade"
    RETIRE = "retire"
    BLOCKED = "blocked"


class EvidenceBudgetBlocker(str, Enum):
    """Machine-readable reasons the mechanism cannot be treated as valuable."""

    MISSING_PROVENANCE = "missing_provenance"
    MISSING_AUTHORITY = "missing_authority"
    UNKNOWN_EVIDENCE = "unknown_evidence"
    INSUFFICIENT_PROOF = "insufficient_proof"
    FALSE_POSITIVE_RATE_TOO_HIGH = "false_positive_rate_too_high"
    COST_EXCEEDS_VALUE = "cost_exceeds_value"
    NEGATIVE_ADOPTION = "negative_adoption"


@dataclass(frozen=True, slots=True)
class EvidenceBudgetCost:
    """Cost dimensions the charter requires every mechanism to record."""

    cost_to_diagnose_usd: float
    cost_to_fix_usd: float
    cost_to_prevent_regression_usd: float
    human_review_minutes: float

    def __post_init__(self) -> None:
        for field_name in (
            "cost_to_diagnose_usd",
            "cost_to_fix_usd",
            "cost_to_prevent_regression_usd",
            "human_review_minutes",
        ):
            _require_finite_non_negative(getattr(self, field_name), field_name)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvidenceBudgetCost(cost_to_diagnose_usd={self.cost_to_diagnose_usd!r}, cost_to_fix_usd={self.cost_to_fix_usd!r}, cost_to_prevent_regression_usd={self.cost_to_prevent_regression_usd!r})"


@dataclass(frozen=True, slots=True)
class EvidenceBudgetValue:
    """Outcome dimensions used to prove the mechanism still earns its cost."""

    avoided_repeat_failures: int
    adoption_outcome: AdoptionOutcome | str
    evidence_confidence: float | None
    false_positive_rate: float
    value_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.avoided_repeat_failures, int) or self.avoided_repeat_failures < 0:
            raise EvidenceBudgetError("avoided-repeat-failures-invalid", "must be a non-negative integer")
        object.__setattr__(
            self,
            "adoption_outcome",
            _coerce_enum(AdoptionOutcome, self.adoption_outcome, "adoption-outcome-unknown"),
        )
        if self.evidence_confidence is not None:
            _require_ratio(self.evidence_confidence, "evidence_confidence")
        _require_ratio(self.false_positive_rate, "false_positive_rate")
        _require_text_tuple(self.value_notes, "value_notes", allow_empty=True)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvidenceBudgetValue(avoided_repeat_failures={self.avoided_repeat_failures!r}, adoption_outcome={self.adoption_outcome!r}, evidence_confidence={self.evidence_confidence!r})"


@dataclass(frozen=True, slots=True)
class EvidenceBudgetRecord:
    """Complete accounting record for one quality mechanism."""

    mechanism_id: str
    mechanism_kind: EvidenceMechanismKind | str
    project_id: str
    captured_at_utc: str
    costs: EvidenceBudgetCost
    value: EvidenceBudgetValue
    provenance_refs: tuple[str, ...]
    authority_ref: str
    target_ref: str
    mechanism_version: str
    owner: str
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in (
            "mechanism_id",
            "project_id",
            "captured_at_utc",
            "target_ref",
            "mechanism_version",
            "owner",
        ):
            _require_text(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "mechanism_kind",
            _coerce_enum(EvidenceMechanismKind, self.mechanism_kind, "mechanism-kind-unknown"),
        )
        if not isinstance(self.costs, EvidenceBudgetCost):
            raise EvidenceBudgetError("costs-invalid", "costs must be EvidenceBudgetCost")
        if not isinstance(self.value, EvidenceBudgetValue):
            raise EvidenceBudgetError("value-invalid", "value must be EvidenceBudgetValue")
        if not isinstance(self.provenance_refs, tuple):
            raise EvidenceBudgetError("provenance_refs-invalid", "provenance_refs must be a tuple")
        if not isinstance(self.metadata, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.metadata.items()
        ):
            raise EvidenceBudgetError("metadata-invalid", "metadata must be a string mapping")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-schema-shaped payload.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["mechanism_kind"] = self.mechanism_kind.value
        payload["value"]["adoption_outcome"] = self.value.adoption_outcome.value
        payload["provenance_refs"] = list(self.provenance_refs)
        payload["value"]["value_notes"] = list(self.value.value_notes)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvidenceBudgetRecord(mechanism_id={self.mechanism_id!r}, mechanism_kind={self.mechanism_kind!r}, project_id={self.project_id!r})"


@dataclass(frozen=True, slots=True)
class EvidenceBudgetPolicy:
    """Thresholds used to decide whether quality machinery pays for itself."""

    min_confidence: float = 0.65
    max_false_positive_rate: float = 0.25
    min_value_to_cost_ratio: float = 1.0
    high_cost_usd: float = 500.0
    cost_per_review_minute_usd: float = 2.0
    value_per_avoided_failure_usd: float = 250.0
    pilot_adoption_value_usd: float = 100.0
    adopted_value_usd: float = 400.0

    def __post_init__(self) -> None:
        _require_ratio(self.min_confidence, "min_confidence")
        _require_ratio(self.max_false_positive_rate, "max_false_positive_rate")
        for field_name in (
            "min_value_to_cost_ratio",
            "high_cost_usd",
            "cost_per_review_minute_usd",
            "value_per_avoided_failure_usd",
            "pilot_adoption_value_usd",
            "adopted_value_usd",
        ):
            _require_finite_non_negative(getattr(self, field_name), field_name)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvidenceBudgetPolicy(min_confidence={self.min_confidence!r}, max_false_positive_rate={self.max_false_positive_rate!r}, min_value_to_cost_ratio={self.min_value_to_cost_ratio!r})"


@dataclass(frozen=True, slots=True)
class EvidenceBudgetVerdict:
    """Fail-closed value-accounting result."""

    mechanism_id: str
    passed: bool
    decision: EvidenceBudgetDecision
    blockers: tuple[EvidenceBudgetBlocker, ...]
    total_cost_usd: float
    estimated_value_usd: float
    value_to_cost_ratio: float
    cost_breakdown: dict[str, float]
    value_breakdown: dict[str, float]
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable verdict payload."""
        return {
            "mechanism_id": self.mechanism_id,
            "passed": self.passed,
            "decision": self.decision.value,
            "blockers": [blocker.value for blocker in self.blockers],
            "total_cost_usd": self.total_cost_usd,
            "estimated_value_usd": self.estimated_value_usd,
            "value_to_cost_ratio": self.value_to_cost_ratio,
            "cost_breakdown": dict(self.cost_breakdown),
            "value_breakdown": dict(self.value_breakdown),
            "evidence": dict(self.evidence),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvidenceBudgetVerdict(mechanism_id={self.mechanism_id!r}, passed={self.passed!r}, decision={self.decision!r})"


def evaluate_evidence_budget(
    record: EvidenceBudgetRecord,
    *,
    policy: EvidenceBudgetPolicy | None = None,
) -> EvidenceBudgetVerdict:
    """Evaluate a quality mechanism and route low-value machinery away from live use.

    Returns:
        EvidenceBudgetVerdict value produced by evaluate_evidence_budget().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(record, EvidenceBudgetRecord):
        raise EvidenceBudgetError("record-invalid", "record must be EvidenceBudgetRecord")
    active_policy = policy or EvidenceBudgetPolicy()
    total_cost, cost_breakdown = _cost_breakdown(record.costs, active_policy)
    estimated_value, value_breakdown = _value_breakdown(record.value, active_policy)
    value_to_cost_ratio = estimated_value / total_cost if total_cost else float("inf")
    blockers = _blockers(record, active_policy, value_to_cost_ratio)
    decision = _decision(record, total_cost, blockers, active_policy)
    passed = decision is EvidenceBudgetDecision.APPROVE and not blockers
    return EvidenceBudgetVerdict(
        mechanism_id=record.mechanism_id,
        passed=passed,
        decision=decision,
        blockers=tuple(blockers),
        total_cost_usd=round(total_cost, 2),
        estimated_value_usd=round(estimated_value, 2),
        value_to_cost_ratio=round(value_to_cost_ratio, 4) if isfinite(value_to_cost_ratio) else value_to_cost_ratio,
        cost_breakdown={key: round(value, 2) for key, value in cost_breakdown.items()},
        value_breakdown={key: round(value, 2) for key, value in value_breakdown.items()},
        evidence={
            "mechanism_kind": record.mechanism_kind.value,
            "project_id": record.project_id,
            "target_ref": record.target_ref,
            "mechanism_version": record.mechanism_version,
            "captured_at_utc": record.captured_at_utc,
            "provenance_refs": list(record.provenance_refs),
            "authority_ref": record.authority_ref,
            "adoption_outcome": record.value.adoption_outcome.value,
            "evidence_confidence": record.value.evidence_confidence,
            "false_positive_rate": record.value.false_positive_rate,
            "avoided_repeat_failures": record.value.avoided_repeat_failures,
        },
    )


def _cost_breakdown(costs: EvidenceBudgetCost, policy: EvidenceBudgetPolicy) -> tuple[float, dict[str, float]]:
    review_cost = costs.human_review_minutes * policy.cost_per_review_minute_usd
    breakdown = {
        "diagnose": costs.cost_to_diagnose_usd,
        "fix": costs.cost_to_fix_usd,
        "prevent_regression": costs.cost_to_prevent_regression_usd,
        "human_review": review_cost,
    }
    return sum(breakdown.values()), breakdown


def _value_breakdown(value: EvidenceBudgetValue, policy: EvidenceBudgetPolicy) -> tuple[float, dict[str, float]]:
    adoption_value = {
        AdoptionOutcome.ADOPTED: policy.adopted_value_usd,
        AdoptionOutcome.PILOT: policy.pilot_adoption_value_usd,
        AdoptionOutcome.UNUSED: 0.0,
        AdoptionOutcome.RETIRED: 0.0,
        AdoptionOutcome.UNKNOWN: 0.0,
    }[value.adoption_outcome]
    failure_value = value.avoided_repeat_failures * policy.value_per_avoided_failure_usd
    false_positive_penalty = (failure_value + adoption_value) * value.false_positive_rate
    breakdown = {
        "avoided_repeat_failures": failure_value,
        "adoption": adoption_value,
        "false_positive_penalty": -false_positive_penalty,
    }
    return max(0.0, sum(breakdown.values())), breakdown


def _blockers(
    record: EvidenceBudgetRecord,
    policy: EvidenceBudgetPolicy,
    value_to_cost_ratio: float,
) -> list[EvidenceBudgetBlocker]:
    blockers: list[EvidenceBudgetBlocker] = []
    if not tuple(ref for ref in record.provenance_refs if ref.strip()):
        blockers.append(EvidenceBudgetBlocker.MISSING_PROVENANCE)
    if not record.authority_ref.strip():
        blockers.append(EvidenceBudgetBlocker.MISSING_AUTHORITY)
    if record.value.evidence_confidence is None or record.value.adoption_outcome is AdoptionOutcome.UNKNOWN:
        blockers.append(EvidenceBudgetBlocker.UNKNOWN_EVIDENCE)
    elif record.value.evidence_confidence < policy.min_confidence:
        blockers.append(EvidenceBudgetBlocker.INSUFFICIENT_PROOF)
    if record.value.false_positive_rate > policy.max_false_positive_rate:
        blockers.append(EvidenceBudgetBlocker.FALSE_POSITIVE_RATE_TOO_HIGH)
    if value_to_cost_ratio < policy.min_value_to_cost_ratio:
        blockers.append(EvidenceBudgetBlocker.COST_EXCEEDS_VALUE)
    if record.value.adoption_outcome in {AdoptionOutcome.UNUSED, AdoptionOutcome.RETIRED}:
        blockers.append(EvidenceBudgetBlocker.NEGATIVE_ADOPTION)
    return blockers


def _decision(
    record: EvidenceBudgetRecord,
    total_cost_usd: float,
    blockers: list[EvidenceBudgetBlocker],
    policy: EvidenceBudgetPolicy,
) -> EvidenceBudgetDecision:
    if (
        EvidenceBudgetBlocker.MISSING_PROVENANCE in blockers
        or EvidenceBudgetBlocker.MISSING_AUTHORITY in blockers
        or EvidenceBudgetBlocker.UNKNOWN_EVIDENCE in blockers
    ):
        return EvidenceBudgetDecision.BLOCKED
    if not blockers:
        return EvidenceBudgetDecision.APPROVE
    if record.value.adoption_outcome is AdoptionOutcome.RETIRED:
        return EvidenceBudgetDecision.RETIRE
    if total_cost_usd >= policy.high_cost_usd and EvidenceBudgetBlocker.COST_EXCEEDS_VALUE in blockers:
        return EvidenceBudgetDecision.RETIRE
    if EvidenceBudgetBlocker.FALSE_POSITIVE_RATE_TOO_HIGH in blockers:
        return EvidenceBudgetDecision.BATCH
    return EvidenceBudgetDecision.DOWNGRADE


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise EvidenceBudgetError(f"{field_name}-missing", f"{field_name} must be non-empty")


def _require_text_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise EvidenceBudgetError(f"{field_name}-invalid", f"{field_name} must be a tuple")
    if not allow_empty and not values:
        raise EvidenceBudgetError(f"{field_name}-missing", f"{field_name} must not be empty")
    for value in values:
        _require_text(value, field_name)


def _require_finite_non_negative(value: float, field_name: str) -> None:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceBudgetError(f"{field_name}-invalid", "must be numeric") from exc
    if not isfinite(number) or number < 0:
        raise EvidenceBudgetError(f"{field_name}-invalid", "must be finite and non-negative")


def _require_ratio(value: float, field_name: str) -> None:
    _require_finite_non_negative(value, field_name)
    if float(value) > 1:
        raise EvidenceBudgetError(f"{field_name}-invalid", "must be between 0 and 1")


def _coerce_enum(enum_type: type[Enum], value: Enum | str, reason: str) -> Enum:
    if isinstance(value, enum_type):
        return value
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return enum_type(str(raw_value))
    except ValueError as exc:
        raise EvidenceBudgetError(reason, f"unsupported value {value!r}") from exc


__all__ = [
    "AdoptionOutcome",
    "EvidenceBudgetBlocker",
    "EvidenceBudgetCost",
    "EvidenceBudgetDecision",
    "EvidenceBudgetError",
    "EvidenceBudgetPolicy",
    "EvidenceBudgetRecord",
    "EvidenceBudgetValue",
    "EvidenceBudgetVerdict",
    "EvidenceMechanismKind",
    "evaluate_evidence_budget",
]
