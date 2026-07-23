"""Fail-closed domain expert review queue runtime."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

logger = logging.getLogger(__name__)


class ReviewDimension(StrEnum):
    """Rubric dimensions domain reviewers can score."""

    SOURCE_ACCURACY = "source_accuracy"
    TONE = "tone"
    POLICY_COMPLIANCE = "policy_compliance"
    CITATION_SUFFICIENCY = "citation_sufficiency"
    RISK_RATING = "risk_rating"
    USEFULNESS = "usefulness"
    CORRECTED_OUTPUT = "corrected_output"


class CorrectionArtifactKind(StrEnum):
    """Downstream artifact kinds a correction may feed after consent gates."""

    EVAL_CASE = "eval_case"
    PREFERENCE_DRAFT = "preference_draft"
    METHOD_CARD = "method_card"
    SOURCE_TOOL_CARD = "source_tool_card"
    DIAGNOSIS_LABEL = "diagnosis_label"
    TRAINING_CANDIDATE = "training_candidate"


REQUIRED_DIMENSIONS: tuple[ReviewDimension, ...] = tuple(ReviewDimension)


@dataclass(frozen=True, slots=True)
class ReviewerAuthority:
    """Authority token proving a reviewer may evaluate a task."""

    reviewer_id: str
    role: str
    domains: tuple[str, ...]
    authority_ref: str
    expires_at_utc: str | None = None

    def active_for(self, domain: str, *, now_utc: datetime | None = None) -> bool:
        """Execute the active for operation.

        Returns:
            bool value produced by active_for().
        """
        if not self.reviewer_id.strip() or not self.authority_ref.strip():
            return False
        if domain not in self.domains and "*" not in self.domains:
            return False
        if self.expires_at_utc is None:
            return True
        now = now_utc or datetime.now(timezone.utc)
        try:
            expires = datetime.fromisoformat(self.expires_at_utc.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return False
        if expires.tzinfo is None:
            logger.warning("Reviewer authority expiry timestamp is naive; authority failed closed")
            return False
        return expires >= now

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ReviewerAuthority(reviewer_id={self.reviewer_id!r}, role={self.role!r}, domains={self.domains!r})"


@dataclass(frozen=True, slots=True)
class DomainReviewCandidate:
    """Output candidate that may be queued for domain-expert review."""

    candidate_id: str
    domain: str
    output_excerpt: str
    source_excerpt: str
    evidence_refs: tuple[str, ...]
    provenance_ref: str
    confidence: float
    safety_state_ref: str
    policy_refs: tuple[str, ...]
    citation_refs: tuple[str, ...]
    risk_hint: str
    correction_consent: tuple[CorrectionArtifactKind, ...]
    internal_pipeline_notes: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DomainReviewCandidate(candidate_id={self.candidate_id!r}, domain={self.domain!r}, output_excerpt={self.output_excerpt!r})"


@dataclass(frozen=True, slots=True)
class ReviewTask:
    """Minimum necessary context shown to a non-coder reviewer."""

    task_id: str
    candidate_id: str
    domain: str
    dimensions: tuple[ReviewDimension, ...]
    context: Mapping[str, object]
    required_artifact_consent: tuple[CorrectionArtifactKind, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ReviewTask(task_id={self.task_id!r}, candidate_id={self.candidate_id!r}, domain={self.domain!r})"


@dataclass(frozen=True, slots=True)
class CorrectionArtifact:
    """Rendered correction artifact proposal."""

    artifact_id: str
    kind: CorrectionArtifactKind
    candidate_id: str
    reviewer_id: str
    summary: str
    evidence_refs: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CorrectionArtifact(artifact_id={self.artifact_id!r}, kind={self.kind!r}, candidate_id={self.candidate_id!r})"


@dataclass(frozen=True, slots=True)
class DomainReviewDecision:
    """Fail-closed decision for queueing or submitting review work."""

    accepted: bool
    blockers: tuple[str, ...] = ()
    task: ReviewTask | None = None
    correction_artifacts: tuple[CorrectionArtifact, ...] = ()
    alerts: tuple[str, ...] = ()
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DomainReviewDecision(accepted={self.accepted!r}, blockers={self.blockers!r}, task={self.task!r})"


@dataclass(frozen=True, slots=True)
class DomainReviewSubmission:
    """Reviewer's completed rubric and corrected output."""

    task_id: str
    reviewer: ReviewerAuthority
    scores: Mapping[ReviewDimension, int]
    corrected_output: str
    rationale: str
    citation_refs: tuple[str, ...]
    rubric_confusions: tuple[str, ...] = ()
    consented_artifacts: tuple[CorrectionArtifactKind, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DomainReviewSubmission(task_id={self.task_id!r}, reviewer={self.reviewer!r}, scores={self.scores!r})"


@dataclass(frozen=True, slots=True)
class CalibrationGoldTask:
    """Gold task used to calibrate reviewer scoring drift."""

    task: ReviewTask
    expected_scores: Mapping[ReviewDimension, int]
    expected_corrected_output_contains: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CalibrationAlert:
    """Reviewer calibration or inter-rater quality alert."""

    kind: str
    reviewer_id: str
    severity: str
    reason: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CalibrationAlert(kind={self.kind!r}, reviewer_id={self.reviewer_id!r}, severity={self.severity!r})"


@dataclass(frozen=True, slots=True)
class ReviewerProfile:
    """Current calibration state for one reviewer."""

    reviewer_id: str
    drift_score: float
    gold_task_count: int
    alerts: tuple[CalibrationAlert, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ReviewerProfile(reviewer_id={self.reviewer_id!r}, drift_score={self.drift_score!r}, gold_task_count={self.gold_task_count!r})"


def _missing_text(value: str) -> bool:
    return not value or not value.strip()


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


class DomainReviewRuntime:
    """Build review queues, calibration alerts, and gated correction artifacts."""

    minimum_confidence = 0.0

    def build_review_task(
        self,
        candidate: DomainReviewCandidate,
        reviewer: ReviewerAuthority | None,
        *,
        now_utc: datetime | None = None,
    ) -> DomainReviewDecision:
        """Return a queue task only when all authority and evidence gates pass.

        Args:
            candidate: Candidate value consumed by build_review_task().
            reviewer: Reviewer value consumed by build_review_task().
            now_utc: Now utc value consumed by build_review_task().

        Returns:
            Newly constructed review task value.
        """
        blockers: list[str] = []
        if _missing_text(candidate.candidate_id):
            blockers.append("candidate_id_missing")
        if _missing_text(candidate.domain):
            blockers.append("domain_missing")
        if _missing_text(candidate.output_excerpt) or _missing_text(candidate.source_excerpt):
            blockers.append("minimum_context_missing")
        if not candidate.evidence_refs:
            blockers.append("evidence_refs_missing")
        if _missing_text(candidate.provenance_ref):
            blockers.append("provenance_missing")
        if candidate.confidence < self.minimum_confidence:
            blockers.append("confidence_unavailable")
        if _missing_text(candidate.safety_state_ref):
            blockers.append("safety_state_missing")
        if not candidate.policy_refs:
            blockers.append("policy_authority_missing")
        if not candidate.citation_refs:
            blockers.append("citation_evidence_missing")
        if reviewer is None or not reviewer.active_for(candidate.domain, now_utc=now_utc):
            blockers.append("reviewer_authority_missing")

        evidence = {
            "candidate_id": candidate.candidate_id,
            "evidence_ref_count": len(candidate.evidence_refs),
            "policy_ref_count": len(candidate.policy_refs),
            "citation_ref_count": len(candidate.citation_refs),
            "consented_artifact_count": len(candidate.correction_consent),
        }
        if blockers:
            return DomainReviewDecision(accepted=False, blockers=_unique(blockers), evidence=evidence)

        task = ReviewTask(
            task_id=f"domain-review:{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
            domain=candidate.domain,
            dimensions=REQUIRED_DIMENSIONS,
            context={
                "output_excerpt": candidate.output_excerpt,
                "source_excerpt": candidate.source_excerpt,
                "evidence_refs": candidate.evidence_refs,
                "provenance_ref": candidate.provenance_ref,
                "confidence": candidate.confidence,
                "safety_state_ref": candidate.safety_state_ref,
                "policy_refs": candidate.policy_refs,
                "citation_refs": candidate.citation_refs,
                "risk_hint": candidate.risk_hint,
            },
            required_artifact_consent=candidate.correction_consent,
        )
        return DomainReviewDecision(accepted=True, task=task, evidence=evidence)

    def submit_review(
        self,
        task: ReviewTask,
        submission: DomainReviewSubmission,
        *,
        now_utc: datetime | None = None,
    ) -> DomainReviewDecision:
        """Render correction artifacts only after fail-closed review validation.

        Args:
            task: Task value consumed by submit_review().
            submission: Submission value consumed by submit_review().
            now_utc: Now utc value consumed by submit_review().

        Returns:
            DomainReviewDecision value produced by submit_review().
        """
        blockers: list[str] = []
        if submission.task_id != task.task_id:
            blockers.append("task_id_mismatch")
        if not submission.reviewer.active_for(task.domain, now_utc=now_utc):
            blockers.append("reviewer_authority_missing")
        missing_dimensions = [dimension.value for dimension in task.dimensions if dimension not in submission.scores]
        if missing_dimensions:
            blockers.append("rubric_scores_missing")
        if any(score < 1 or score > 5 for score in submission.scores.values()):
            blockers.append("rubric_score_out_of_range")
        if _missing_text(submission.corrected_output):
            blockers.append("corrected_output_missing")
        if _missing_text(submission.rationale):
            blockers.append("rationale_missing")
        if not submission.citation_refs:
            blockers.append("citation_evidence_missing")

        allowed = set(task.required_artifact_consent)
        requested = set(submission.consented_artifacts)
        if requested - allowed:
            blockers.append("artifact_consent_not_authorized")

        alerts = tuple(f"rubric_confusion:{item}" for item in submission.rubric_confusions)
        evidence = {
            "candidate_id": task.candidate_id,
            "reviewer_id": submission.reviewer.reviewer_id,
            "missing_dimensions": tuple(missing_dimensions),
            "requested_artifact_count": len(requested),
            "authorized_artifact_count": len(allowed),
        }
        if blockers:
            return DomainReviewDecision(accepted=False, blockers=_unique(blockers), alerts=alerts, evidence=evidence)

        artifacts = tuple(
            CorrectionArtifact(
                artifact_id=f"{kind.value}:{task.candidate_id}:{submission.reviewer.reviewer_id}",
                kind=kind,
                candidate_id=task.candidate_id,
                reviewer_id=submission.reviewer.reviewer_id,
                summary=submission.corrected_output,
                evidence_refs=submission.citation_refs,
            )
            for kind in sorted(requested, key=lambda item: item.value)
        )
        return DomainReviewDecision(
            accepted=True,
            correction_artifacts=artifacts,
            alerts=alerts,
            evidence=evidence,
        )

    def evaluate_gold_task(
        self,
        gold_task: CalibrationGoldTask,
        submission: DomainReviewSubmission,
    ) -> ReviewerProfile:
        """Compare a reviewer submission to a gold task and emit drift alerts.

        Args:
            gold_task: Gold task value consumed by evaluate_gold_task().
            submission: Submission value consumed by evaluate_gold_task().

        Returns:
            ReviewerProfile value produced by evaluate_gold_task().
        """
        score_deltas = [
            abs(submission.scores.get(dimension, 0) - expected)
            for dimension, expected in gold_task.expected_scores.items()
        ]
        drift_score = sum(score_deltas) / max(len(score_deltas), 1)
        alerts: list[CalibrationAlert] = []
        if drift_score >= 1.5:
            alerts.append(
                CalibrationAlert(
                    kind="reviewer_drift",
                    reviewer_id=submission.reviewer.reviewer_id,
                    severity="high",
                    reason=f"gold task score delta {drift_score:.2f}",
                )
            )
        corrected = submission.corrected_output.lower()
        missing_expected = [
            token for token in gold_task.expected_corrected_output_contains if token.lower() not in corrected
        ]
        if missing_expected:
            alerts.append(
                CalibrationAlert(
                    kind="gold_correction_missed",
                    reviewer_id=submission.reviewer.reviewer_id,
                    severity="medium",
                    reason="missing expected correction tokens",
                )
            )
        alerts.extend(
            CalibrationAlert(
                kind="rubric_confusion",
                reviewer_id=submission.reviewer.reviewer_id,
                severity="medium",
                reason=item,
            )
            for item in submission.rubric_confusions
        )
        return ReviewerProfile(
            reviewer_id=submission.reviewer.reviewer_id,
            drift_score=drift_score,
            gold_task_count=1,
            alerts=tuple(alerts),
        )

    def compare_inter_rater(
        self,
        submissions: tuple[DomainReviewSubmission, ...],
        *,
        disagreement_threshold: int = 2,
    ) -> tuple[CalibrationAlert, ...]:
        """Detect inter-rater disagreement across shared dimensions.

        Returns:
            tuple[CalibrationAlert, ...] value produced by compare_inter_rater().
        """
        alerts: list[CalibrationAlert] = []
        if len(submissions) < 2:
            return ()
        reviewers = ",".join(submission.reviewer.reviewer_id for submission in submissions)
        dimensions = set().union(*(submission.scores.keys() for submission in submissions))
        for dimension in dimensions:
            scores = [submission.scores[dimension] for submission in submissions if dimension in submission.scores]
            if scores and max(scores) - min(scores) >= disagreement_threshold:
                alerts.append(
                    CalibrationAlert(
                        kind="inter_rater_disagreement",
                        reviewer_id=reviewers,
                        severity="medium",
                        reason=f"{dimension.value} spread {max(scores) - min(scores)}",
                    )
                )
        return tuple(alerts)
