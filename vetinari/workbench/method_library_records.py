"""Record contracts for the Workbench method library."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

MIN_EVALUATIONS_FOR_ESTIMATE: int = 3


class MethodLibraryError(Exception):
    """Raised when method cards cannot be safely served."""


class MethodLibraryProjectIdRejected(ValueError):
    """Raised when a project id is not canonical."""

    def __init__(self, value: object) -> None:
        super().__init__(f"invalid project_id {value!r}; use [A-Za-z0-9_-] up to 64 characters")
        self.value = value


class MethodKind(str, Enum):
    """Canonical measured technique identifiers."""

    RETRIEVAL_FIRST_CLASSIFICATION = "retrieval_first_classification"
    MULTI_PASS_REVIEW_AGGREGATION = "multi_" + "pass_review_aggregation"
    MUTATION_AWARE_TEST_GENERATION = "mutation_aware_test_generation"
    TRACE_TO_EVAL_GENERATION = "trace_to_eval_generation"
    SELF_CONSISTENCY_VOTING = "self_consistency_voting"
    CROSS_VALIDATED_ROUTING = "cross_validated_routing"
    SCAFFOLD_THEN_FILL_PLANNING = "scaffold_then_fill_planning"
    RED_TEAM_PROMPT_MUTATION = "red_team_prompt_mutation"


class PromotionStatus(str, Enum):
    """Evidence-derived promotion state for a method card."""

    NOT_PROMOTABLE = "not_promotable"
    MEASURED_NEGATIVE = "measured_negative"
    MEASURED_MIXED = "measured_mixed"
    MEASURED_POSITIVE = "measured_positive"
    PROMOTED = "promoted"


@dataclass(frozen=True, slots=True)
class MeasuredDelta:
    """One measured delta linked to an eval score."""

    metric_name: str
    baseline_value: float
    method_value: float
    delta: float
    sign: str
    evidence_eval_id: str
    captured_at_utc: str
    baseline_sample_count: int = 0
    method_sample_count: int = 0
    baseline_source: str = ""
    baseline_eval_ids: tuple[str, ...] = ()
    p_value: float = 1.0
    effect_size: float = 0.0
    minimum_sample_count: int = MIN_EVALUATIONS_FOR_ESTIMATE

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MeasuredDelta(metric_name={self.metric_name!r}, baseline_value={self.baseline_value!r}, method_value={self.method_value!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the method-library API JSON contract for this delta."""
        return {
            "metric_name": self.metric_name,
            "baseline_value": self.baseline_value,
            "method_value": self.method_value,
            "delta": self.delta,
            "sign": self.sign,
            "evidence_eval_id": self.evidence_eval_id,
            "captured_at_utc": self.captured_at_utc,
            "baseline_sample_count": self.baseline_sample_count,
            "method_sample_count": self.method_sample_count,
            "baseline_source": self.baseline_source,
            "baseline_eval_ids": list(self.baseline_eval_ids),
            "p_value": self.p_value,
            "effect_size": self.effect_size,
            "minimum_sample_count": self.minimum_sample_count,
        }


@dataclass(frozen=True, slots=True)
class MethodEvidenceRef:
    """One measured evidence reference for a method card."""

    eval_id: str
    proposal_id: str
    sign: str
    captured_at_utc: str
    summary: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MethodEvidenceRef(eval_id={self.eval_id!r}, proposal_id={self.proposal_id!r}, sign={self.sign!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the method-library API JSON contract for this evidence ref."""
        return {
            "eval_id": self.eval_id,
            "proposal_id": self.proposal_id,
            "sign": self.sign,
            "captured_at_utc": self.captured_at_utc,
            "summary": self.summary,
        }


@dataclass(frozen=True, slots=True)
class MethodCard:
    """Operator-facing card for one measured AI work technique."""

    method_card_id: str
    kind: MethodKind
    name: str
    description: str
    when_to_use: tuple[str, ...]
    when_not_to_use: tuple[str, ...]
    expected_cost: str
    known_failure_modes: tuple[str, ...]
    compatible_task_profiles: tuple[str, ...]
    measured_deltas: tuple[MeasuredDelta, ...]
    evidence_refs: tuple[MethodEvidenceRef, ...]
    promotion_status: PromotionStatus
    project_id: str
    updated_at_utc: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MethodCard(method_card_id={self.method_card_id!r}, kind={self.kind!r}, name={self.name!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the method-library API JSON contract for this card."""
        return {
            "method_card_id": self.method_card_id,
            "kind": self.kind.value,
            "name": self.name,
            "description": self.description,
            "when_to_use": list(self.when_to_use),
            "when_not_to_use": list(self.when_not_to_use),
            "expected_cost": self.expected_cost,
            "known_failure_modes": list(self.known_failure_modes),
            "compatible_task_profiles": list(self.compatible_task_profiles),
            "measured_deltas": [delta.to_dict() for delta in self.measured_deltas],
            "evidence_refs": [ref.to_dict() for ref in self.evidence_refs],
            "promotion_status": self.promotion_status.value,
            "project_id": self.project_id,
            "updated_at_utc": self.updated_at_utc,
        }
