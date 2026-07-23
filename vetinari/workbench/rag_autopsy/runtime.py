"""Retrieval-stage autopsy and critical-answer reuse policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from vetinari.workbench.source_health import SourceHealthReport


class AutopsyStage(str, Enum):
    """Stages replayed by the retrieval autopsy."""

    QUERY = "query"
    RETRIEVAL = "retrieval"
    RERANK = "rerank"
    CONTEXT = "context"
    ANSWER = "answer"


class AutopsyFailureLabel(str, Enum):
    """Machine-readable labels for stage-level retrieval failures."""

    QUERY_MISSING = "query_missing"
    EVIDENCE_MISSING = "evidence_missing"
    SOURCE_NOT_RETRIEVED = "source_not_retrieved"
    SOURCE_DROPPED_BY_RERANK = "source_dropped_by_rerank"
    SOURCE_DROPPED_FROM_CONTEXT = "source_dropped_from_context"
    ANSWER_CITATION_MISMATCH = "answer_citation_mismatch"
    SOURCE_HEALTH_DEGRADED = "source_health_degraded"


@dataclass(frozen=True, slots=True)
class RetrievalAutopsyInput:
    """Replayable retrieval facts for the five-stage autopsy."""

    query_text: str
    expected_source_ids: tuple[str, ...]
    retrieved_source_ids: tuple[str, ...]
    reranked_source_ids: tuple[str, ...]
    context_source_ids: tuple[str, ...]
    answer_text: str
    answer_cited_source_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetrievalAutopsyInput(query_text={self.query_text!r}, expected_source_ids={self.expected_source_ids!r}, retrieved_source_ids={self.retrieved_source_ids!r})"


@dataclass(frozen=True, slots=True)
class StageScore:
    """One autopsy stage score plus its failure labels."""

    stage: AutopsyStage
    score: float
    evidence_refs: tuple[str, ...]
    failure_labels: tuple[AutopsyFailureLabel, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"StageScore(stage={self.stage!r}, score={self.score!r}, evidence_refs={self.evidence_refs!r})"


@dataclass(frozen=True, slots=True)
class RetrievalAutopsy:
    """Full query-to-answer replay result."""

    autopsy_id: str
    project_id: str
    passed: bool
    overall_score: float
    stages: tuple[StageScore, ...]
    failure_labels: tuple[AutopsyFailureLabel, ...]
    source_health_report_id: str
    model_routing_hints: tuple[str, ...]
    eval_refs: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"RetrievalAutopsy(autopsy_id={self.autopsy_id!r}, project_id={self.project_id!r}, passed={self.passed!r})"
        )


@dataclass(frozen=True, slots=True)
class CriticalAnswerPolicy:
    """Policy thresholds for bypassing generation with a critical answer card."""

    min_confidence: float = 0.9
    min_autopsy_score: float = 0.85
    require_policy_approval: bool = True


@dataclass(frozen=True, slots=True)
class CriticalAnswerCard:
    """Stable answer candidate that may bypass generation only after governance checks."""

    card_id: str
    question: str
    answer: str
    source_ids: tuple[str, ...]
    confidence: float | None
    autopsy: RetrievalAutopsy
    source_health: SourceHealthReport
    evidence_refs: tuple[str, ...]
    authority_refs: tuple[str, ...]
    safety_refs: tuple[str, ...]
    policy_approved: bool
    expires_at_utc: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CriticalAnswerCard(card_id={self.card_id!r}, question={self.question!r}, answer={self.answer!r})"


@dataclass(frozen=True, slots=True)
class CriticalAnswerDecision:
    """Decision returned to RAG callers before generation bypass."""

    card_id: str
    bypass_allowed: bool
    blockers: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CriticalAnswerDecision(card_id={self.card_id!r}, bypass_allowed={self.bypass_allowed!r}, blockers={self.blockers!r})"


def replay_retrieval_autopsy(
    *,
    autopsy_id: str,
    project_id: str,
    replay: RetrievalAutopsyInput,
    source_health: SourceHealthReport,
) -> RetrievalAutopsy:
    """Replay query, retrieval, rerank, context, and answer stages.

    Returns:
        RetrievalAutopsy value produced by replay_retrieval_autopsy().
    """
    _require_text(autopsy_id, "autopsy_id")
    _require_text(project_id, "project_id")
    expected = set(replay.expected_source_ids)
    stages = (
        _stage_query(replay),
        _stage_membership(
            AutopsyStage.RETRIEVAL,
            expected,
            set(replay.retrieved_source_ids),
            replay.evidence_refs,
            AutopsyFailureLabel.SOURCE_NOT_RETRIEVED,
        ),
        _stage_membership(
            AutopsyStage.RERANK,
            expected,
            set(replay.reranked_source_ids),
            replay.evidence_refs,
            AutopsyFailureLabel.SOURCE_DROPPED_BY_RERANK,
        ),
        _stage_membership(
            AutopsyStage.CONTEXT,
            expected,
            set(replay.context_source_ids),
            replay.evidence_refs,
            AutopsyFailureLabel.SOURCE_DROPPED_FROM_CONTEXT,
        ),
        _stage_answer(replay, expected),
    )
    labels = [label for stage in stages for label in stage.failure_labels]
    if source_health.degraded:
        labels.append(AutopsyFailureLabel.SOURCE_HEALTH_DEGRADED)
    overall = min(stage.score for stage in stages) if stages else 0.0
    passed = overall >= 0.85 and not labels and source_health.passed
    return RetrievalAutopsy(
        autopsy_id=autopsy_id,
        project_id=project_id,
        passed=passed,
        overall_score=overall,
        stages=stages,
        failure_labels=tuple(dict.fromkeys(labels)),
        source_health_report_id=source_health.report_id,
        model_routing_hints=("critical_answer_candidate",) if passed else ("force_generation_with_retrieval_repair",),
        eval_refs=(f"retrieval-autopsy-eval:{autopsy_id}",),
    )


def evaluate_critical_answer_card(
    card: CriticalAnswerCard,
    *,
    now_utc: datetime | None = None,
    policy: CriticalAnswerPolicy | None = None,
) -> CriticalAnswerDecision:
    """Allow generation bypass only when evidence, authority, health, and policy are clean.

    Returns:
        CriticalAnswerDecision value produced by evaluate_critical_answer_card().
    """
    selected_policy = policy or CriticalAnswerPolicy()
    blockers: list[str] = []
    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not card.question.strip():
        blockers.append("missing_question")
    if not card.answer.strip():
        blockers.append("missing_answer")
    if not card.source_ids:
        blockers.append("missing_source_ids")
    if not card.evidence_refs:
        blockers.append("missing_evidence")
    if not card.authority_refs:
        blockers.append("missing_authority")
    if not card.safety_refs:
        blockers.append("missing_safety")
    if card.confidence is None or card.confidence < selected_policy.min_confidence:
        blockers.append("confidence_below_policy")
    if selected_policy.require_policy_approval and not card.policy_approved:
        blockers.append("policy_approval_missing")
    if not card.source_health.passed:
        blockers.append("source_health_degraded")
    if card.source_ids and not set(card.source_ids).issubset(set(card.source_health.source_card_refs)):
        blockers.append("source_ids_not_health_verified")
    if not card.autopsy.passed or card.autopsy.overall_score < selected_policy.min_autopsy_score:
        blockers.append("retrieval_autopsy_failed")
    try:
        expires_at = datetime.fromisoformat(card.expires_at_utc.replace("Z", "+00:00"))
    except ValueError:
        blockers.append("expiry_unreadable")
    else:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at.astimezone(timezone.utc) <= now:
            blockers.append("card_expired")
    return CriticalAnswerDecision(
        card_id=card.card_id,
        bypass_allowed=not blockers,
        blockers=tuple(dict.fromkeys(blockers)),
        evidence_refs=tuple(dict.fromkeys(card.evidence_refs + card.authority_refs + card.safety_refs)),
    )


def _stage_query(replay: RetrievalAutopsyInput) -> StageScore:
    labels: list[AutopsyFailureLabel] = []
    if not replay.query_text.strip():
        labels.append(AutopsyFailureLabel.QUERY_MISSING)
    if not replay.evidence_refs:
        labels.append(AutopsyFailureLabel.EVIDENCE_MISSING)
    return StageScore(
        stage=AutopsyStage.QUERY,
        score=0.0 if labels else 1.0,
        evidence_refs=replay.evidence_refs or ("autopsy:evidence-missing",),
        failure_labels=tuple(labels),
    )


def _stage_membership(
    stage: AutopsyStage,
    expected: set[str],
    actual: set[str],
    evidence_refs: tuple[str, ...],
    label: AutopsyFailureLabel,
) -> StageScore:
    if not expected:
        return StageScore(
            stage,
            0.0,
            evidence_refs or ("autopsy:expected-source-missing",),
            (AutopsyFailureLabel.EVIDENCE_MISSING,),
        )
    score = len(expected & actual) / len(expected)
    return StageScore(
        stage=stage,
        score=score,
        evidence_refs=evidence_refs or (f"autopsy:{stage.value}:evidence-missing",),
        failure_labels=() if score == 1.0 else (label,),
    )


def _stage_answer(replay: RetrievalAutopsyInput, expected: set[str]) -> StageScore:
    labels: list[AutopsyFailureLabel] = []
    if (
        not replay.answer_text.strip()
        or not replay.answer_cited_source_ids
        or expected - set(replay.answer_cited_source_ids)
    ):
        labels.append(AutopsyFailureLabel.ANSWER_CITATION_MISMATCH)
    return StageScore(
        stage=AutopsyStage.ANSWER,
        score=0.0 if labels else 1.0,
        evidence_refs=replay.evidence_refs or ("autopsy:answer:evidence-missing",),
        failure_labels=tuple(labels),
    )


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


__all__ = [
    "AutopsyFailureLabel",
    "AutopsyStage",
    "CriticalAnswerCard",
    "CriticalAnswerDecision",
    "CriticalAnswerPolicy",
    "RetrievalAutopsy",
    "RetrievalAutopsyInput",
    "StageScore",
    "evaluate_critical_answer_card",
    "replay_retrieval_autopsy",
]
