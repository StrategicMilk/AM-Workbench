"""Fail-closed mining of governed before/after learning cases.

The miner is import-safe and writes no shared state. Callers provide explicit
artifact records, and the runtime returns either governed pairs or a typed
degraded result describing why mining could not be trusted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

from vetinari.workbench.weaving import WeavingAuthorityLevel


class ArtifactPairMinerError(ValueError):
    """Raised when a before/after pair cannot be trusted."""


class ArtifactPairSourceKind(str, Enum):
    """Sources that can produce a learning-quality before/after pair."""

    FAILING_TEST_FIX = "failing_test_fix"
    PR_COMMENT_PATCH = "pr_comment_patch"
    AUDIT_FINDING_REMEDIATION = "audit_finding_remediation"
    PLAN_CORRECTION = "plan_correction"
    REJECTED_OUTPUT_APPROVED_OUTPUT = "rejected_output_approved_output"


class ArtifactPairConsumer(str, Enum):
    """Downstream consumers allowed to read a governed pair."""

    EVAL_CASE = "eval_case"
    TRAINING_CANDIDATE = "training_candidate"
    GOVERNANCE_REVIEW = "governance_review"


class PairReviewStatus(str, Enum):
    """Reviewer state for using a pair as learning data."""

    UNREVIEWED = "unreviewed"
    APPROVED_FOR_EVAL = "approved_for_eval"
    APPROVED_FOR_TRAINING = "approved_for_training"
    REJECTED = "rejected"


class ArtifactPairTaint(str, Enum):
    """Taints that can restrict learning-data consumers."""

    SENSITIVE_DATA = "sensitive_data"
    LICENSE_RESTRICTED = "license_restricted"
    USER_PRIVATE = "user_private"
    CONTAMINATION_RISK = "contamination_risk"
    SAFETY_RELEVANT = "safety_relevant"
    LOW_CONFIDENCE = "low_confidence"
    UNTRUSTED_PROVENANCE = "untrusted_provenance"


class MiningStatus(str, Enum):
    """Top-level mining result state."""

    READY = "ready"
    DEGRADED = "degraded"


_AUTHORITY_RANK = {
    WeavingAuthorityLevel.OBSERVED: 1,
    WeavingAuthorityLevel.SUGGESTED: 2,
    WeavingAuthorityLevel.PROPOSED: 3,
    WeavingAuthorityLevel.APPROVED: 4,
    WeavingAuthorityLevel.EXECUTED: 5,
    WeavingAuthorityLevel.PROMOTED: 6,
}

_TRAINING_BLOCKING_TAINTS = {
    ArtifactPairTaint.SENSITIVE_DATA,
    ArtifactPairTaint.LICENSE_RESTRICTED,
    ArtifactPairTaint.USER_PRIVATE,
    ArtifactPairTaint.CONTAMINATION_RISK,
    ArtifactPairTaint.UNTRUSTED_PROVENANCE,
}


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ArtifactPairMinerError(f"{field_name} must be non-empty")


def _require_string_tuple(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    if isinstance(values, str) or not values:
        raise ArtifactPairMinerError(f"{field_name} must be a non-empty sequence")
    normalized: list[str] = []
    for value in values:
        _require_text(value, field_name)
        normalized.append(value)
    return tuple(normalized)


def _enum_value(value: Enum | str) -> str:
    return value.value if isinstance(value, Enum) else value


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    """One before or after artifact with governance-critical metadata."""

    artifact_id: str
    kind: str
    content_ref: str
    summary: str
    captured_at_utc: str
    evidence_refs: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    persisted_ref: str
    authority_level: WeavingAuthorityLevel
    confidence: float
    safety_classification: str
    budget_ref: str

    def __post_init__(self) -> None:
        _require_text(self.artifact_id, "artifact_id")
        _require_text(self.kind, "kind")
        _require_text(self.content_ref, "content_ref")
        _require_text(self.summary, "summary")
        _require_text(self.captured_at_utc, "captured_at_utc")
        _require_text(self.persisted_ref, "persisted_ref")
        _require_text(self.safety_classification, "safety_classification")
        _require_text(self.budget_ref, "budget_ref")
        object.__setattr__(self, "evidence_refs", _require_string_tuple(self.evidence_refs, "evidence_refs"))
        object.__setattr__(self, "provenance_refs", _require_string_tuple(self.provenance_refs, "provenance_refs"))
        if not isinstance(self.authority_level, WeavingAuthorityLevel):
            raise ArtifactPairMinerError("authority_level must be WeavingAuthorityLevel")
        if not 0.0 < self.confidence <= 1.0:
            raise ArtifactPairMinerError("confidence must be > 0.0 and <= 1.0")
        if self.safety_classification == "unknown":
            raise ArtifactPairMinerError("safety_classification must be classified")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-schema-compatible snapshot."""
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "content_ref": self.content_ref,
            "summary": self.summary,
            "captured_at_utc": self.captured_at_utc,
            "evidence_refs": list(self.evidence_refs),
            "provenance_refs": list(self.provenance_refs),
            "persisted_ref": self.persisted_ref,
            "authority_level": self.authority_level.value,
            "confidence": self.confidence,
            "safety_classification": self.safety_classification,
            "budget_ref": self.budget_ref,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ArtifactSnapshot(artifact_id={self.artifact_id!r}, kind={self.kind!r}, content_ref={self.content_ref!r})"
        )


@dataclass(frozen=True, slots=True)
class BeforeAfterArtifactPair:
    """A reviewer-governed learning case candidate."""

    pair_id: str
    source_kind: ArtifactPairSourceKind
    before: ArtifactSnapshot
    after: ArtifactSnapshot
    after_won_reason: str
    reviewer_status: PairReviewStatus
    reviewer_ref: str
    allowed_consumers: tuple[ArtifactPairConsumer, ...]
    taints: tuple[ArtifactPairTaint, ...]
    model_version: str
    task_shape: str
    created_at_utc: str

    def __post_init__(self) -> None:
        _require_text(self.pair_id, "pair_id")
        _require_text(self.after_won_reason, "after_won_reason")
        _require_text(self.reviewer_ref, "reviewer_ref")
        _require_text(self.model_version, "model_version")
        _require_text(self.task_shape, "task_shape")
        _require_text(self.created_at_utc, "created_at_utc")
        if not isinstance(self.source_kind, ArtifactPairSourceKind):
            raise ArtifactPairMinerError("source_kind must be ArtifactPairSourceKind")
        if not isinstance(self.before, ArtifactSnapshot) or not isinstance(self.after, ArtifactSnapshot):
            raise ArtifactPairMinerError("before and after must be ArtifactSnapshot")
        if self.before.artifact_id == self.after.artifact_id:
            raise ArtifactPairMinerError("before and after artifacts must be distinct")
        if not isinstance(self.reviewer_status, PairReviewStatus):
            raise ArtifactPairMinerError("reviewer_status must be PairReviewStatus")
        if not self.allowed_consumers:
            raise ArtifactPairMinerError("allowed_consumers must be non-empty")
        for consumer in self.allowed_consumers:
            if not isinstance(consumer, ArtifactPairConsumer):
                raise ArtifactPairMinerError("allowed_consumers entries must be ArtifactPairConsumer")
        for taint in self.taints:
            if not isinstance(taint, ArtifactPairTaint):
                raise ArtifactPairMinerError("taints entries must be ArtifactPairTaint")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-schema-compatible pair payload."""
        return {
            "pair_id": self.pair_id,
            "source_kind": self.source_kind.value,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "after_won_reason": self.after_won_reason,
            "reviewer_status": self.reviewer_status.value,
            "reviewer_ref": self.reviewer_ref,
            "allowed_consumers": [consumer.value for consumer in self.allowed_consumers],
            "taints": [taint.value for taint in self.taints],
            "model_version": self.model_version,
            "task_shape": self.task_shape,
            "created_at_utc": self.created_at_utc,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BeforeAfterArtifactPair(pair_id={self.pair_id!r}, source_kind={self.source_kind!r}, before={self.before!r})"


@dataclass(frozen=True, slots=True)
class ArtifactPairDecision:
    """Consumer-specific decision for a before/after pair."""

    pair_id: str
    consumer: ArtifactPairConsumer
    approved: bool
    blockers: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    provenance_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-schema-compatible decision."""
        return {
            "pair_id": self.pair_id,
            "consumer": self.consumer.value,
            "approved": self.approved,
            "blockers": list(self.blockers),
            "evidence_refs": list(self.evidence_refs),
            "provenance_refs": list(self.provenance_refs),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ArtifactPairDecision(pair_id={self.pair_id!r}, consumer={self.consumer!r}, approved={self.approved!r})"


@dataclass(frozen=True, slots=True)
class MiningResult:
    """Result of parsing raw records into governed pairs."""

    status: MiningStatus
    pairs: tuple[BeforeAfterArtifactPair, ...]
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-schema-compatible mining result."""
        return {
            "status": self.status.value,
            "pairs": [pair.to_dict() for pair in self.pairs],
            "blockers": list(self.blockers),
        }


def evaluate_artifact_pair(
    pair: BeforeAfterArtifactPair,
    consumer: ArtifactPairConsumer = ArtifactPairConsumer.EVAL_CASE,
    *,
    minimum_confidence: float = 0.85,
    minimum_authority: WeavingAuthorityLevel = WeavingAuthorityLevel.APPROVED,
) -> ArtifactPairDecision:
    """Evaluate whether a pair may flow to one governed consumer.

    Args:
        pair: Pair value consumed by evaluate_artifact_pair().
        consumer: Consumer value consumed by evaluate_artifact_pair().
        minimum_confidence: Minimum confidence value consumed by evaluate_artifact_pair().
        minimum_authority: Minimum authority value consumed by evaluate_artifact_pair().

    Returns:
        ArtifactPairDecision value produced by evaluate_artifact_pair().
    """
    blockers: list[str] = []
    if consumer not in pair.allowed_consumers:
        blockers.append("consumer_not_allowed")
    if pair.after.confidence < minimum_confidence:
        blockers.append("after_confidence_below_threshold")
    if pair.after.confidence < pair.before.confidence:
        blockers.append("after_confidence_below_before")
    if _AUTHORITY_RANK[pair.after.authority_level] < _AUTHORITY_RANK[minimum_authority]:
        blockers.append("after_authority_below_minimum")
    if pair.reviewer_status is PairReviewStatus.REJECTED:
        blockers.append("reviewer_rejected")
    if consumer is ArtifactPairConsumer.EVAL_CASE and pair.reviewer_status not in {
        PairReviewStatus.APPROVED_FOR_EVAL,
        PairReviewStatus.APPROVED_FOR_TRAINING,
    }:
        blockers.append("reviewer_eval_approval_required")
    if consumer is ArtifactPairConsumer.TRAINING_CANDIDATE:
        if pair.reviewer_status is not PairReviewStatus.APPROVED_FOR_TRAINING:
            blockers.append("reviewer_training_approval_required")
        if _TRAINING_BLOCKING_TAINTS.intersection(pair.taints):
            blockers.append("training_blocked_by_taint")

    evidence_refs = tuple(dict.fromkeys((*pair.before.evidence_refs, *pair.after.evidence_refs)))
    provenance_refs = tuple(dict.fromkeys((*pair.before.provenance_refs, *pair.after.provenance_refs)))
    unique_blockers = tuple(dict.fromkeys(blockers))
    return ArtifactPairDecision(
        pair_id=pair.pair_id,
        consumer=consumer,
        approved=not unique_blockers,
        blockers=unique_blockers,
        evidence_refs=evidence_refs,
        provenance_refs=provenance_refs,
    )


def artifact_pair_to_eval_case(pair: BeforeAfterArtifactPair) -> dict[str, Any]:
    """Convert an approved pair into an eval-case payload.

    Returns:
        dict[str, Any] value produced by artifact_pair_to_eval_case().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    decision = evaluate_artifact_pair(pair, ArtifactPairConsumer.EVAL_CASE)
    if not decision.approved:
        raise PermissionError(f"artifact pair blocked for eval: {list(decision.blockers)}")
    return {
        "eval_case_id": f"eval:{pair.pair_id}",
        "source_pair_id": pair.pair_id,
        "task_shape": pair.task_shape,
        "model_version": pair.model_version,
        "before_ref": pair.before.content_ref,
        "after_ref": pair.after.content_ref,
        "expected_improvement": pair.after_won_reason,
        "evidence_refs": list(decision.evidence_refs),
        "provenance_refs": list(decision.provenance_refs),
    }


def artifact_pair_to_training_candidate(pair: BeforeAfterArtifactPair) -> dict[str, Any]:
    """Convert an approved pair into a training-candidate payload.

    Returns:
        dict[str, Any] value produced by artifact_pair_to_training_candidate().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    decision = evaluate_artifact_pair(pair, ArtifactPairConsumer.TRAINING_CANDIDATE)
    if not decision.approved:
        raise PermissionError(f"artifact pair blocked for training: {list(decision.blockers)}")
    return {
        "training_candidate_id": f"training:{pair.pair_id}",
        "source_pair_id": pair.pair_id,
        "task_shape": pair.task_shape,
        "model_version": pair.model_version,
        "before_ref": pair.before.content_ref,
        "after_ref": pair.after.content_ref,
        "evidence_refs": list(decision.evidence_refs),
        "provenance_refs": list(decision.provenance_refs),
        "reviewer_ref": pair.reviewer_ref,
    }


def mine_artifact_pair_candidates(records: Iterable[Mapping[str, Any]]) -> MiningResult:
    """Parse raw candidate records and fail closed on any untrusted record.

    Returns:
        MiningResult value produced by mine_artifact_pair_candidates().
    """
    pairs: list[BeforeAfterArtifactPair] = []
    blockers: list[str] = []
    saw_record = False
    for index, record in enumerate(records):
        saw_record = True
        try:
            pairs.append(_pair_from_mapping(record))
        except (ArtifactPairMinerError, KeyError, TypeError, ValueError) as exc:
            blockers.append(f"record_{index}_untrusted:{exc}")
    if not saw_record:
        blockers.append("no_candidate_records")
    if blockers:
        return MiningResult(status=MiningStatus.DEGRADED, pairs=(), blockers=tuple(blockers))
    return MiningResult(status=MiningStatus.READY, pairs=tuple(pairs), blockers=())


def _snapshot_from_mapping(payload: Mapping[str, Any]) -> ArtifactSnapshot:
    return ArtifactSnapshot(
        artifact_id=str(payload["artifact_id"]),
        kind=str(payload["kind"]),
        content_ref=str(payload["content_ref"]),
        summary=str(payload["summary"]),
        captured_at_utc=str(payload["captured_at_utc"]),
        evidence_refs=tuple(str(value) for value in payload["evidence_refs"]),
        provenance_refs=tuple(str(value) for value in payload["provenance_refs"]),
        persisted_ref=str(payload["persisted_ref"]),
        authority_level=WeavingAuthorityLevel(str(payload["authority_level"])),
        confidence=float(payload["confidence"]),
        safety_classification=str(payload["safety_classification"]),
        budget_ref=str(payload["budget_ref"]),
    )


def _pair_from_mapping(payload: Mapping[str, Any]) -> BeforeAfterArtifactPair:
    return BeforeAfterArtifactPair(
        pair_id=str(payload["pair_id"]),
        source_kind=ArtifactPairSourceKind(str(payload["source_kind"])),
        before=_snapshot_from_mapping(payload["before"]),
        after=_snapshot_from_mapping(payload["after"]),
        after_won_reason=str(payload["after_won_reason"]),
        reviewer_status=PairReviewStatus(str(payload["reviewer_status"])),
        reviewer_ref=str(payload["reviewer_ref"]),
        allowed_consumers=tuple(ArtifactPairConsumer(_enum_value(value)) for value in payload["allowed_consumers"]),
        taints=tuple(ArtifactPairTaint(_enum_value(value)) for value in payload.get("taints", ())),
        model_version=str(payload["model_version"]),
        task_shape=str(payload["task_shape"]),
        created_at_utc=str(payload["created_at_utc"]),
    )
