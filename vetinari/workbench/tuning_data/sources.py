"""Fail-closed intake registry for Workbench tuning-data sources.

The registry is intentionally side-effect free. Callers pass already persisted
source cards or collector records into the runtime and receive deterministic
approval decisions before data can become an eval case, route signal,
preference pair, training candidate, or roadmap finding.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar

from vetinari.exceptions import ProvenanceValidationError


class TuningDataSourceRegistryError(ValueError):
    """Raised when source metadata or intake state cannot be trusted."""


class TuningDataSourceKind(str, Enum):
    """Canonical kinds that can enter the tuning-data registry."""

    EXTERNAL_DATASET = "external_dataset"
    INTERNAL_TRACE = "internal_trace"
    FEEDBACK = "feedback"
    WORK_ARTIFACT = "work_artifact"
    SOURCE_CARD = "source_card"
    SYNTHETIC = "synthetic"


class CollectorKind(str, Enum):
    """Collector surfaces allowed to feed the governed intake layer."""

    FEEDBACK_STORE = "feedback_store"
    IMPLICIT_FEEDBACK = "implicit_feedback"
    RECEIPT = "receipt"
    EXECUTION_FEEDBACK = "execution_feedback"
    ROUTE_OUTCOME = "route_outcome"
    RAG_SOURCE_HEALTH = "rag_source_health"
    PLAN_FEEDBACK = "plan_feedback"
    REVIEWED_EXTERNAL_BENCHMARK = "reviewed_external_benchmark"


class TuningDataConsumer(str, Enum):
    """Promotion targets guarded by the intake registry."""

    EVAL_CASE = "eval_case"
    ROUTE_DEFAULT_SIGNAL = "route_default_signal"
    PREFERENCE_PAIR = "preference_pair"
    TRAINING_CANDIDATE = "training_candidate"
    ROADMAP_FINDING = "roadmap_finding"


class SplitName(str, Enum):
    """Split firewall labels used to keep training and evaluation separate."""

    RAW = "raw"
    TRAIN = "train"
    VALIDATION = "validation"
    EVAL_HOLDOUT = "eval_holdout"
    PRODUCTION_FEEDBACK = "production_feedback"


class TuningSourceReviewState(str, Enum):
    """Governance review state for a source row."""

    OBSERVED = "observed"
    REVIEWED = "reviewed"
    REVOKED = "revoked"


class IntakeDecisionStatus(str, Enum):
    """Outcome of checking one source for one consumer."""

    APPROVED = "approved"
    BLOCKED = "blocked"


BLOCKING_TAINTS: frozenset[str] = frozenset({
    "pii_unredacted",
    "license_restricted",
    "consent_missing",
    "split_overlap",
    "prompt_injection_unreviewed",
    "synthetic_unlabeled",
    "revoked",
})

_COLLECTOR_SOURCE_KIND = {
    CollectorKind.FEEDBACK_STORE: TuningDataSourceKind.FEEDBACK,
    CollectorKind.IMPLICIT_FEEDBACK: TuningDataSourceKind.FEEDBACK,
    CollectorKind.RECEIPT: TuningDataSourceKind.WORK_ARTIFACT,
    CollectorKind.EXECUTION_FEEDBACK: TuningDataSourceKind.INTERNAL_TRACE,
    CollectorKind.ROUTE_OUTCOME: TuningDataSourceKind.INTERNAL_TRACE,
    CollectorKind.RAG_SOURCE_HEALTH: TuningDataSourceKind.SOURCE_CARD,
    CollectorKind.PLAN_FEEDBACK: TuningDataSourceKind.WORK_ARTIFACT,
    CollectorKind.REVIEWED_EXTERNAL_BENCHMARK: TuningDataSourceKind.EXTERNAL_DATASET,
}

_CONSUMER_ALLOWED_SPLITS = {
    TuningDataConsumer.EVAL_CASE: frozenset({SplitName.EVAL_HOLDOUT, SplitName.VALIDATION}),
    TuningDataConsumer.ROUTE_DEFAULT_SIGNAL: frozenset({SplitName.VALIDATION, SplitName.PRODUCTION_FEEDBACK}),
    TuningDataConsumer.PREFERENCE_PAIR: frozenset({SplitName.TRAIN, SplitName.PRODUCTION_FEEDBACK}),
    TuningDataConsumer.TRAINING_CANDIDATE: frozenset({SplitName.TRAIN, SplitName.PRODUCTION_FEEDBACK}),
    TuningDataConsumer.ROADMAP_FINDING: frozenset({SplitName.RAW, SplitName.VALIDATION, SplitName.PRODUCTION_FEEDBACK}),
}

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class TuningDataSourceGovernance:
    """Evidence required before a source may feed any tuning consumer."""

    consent_ref: str
    license_ref: str
    privacy_class: str
    pii_classification: str
    freshness_expires_at_utc: str
    safety_review_ref: str
    authority_ref: str
    budget_ref: str
    persisted_ref: str
    review_state: TuningSourceReviewState
    allowed_consumers: tuple[TuningDataConsumer, ...]
    taints: tuple[str, ...] = ()
    revocation_ref: str = ""

    def __post_init__(self) -> None:
        _require_text(self.consent_ref, "consent_ref")
        _require_text(self.license_ref, "license_ref")
        _require_text(self.privacy_class, "privacy_class")
        _require_text(self.pii_classification, "pii_classification")
        _parse_utc(self.freshness_expires_at_utc, "freshness_expires_at_utc")
        _require_text(self.safety_review_ref, "safety_review_ref")
        _require_text(self.authority_ref, "authority_ref")
        _require_text(self.budget_ref, "budget_ref")
        _require_text(self.persisted_ref, "persisted_ref")
        if not isinstance(self.review_state, TuningSourceReviewState):
            raise TuningDataSourceRegistryError("review_state must be a TuningSourceReviewState")
        if not self.allowed_consumers:
            raise TuningDataSourceRegistryError("allowed_consumers must be non-empty")
        for consumer in self.allowed_consumers:
            if not isinstance(consumer, TuningDataConsumer):
                raise TuningDataSourceRegistryError("allowed_consumers must contain TuningDataConsumer values")
        _require_string_tuple(self.taints, "taints", allow_empty=True)
        if self.review_state is TuningSourceReviewState.REVOKED and not self.revocation_ref.strip():
            raise TuningDataSourceRegistryError("revoked sources require revocation_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TuningDataSourceGovernance(consent_ref={self.consent_ref!r}, license_ref={self.license_ref!r}, privacy_class={self.privacy_class!r})"


@dataclass(frozen=True, slots=True)
class TuningDataSource:
    """One governed source row before any training or promotion use."""

    source_id: str
    kind: TuningDataSourceKind
    source_ref: str
    source_revision: str
    observed_at_utc: str
    collected_at_utc: str
    provenance_refs: tuple[str, ...]
    collector_kind: CollectorKind
    split: SplitName
    confidence: float
    governance: TuningDataSourceGovernance
    project_id: str = "default"
    synthetic_declared: bool = False
    reviewed_external_benchmark: bool = False
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_text(self.source_id, "source_id")
        _require_text(self.source_ref, "source_ref")
        _require_text(self.source_revision, "source_revision")
        _parse_utc(self.observed_at_utc, "observed_at_utc")
        _parse_utc(self.collected_at_utc, "collected_at_utc")
        _require_text(self.project_id, "project_id")
        _require_string_tuple(self.provenance_refs, "provenance_refs")
        if not isinstance(self.kind, TuningDataSourceKind):
            raise TuningDataSourceRegistryError("kind must be TuningDataSourceKind")
        if not isinstance(self.collector_kind, CollectorKind):
            raise TuningDataSourceRegistryError("collector_kind must be CollectorKind")
        if not isinstance(self.split, SplitName):
            raise TuningDataSourceRegistryError("split must be SplitName")
        if not 0.0 < self.confidence <= 1.0:
            raise TuningDataSourceRegistryError("confidence must be > 0.0 and <= 1.0")
        if not isinstance(self.governance, TuningDataSourceGovernance):
            raise TuningDataSourceRegistryError("governance must be TuningDataSourceGovernance")
        if self.kind is TuningDataSourceKind.SYNTHETIC and not self.synthetic_declared:
            raise TuningDataSourceRegistryError("synthetic sources must be declared")
        if self.collector_kind is CollectorKind.REVIEWED_EXTERNAL_BENCHMARK and not self.reviewed_external_benchmark:
            raise TuningDataSourceRegistryError("external benchmark collector requires reviewed_external_benchmark")

    def to_dict(self) -> dict[str, Any]:
        """Return a schema-compatible representation of the source row."""
        return {
            "source_id": self.source_id,
            "kind": self.kind.value,
            "source_ref": self.source_ref,
            "source_revision": self.source_revision,
            "observed_at_utc": self.observed_at_utc,
            "collected_at_utc": self.collected_at_utc,
            "provenance_refs": list(self.provenance_refs),
            "collector_kind": self.collector_kind.value,
            "split": self.split.value,
            "confidence": self.confidence,
            "project_id": self.project_id,
            "synthetic_declared": self.synthetic_declared,
            "reviewed_external_benchmark": self.reviewed_external_benchmark,
            "metadata": dict(self.metadata or {}),
            "governance": {
                "consent_ref": self.governance.consent_ref,
                "license_ref": self.governance.license_ref,
                "privacy_class": self.governance.privacy_class,
                "pii_classification": self.governance.pii_classification,
                "freshness_expires_at_utc": self.governance.freshness_expires_at_utc,
                "safety_review_ref": self.governance.safety_review_ref,
                "authority_ref": self.governance.authority_ref,
                "budget_ref": self.governance.budget_ref,
                "persisted_ref": self.governance.persisted_ref,
                "review_state": self.governance.review_state.value,
                "allowed_consumers": [consumer.value for consumer in self.governance.allowed_consumers],
                "taints": list(self.governance.taints),
                "revocation_ref": self.governance.revocation_ref,
            },
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TuningDataSource(source_id={self.source_id!r}, kind={self.kind!r}, source_ref={self.source_ref!r})"


@dataclass(frozen=True, slots=True)
class CollectorRecord:
    """Normalized collector output before governance is attached."""

    record_id: str
    collector_kind: CollectorKind
    source_ref: str
    source_revision: str
    observed_at_utc: str
    collected_at_utc: str
    provenance_refs: tuple[str, ...]
    payload_ref: str
    project_id: str = "default"
    metadata: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_text(self.record_id, "record_id")
        _require_text(self.source_ref, "source_ref")
        _require_text(self.source_revision, "source_revision")
        _parse_utc(self.observed_at_utc, "observed_at_utc")
        _parse_utc(self.collected_at_utc, "collected_at_utc")
        _require_string_tuple(self.provenance_refs, "provenance_refs")
        _require_text(self.payload_ref, "payload_ref")
        _require_text(self.project_id, "project_id")
        if not isinstance(self.collector_kind, CollectorKind):
            raise TuningDataSourceRegistryError("collector_kind must be CollectorKind")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CollectorRecord(record_id={self.record_id!r}, collector_kind={self.collector_kind!r}, source_ref={self.source_ref!r})"


@dataclass(frozen=True, slots=True)
class IntakeDecision:
    """Deterministic decision for a source and target consumer."""

    source_id: str
    consumer: TuningDataConsumer
    status: IntakeDecisionStatus
    blockers: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    allowed_split: SplitName | None

    @property
    def approved(self) -> bool:
        """Return true only for a clean approval decision."""
        return self.status is IntakeDecisionStatus.APPROVED and not self.blockers

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"IntakeDecision(source_id={self.source_id!r}, consumer={self.consumer!r}, status={self.status!r})"


class TuningDataSourceRegistry:
    """In-memory governed registry projection for tuning-data source rows."""

    def __init__(self, sources: Iterable[TuningDataSource] = ()) -> None:
        self._sources: dict[str, TuningDataSource] = {}
        for source in sources:
            self.register_source(source)

    @property
    def sources(self) -> tuple[TuningDataSource, ...]:
        """Return the currently registered source rows."""
        return tuple(self._sources.values())

    def register_source(self, source: TuningDataSource) -> None:
        """Register one source row and reject duplicates.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(source, TuningDataSource):
            raise TuningDataSourceRegistryError("source must be TuningDataSource")
        if source.source_id in self._sources:
            raise TuningDataSourceRegistryError(f"duplicate source_id rejected: {source.source_id}")
        self._sources[source.source_id] = source

    def get_source(self, source_id: str) -> TuningDataSource:
        """Return a source row or fail closed.

        Returns:
            Resolved source value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        _require_text(source_id, "source_id")
        try:
            return self._sources[source_id]
        except KeyError as exc:
            raise TuningDataSourceRegistryError(f"source_id not registered: {source_id}") from exc

    def evaluate_source(
        self,
        source_id: str,
        *,
        consumer: TuningDataConsumer,
        now_utc: datetime | None = None,
    ) -> IntakeDecision:
        """Evaluate whether one source may feed the target consumer.

        Returns:
            IntakeDecision value produced by evaluate_source().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(consumer, TuningDataConsumer):
            raise TuningDataSourceRegistryError("consumer must be TuningDataConsumer")
        source = self.get_source(source_id)
        return evaluate_tuning_data_source(source, consumer=consumer, now_utc=now_utc)

    def require_source_for_consumer(
        self,
        source_id: str,
        *,
        consumer: TuningDataConsumer,
        now_utc: datetime | None = None,
    ) -> TuningDataSource:
        """Return the source only after a clean intake decision and provenance check.

        Runs :func:`evaluate_tuning_data_source` to verify the intake decision
        is approved, then calls :func:`validate_source_provenance` to confirm
        the source has a registered SHA-256 provenance digest.  Both gates must
        pass before the source row is returned to the caller.

        Args:
            source_id: Registered source identifier.
            consumer: The downstream consumer requesting access.
            now_utc: Optional clock override for deterministic testing.

        Returns:
            The governed source row, ready for use by the consumer.

        Raises:
            PermissionError: If the intake decision is not approved.
            ProvenanceValidationError: If the SHA-256 provenance digest is
                absent from ``source.provenance_refs``.
            TuningDataSourceRegistryError: If ``source_id`` is not registered.
        """
        decision = self.evaluate_source(source_id, consumer=consumer, now_utc=now_utc)
        if not decision.approved:
            raise PermissionError(f"tuning data source blocked: {list(decision.blockers)}")
        source = self.get_source(source_id)
        # Provenance gate: fail closed if the SHA-256 digest is not registered.
        validate_source_provenance(source)
        return source


def build_source_from_collector(
    record: CollectorRecord,
    *,
    source_id: str,
    split: SplitName,
    confidence: float,
    governance: TuningDataSourceGovernance,
    synthetic_declared: bool = False,
    reviewed_external_benchmark: bool = False,
) -> TuningDataSource:
    """Build a governed source row from one normalized collector record.

    Returns:
        Newly constructed source from collector value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(record, CollectorRecord):
        raise TuningDataSourceRegistryError("record must be CollectorRecord")
    if record.collector_kind is CollectorKind.REVIEWED_EXTERNAL_BENCHMARK:
        review_ref = str((record.metadata or {}).get("external_benchmark_review_ref", "")).strip()
        review_state = str((record.metadata or {}).get("review_state", "")).strip()
        if not reviewed_external_benchmark or review_state != "reviewed" or not review_ref:
            raise TuningDataSourceRegistryError(
                "reviewed_external_benchmark requires persisted review_state=reviewed and external_benchmark_review_ref"
            )
    return TuningDataSource(
        source_id=source_id,
        kind=_COLLECTOR_SOURCE_KIND[record.collector_kind],
        source_ref=record.source_ref,
        source_revision=record.source_revision,
        observed_at_utc=record.observed_at_utc,
        collected_at_utc=record.collected_at_utc,
        provenance_refs=record.provenance_refs,
        collector_kind=record.collector_kind,
        split=split,
        confidence=confidence,
        governance=governance,
        project_id=record.project_id,
        synthetic_declared=synthetic_declared,
        reviewed_external_benchmark=reviewed_external_benchmark,
        metadata={
            **dict(record.metadata or {}),
            "collector_payload_ref": record.payload_ref,
            "collector_record_id": record.record_id,
        },
    )


def evaluate_tuning_data_source(
    source: TuningDataSource,
    *,
    consumer: TuningDataConsumer,
    now_utc: datetime | None = None,
) -> IntakeDecision:
    """Fail closed unless governance, freshness, split, and allowlist pass.

    Returns:
        IntakeDecision value produced by evaluate_tuning_data_source().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(source, TuningDataSource):
        raise TuningDataSourceRegistryError("source must be TuningDataSource")
    if not isinstance(consumer, TuningDataConsumer):
        raise TuningDataSourceRegistryError("consumer must be TuningDataConsumer")

    now = (now_utc or datetime.now(timezone.utc)).astimezone(timezone.utc)
    governance = source.governance
    blockers: list[str] = []
    evidence_refs = [
        *source.provenance_refs,
        governance.consent_ref,
        governance.license_ref,
        governance.safety_review_ref,
        governance.authority_ref,
        governance.budget_ref,
        governance.persisted_ref,
    ]

    if governance.review_state is not TuningSourceReviewState.REVIEWED:
        blockers.append(f"review_state_{governance.review_state.value}")
    if governance.review_state is TuningSourceReviewState.REVOKED or governance.revocation_ref:
        blockers.append("source_revoked")
    if consumer not in governance.allowed_consumers:
        blockers.append("consumer_not_allowed")
    if source.confidence < 0.7:
        blockers.append("confidence_below_threshold")
    expires_at = _parse_utc(governance.freshness_expires_at_utc, "freshness_expires_at_utc")
    if expires_at <= now:
        blockers.append("source_freshness_expired")
    disallowed_taints = tuple(sorted(set(governance.taints) & BLOCKING_TAINTS))
    blockers.extend(f"blocking_taint:{taint}" for taint in disallowed_taints)
    allowed_splits = _CONSUMER_ALLOWED_SPLITS[consumer]
    if source.split not in allowed_splits:
        blockers.append(f"split_firewall:{source.split.value}_cannot_feed_{consumer.value}")
    if source.kind is TuningDataSourceKind.SYNTHETIC and not source.synthetic_declared:
        blockers.append("synthetic_source_not_declared")
    if source.collector_kind is CollectorKind.REVIEWED_EXTERNAL_BENCHMARK and not source.reviewed_external_benchmark:
        blockers.append("external_benchmark_not_reviewed")

    unique_blockers = tuple(dict.fromkeys(blockers))
    return IntakeDecision(
        source_id=source.source_id,
        consumer=consumer,
        status=IntakeDecisionStatus.BLOCKED if unique_blockers else IntakeDecisionStatus.APPROVED,
        blockers=unique_blockers,
        evidence_refs=tuple(dict.fromkeys(evidence_refs)),
        allowed_split=source.split if source.split in allowed_splits else None,
    )


def validate_source_provenance(source: TuningDataSource) -> None:
    """Verify that the source's SHA-256 provenance digest is declared in provenance_refs.

    Computes a deterministic SHA-256 hex digest over ``source_ref`` and
    ``source_revision`` and checks that the digest appears in the source's
    ``provenance_refs`` tuple. Raises ``ProvenanceValidationError`` if the digest
    is absent so that callers fail closed rather than silently accepting an
    unverified source.

    Args:
        source: The governed source row whose provenance must be verified.

    Raises:
        ProvenanceValidationError: If the computed SHA-256 digest is not present
            in ``source.provenance_refs``.
    """
    digest = hashlib.sha256(f"{source.source_ref}:{source.source_revision}".encode()).hexdigest()
    if digest not in source.provenance_refs:
        raise ProvenanceValidationError(
            f"provenance digest sha256:{digest} not found in provenance_refs "
            f"for source {source.source_id!r}; source must be re-ingested with a "
            "verified provenance ref before it can be used in training or evaluation"
        )


def require_intake_approval(decision: object, callback: Callable[[], T]) -> T:
    """Run a consumer callback only after an explicit clean intake decision.

    Args:
        decision: Decision value consumed by require_intake_approval().
        callback: Callback value consumed by require_intake_approval().

    Returns:
        T value produced by require_intake_approval().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(decision, IntakeDecision) or not decision.approved:
        blockers = getattr(decision, "blockers", ("invalid_intake_decision",))
        raise PermissionError(f"tuning data intake blocked: {list(blockers)}")
    return callback()


def with_revocation(source: TuningDataSource, *, revocation_ref: str) -> TuningDataSource:
    """Return a revoked copy of a source row without mutating registry state.

    Returns:
        TuningDataSource value produced by with_revocation().
    """
    _require_text(revocation_ref, "revocation_ref")
    return replace(
        source,
        governance=replace(
            source.governance,
            review_state=TuningSourceReviewState.REVOKED,
            revocation_ref=revocation_ref,
            taints=tuple(dict.fromkeys((*source.governance.taints, "revoked"))),
        ),
    )


def _parse_utc(value: str, field_name: str) -> datetime:
    _require_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TuningDataSourceRegistryError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise TuningDataSourceRegistryError(f"{field_name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TuningDataSourceRegistryError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise TuningDataSourceRegistryError(f"{field_name} must be a tuple")
    if not values and not allow_empty:
        raise TuningDataSourceRegistryError(f"{field_name} must be non-empty")
    for value in values:
        _require_text(value, f"{field_name} entry")
