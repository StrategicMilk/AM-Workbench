"""Structured plan-review outcomes.

The Foreman consumes these records before dispatching any Worker tasks.  The
defaults fail closed so ambiguous, malformed, or unreviewed plans cannot be
treated as approved work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.agents.contracts import AttestedArtifact, OutcomeSignal
from vetinari.types import EvidenceBasis


def utc_now_iso() -> str:
    """Return a compact UTC timestamp for immutable review records."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class PlanDecision(str, Enum):
    """Canonical decision emitted by the Plan Reviewer."""

    APPROVE = "APPROVE"
    REFUSE = "REFUSE"
    NEEDS_REVISION = "NEEDS_REVISION"


class RefusalReason(str, Enum):
    """Structured reasons for non-approval decisions."""

    NON_GOAL_MATCH = "NON_GOAL_MATCH"
    DESTRUCTIVE_WITHOUT_GUARD = "DESTRUCTIVE_WITHOUT_GUARD"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    IFR_UNEXPLORED = "IFR_UNEXPLORED"
    SCOPE_EXCEEDS_BUDGET = "SCOPE_EXCEEDS_BUDGET"
    OTHER = "OTHER"

    @classmethod
    def from_reason_code(cls, code: str) -> RefusalReason:
        """Map UI plan-feedback reason codes to planner refusal reasons.

        Returns:
            RefusalReason value produced by from_reason_code().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        mapping = {
            "TOO_MANY_TASKS": cls.SCOPE_EXCEEDS_BUDGET,
            "MISSING_DEPENDENCY": cls.EVIDENCE_INSUFFICIENT,
            "WRONG_AGENT": cls.OTHER,
            "OUT_OF_SCOPE": cls.NON_GOAL_MATCH,
            "UNSAFE": cls.DESTRUCTIVE_WITHOUT_GUARD,
        }
        try:
            return mapping[str(code)]
        except KeyError as exc:
            raise ValueError(f"unknown reason_code: {code}") from exc


@dataclass(frozen=True, slots=True)
class _UserFeedbackEvidence(OutcomeSignal):
    """Outcome evidence carrying user plan-feedback metadata."""

    plan_id: str = ""
    severity: str = "medium"
    free_text: str = ""
    source: str = "user_plan_feedback_api"

    def __getitem__(self, key: str) -> str:
        """Expose plan-feedback metadata through mapping-style access."""
        values = {
            "plan_id": self.plan_id,
            "severity": self.severity,
            "free_text": self.free_text,
            "source": self.source,
        }
        return values[key]

    def to_user_feedback_dict(self) -> dict[str, str]:
        """Return the plan-feedback evidence metadata."""
        return {
            "plan_id": self.plan_id,
            "severity": self.severity,
            "free_text": self.free_text,
            "source": self.source,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"_UserFeedbackEvidence(plan_id={self.plan_id!r}, severity={self.severity!r}, free_text={self.free_text!r})"
        )


@dataclass(frozen=True, slots=True)
class OverrideAppeal:
    """Human appeal record for a non-goal match."""

    appealed_by: str
    appeal_reason: str
    appealed_at_utc: str = field(default_factory=utc_now_iso)
    matched_non_goal_ids: list[str] = field(default_factory=list)
    attested_artifact: AttestedArtifact | None = None

    def __repr__(self) -> str:
        """Return a compact debug representation keyed by appellant and match count."""
        return (
            "OverrideAppeal("
            f"appealed_by={self.appealed_by!r}, "
            f"matched_non_goal_ids={len(self.matched_non_goal_ids)!r}, "
            f"has_attested_artifact={self.has_attested_artifact!r})"
        )

    def __post_init__(self) -> None:
        if not self.appealed_by.strip():
            raise ValueError("appealed_by must be non-empty")
        if not self.appeal_reason.strip():
            raise ValueError("appeal_reason must be non-empty")
        object.__setattr__(self, "matched_non_goal_ids", list(dict.fromkeys(self.matched_non_goal_ids)))

    @property
    def has_attested_artifact(self) -> bool:
        """Whether this appeal carries a concrete attested artifact."""
        return self.attested_artifact is not None

    def to_dict(self) -> dict[str, Any]:
        """Serialize the appeal to JSON-friendly primitives.

        Returns:
            Dictionary suitable for API responses and persistence.
        """
        artifact = None
        if self.attested_artifact is not None:
            artifact = {
                "kind": self.attested_artifact.kind.value,
                "attested_by": self.attested_artifact.attested_by,
                "attested_at_utc": self.attested_artifact.attested_at_utc,
                "payload": self.attested_artifact.payload,
            }
        return {
            "appealed_by": self.appealed_by,
            "appeal_reason": self.appeal_reason,
            "appealed_at_utc": self.appealed_at_utc,
            "matched_non_goal_ids": list(self.matched_non_goal_ids),
            "attested_artifact": artifact,
        }


@dataclass(frozen=True, slots=True)
class PlanReviewOutcome:
    """Structured, evidence-backed answer from the Plan Reviewer."""

    decision: str | PlanDecision = PlanDecision.REFUSE  # coerced to PlanDecision in __post_init__
    refusal_reasons: list[str | RefusalReason] = field(
        default_factory=lambda: [RefusalReason.OTHER]
    )  # coerced in __post_init__
    citations: list[str] = field(default_factory=list)
    ifr_alternative: str | None = None
    evidence: OutcomeSignal = field(default_factory=OutcomeSignal)
    reviewed_at_utc: str = field(default_factory=utc_now_iso)
    override_appeal: OverrideAppeal | None = None

    def __repr__(self) -> str:
        """Return a compact debug representation keyed by decision and citations."""
        return (
            "PlanReviewOutcome("
            f"decision={self.decision.value!r}, citations={len(self.citations)!r}, "
            f"passed={self.evidence.passed!r})"
        )

    def __post_init__(self) -> None:
        raw_decision = self.decision.value if isinstance(self.decision, Enum) else self.decision
        decision = self.decision if isinstance(self.decision, PlanDecision) else PlanDecision(raw_decision)
        refusal_reasons = [
            reason
            if isinstance(reason, RefusalReason)
            else RefusalReason(reason.value if isinstance(reason, Enum) else reason)
            for reason in self.refusal_reasons
        ]
        if decision is not PlanDecision.APPROVE and not refusal_reasons:
            refusal_reasons = [RefusalReason.OTHER]
        if decision is PlanDecision.APPROVE:
            refusal_reasons = []
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "refusal_reasons", refusal_reasons)
        object.__setattr__(self, "citations", list(dict.fromkeys(self.citations)))

    @classmethod
    def refuse_default(cls) -> PlanReviewOutcome:
        """Build the fail-closed default outcome."""
        return cls()

    @classmethod
    def from_user_feedback(
        cls,
        plan_id: str,
        reason_code: str,
        severity: str,
        free_text: str | None = None,
    ) -> PlanReviewOutcome:
        """Build a refusal outcome from structured user plan feedback.

        Args:
            plan_id: Plan id value consumed by from_user_feedback().
            reason_code: Reason code value consumed by from_user_feedback().
            severity: Severity value consumed by from_user_feedback().
            free_text: Free text value consumed by from_user_feedback().

        Returns:
            PlanReviewOutcome value produced by from_user_feedback().
        """
        feedback_text = free_text or ""
        return cls(
            decision=PlanDecision.REFUSE,
            refusal_reasons=[RefusalReason.from_reason_code(reason_code)],
            citations=[],
            evidence=_UserFeedbackEvidence(
                passed=False,
                basis=EvidenceBasis.HUMAN_ATTESTED,
                issues=(f"user plan feedback reason_code: {reason_code}",),
                suggestions=(feedback_text,) if feedback_text else (),
                use_case="INTENT_CONFIRMATION",
                plan_id=plan_id,
                severity=severity,
                free_text=feedback_text,
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the outcome to JSON-friendly primitives.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        evidence: dict[str, Any] = {
            "passed": self.evidence.passed,
            "score": self.evidence.score,
            "basis": self.evidence.basis.value,
            "issues": list(self.evidence.issues),
            "suggestions": list(self.evidence.suggestions),
        }
        if isinstance(self.evidence, _UserFeedbackEvidence):
            evidence.update(self.evidence.to_user_feedback_dict())
        return {
            "decision": self.decision.value,
            "refusal_reasons": [reason.value for reason in self.refusal_reasons],
            "citations": list(self.citations),
            "ifr_alternative": self.ifr_alternative,
            "evidence": evidence,
            "reviewed_at_utc": self.reviewed_at_utc,
            "override_appeal": self.override_appeal.to_dict() if self.override_appeal else None,
        }


__all__ = [
    "OverrideAppeal",
    "PlanDecision",
    "PlanReviewOutcome",
    "RefusalReason",
    "utc_now_iso",
]
