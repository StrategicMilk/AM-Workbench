"""Deterministic root-cause diagnosis graph for Workbench evidence.

The graph consumes explicit evidence from traces, monitoring signals, evals,
user corrections, source and dataset health, policy blocks, runtime pressure,
and proposal records. It never treats missing evidence or missing authority as a
successful diagnosis; degraded inputs still return a durable next artifact so
operators have a concrete follow-up.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import Enum
from math import isfinite
from typing import Any

from vetinari.clock import utc_now_iso
from vetinari.workbench.monitoring.signals import MonitoringSignal, MonitoringSignalKind, assess_signal


def _require_text(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _utc_now_iso() -> str:
    return utc_now_iso()


def _stable_id(prefix: str, *parts: object) -> str:
    material = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _coerce_tuple(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    return tuple(str(value) for value in values if str(value).strip())


def _coerce_input_kind(value: DiagnosisInputKind | str) -> DiagnosisInputKind:
    raw_value = value.value if isinstance(value, Enum) else value
    return value if isinstance(value, DiagnosisInputKind) else DiagnosisInputKind(raw_value)


class DiagnosisInputKind(str, Enum):
    """Evidence sources that can feed a Workbench diagnosis."""

    TRACE = "trace"
    MONITORING_SIGNAL = "monitoring_signal"
    EVAL_FAILURE = "eval_failure"
    USER_CORRECTION = "user_correction"
    SOURCE_HEALTH = "source_health"
    DATASET_REVISION = "dataset_revision"
    POLICY_BLOCK = "policy_block"
    RUNTIME_PRESSURE = "runtime_pressure"
    PROPOSAL = "proposal"


class DiagnosisCause(str, Enum):
    """Root-cause classes emitted by the graph."""

    RETRIEVAL_MISS = "retrieval_miss"
    STALE_SOURCE = "stale_source"
    CONTEXT_LOSS = "context_loss"
    TOOL_FAILURE = "tool_failure"
    ROUTE_MISMATCH = "route_mismatch"
    POLICY_CONFLICT = "policy_conflict"
    DATA_CONTAMINATION = "data_contamination"
    RUNTIME_CAPACITY = "runtime_capacity"
    USER_AMBIGUITY = "user_ambiguity"
    EVAL_FAILURE = "eval_failure"
    UNKNOWN = "unknown"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class NextArtifactKind(str, Enum):
    """Durable artifacts a diagnosis can route to."""

    EVAL_CASE = "eval_case"
    ANNOTATION_TASK = "annotation_task"
    PROMPT_PATCH = "prompt_patch"
    SOURCE_REFRESH = "source_refresh"
    METHOD_CARD = "method_card"
    TOOL_CARD = "tool_card"
    ROUTE_PROPOSAL = "route_proposal"
    POLICY_PROPOSAL = "policy_proposal"
    REPRO_CAPSULE = "repro_capsule"
    DOCUMENTED_NO_OP = "documented_no_op"


class DiagnosisBlocker(str, Enum):
    """Reasons a diagnosis cannot be trusted as actionable."""

    MISSING_EVIDENCE = "missing_evidence"
    MISSING_PROVENANCE = "missing_provenance"
    MISSING_CONFIDENCE = "missing_confidence"
    INVALID_CONFIDENCE = "invalid_confidence"
    LOW_CONFIDENCE = "low_confidence"
    MISSING_AUTHORITY = "missing_authority"
    SAFETY_STATE_UNAVAILABLE = "safety_state_unavailable"


@dataclass(frozen=True, slots=True)
class DiagnosisEvidence:
    """One explicit evidence reference in the diagnosis graph."""

    kind: DiagnosisInputKind | str
    ref_id: str
    summary: str
    provenance_ref: str = ""
    observed: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.ref_id, "evidence.ref_id")
        _require_text(self.summary, "evidence.summary")
        object.__setattr__(self, "kind", _coerce_input_kind(self.kind))

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["kind"] = DiagnosisInputKind(self.kind).value
        payload["observed"] = dict(self.observed)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DiagnosisEvidence(kind={self.kind!r}, ref_id={self.ref_id!r}, summary={self.summary!r})"


@dataclass(frozen=True, slots=True)
class DiagnosisInput:
    """Runtime facts submitted to the diagnosis graph."""

    project_id: str
    run_id: str
    evidence: tuple[DiagnosisEvidence, ...]
    authority_ref: str
    confidence: float | int | str | None
    safety_reviewed: bool
    observed_at_utc: str
    input_kinds: tuple[DiagnosisInputKind | str, ...] = ()
    signals: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.project_id, "project_id")
        _require_text(self.run_id, "run_id")
        _require_text(self.observed_at_utc, "observed_at_utc")
        for item in self.evidence:
            if not isinstance(item, DiagnosisEvidence):
                raise ValueError("evidence must contain DiagnosisEvidence instances")
        kinds = self.input_kinds or tuple(item.kind for item in self.evidence)
        object.__setattr__(self, "input_kinds", tuple(_coerce_input_kind(kind) for kind in kinds))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DiagnosisInput(project_id={self.project_id!r}, run_id={self.run_id!r}, evidence={self.evidence!r})"


@dataclass(frozen=True, slots=True)
class DiagnosisCandidate:
    """One candidate root cause with confidence and evidence."""

    cause: DiagnosisCause
    confidence: float
    reason: str
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["cause"] = self.cause.value
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DiagnosisCandidate(cause={self.cause!r}, confidence={self.confidence!r}, reason={self.reason!r})"


@dataclass(frozen=True, slots=True)
class NextArtifact:
    """Durable follow-up artifact selected by a diagnosis."""

    kind: NextArtifactKind
    artifact_id: str
    title: str
    rationale: str
    authority_ref: str

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["kind"] = self.kind.value
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"NextArtifact(kind={self.kind!r}, artifact_id={self.artifact_id!r}, title={self.title!r})"


@dataclass(frozen=True, slots=True)
class WorkbenchDiagnosis:
    """Actionability verdict for one Workbench root-cause graph run."""

    schema_version: str
    diagnosis_id: str
    project_id: str
    run_id: str
    cause: DiagnosisCause
    confidence: float
    degraded: bool
    actionable: bool
    blockers: tuple[DiagnosisBlocker, ...]
    evidence_refs: tuple[str, ...]
    input_kinds: tuple[DiagnosisInputKind, ...]
    candidates: tuple[DiagnosisCandidate, ...]
    next_artifact: NextArtifact
    created_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "diagnosis_id": self.diagnosis_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "cause": self.cause.value,
            "confidence": self.confidence,
            "degraded": self.degraded,
            "actionable": self.actionable,
            "blockers": [blocker.value for blocker in self.blockers],
            "evidence_refs": list(self.evidence_refs),
            "input_kinds": [kind.value for kind in self.input_kinds],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "next_artifact": self.next_artifact.to_dict(),
            "created_at_utc": self.created_at_utc,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchDiagnosis(schema_version={self.schema_version!r}, diagnosis_id={self.diagnosis_id!r}, project_id={self.project_id!r})"


class WorkbenchDiagnosisGraph:
    """Classify root causes and route every result to a durable artifact."""

    def __init__(self, *, utc_now: Callable[[], str] | None = None) -> None:
        self._utc_now = utc_now or _utc_now_iso

    def _created_at_utc(self) -> str:
        return getattr(self, "_utc_now", _utc_now_iso)()

    def diagnose(self, diagnosis_input: DiagnosisInput | Mapping[str, Any]) -> WorkbenchDiagnosis:
        """Return an actionable or degraded diagnosis for one run.

        Returns:
            WorkbenchDiagnosis value produced by diagnose().
        """
        if isinstance(diagnosis_input, Mapping):
            diagnosis_input = _input_from_mapping(diagnosis_input)
        blockers = _input_blockers(diagnosis_input)
        if blockers:
            return _degraded_diagnosis(
                diagnosis_input,
                tuple(dict.fromkeys(blockers)),
                created_at_utc=self._created_at_utc(),
            )

        confidence = float(diagnosis_input.confidence)
        candidates = _candidate_causes(diagnosis_input, confidence)
        primary = (
            candidates[0]
            if candidates
            else _candidate(
                DiagnosisCause.UNKNOWN,
                confidence,
                "Evidence was valid but did not match a known root-cause branch.",
                *[item.ref_id for item in diagnosis_input.evidence],
            )
        )
        if primary.confidence < 0.5:
            blockers = (DiagnosisBlocker.LOW_CONFIDENCE,)
            return _degraded_diagnosis(
                diagnosis_input,
                blockers,
                candidate=primary,
                created_at_utc=self._created_at_utc(),
            )
        artifact = _next_artifact_for(primary.cause, diagnosis_input, actionable=True)
        return WorkbenchDiagnosis(
            schema_version="1.0",
            diagnosis_id=_stable_id(
                "diagnosis", diagnosis_input.project_id, diagnosis_input.run_id, primary.cause.value
            ),
            project_id=diagnosis_input.project_id,
            run_id=diagnosis_input.run_id,
            cause=primary.cause,
            confidence=primary.confidence,
            degraded=False,
            actionable=True,
            blockers=(),
            evidence_refs=tuple(item.ref_id for item in diagnosis_input.evidence),
            input_kinds=tuple(_coerce_input_kind(kind) for kind in diagnosis_input.input_kinds),
            candidates=candidates or (primary,),
            next_artifact=artifact,
            created_at_utc=self._created_at_utc(),
        )


def diagnose_workbench_failure(
    diagnosis_input: DiagnosisInput | Mapping[str, Any],
    *,
    graph: WorkbenchDiagnosisGraph | None = None,
) -> WorkbenchDiagnosis:
    """Public entry point for the Workbench diagnosis graph.

    Returns:
        WorkbenchDiagnosis value produced by diagnose_workbench_failure().
    """
    active_graph = graph if graph is not None else WorkbenchDiagnosisGraph()
    return active_graph.diagnose(diagnosis_input)


def diagnosis_input_from_monitoring_signal(
    signal: MonitoringSignal,
    *,
    authority_ref: str,
    confidence: float | int | str | None,
    safety_reviewed: bool,
) -> DiagnosisInput:
    """Adapt a production monitoring signal into the diagnosis graph input.

    Returns:
        DiagnosisInput value produced by diagnosis_input_from_monitoring_signal().
    """
    assessment = assess_signal(signal)
    observed = {
        "monitoring_kind": str(signal.kind.value if isinstance(signal.kind, MonitoringSignalKind) else signal.kind),
        "severity": str(signal.severity.value if hasattr(signal.severity, "value") else signal.severity),
        "score": signal.score,
        "threshold": signal.threshold,
        "routing_hint": signal.routing_hint,
        "alerting": assessment.alerting,
        "assessment_passed": assessment.passed,
        "assessment_blockers": [reason.value for reason in assessment.blockers],
    }
    evidence = DiagnosisEvidence(
        kind=DiagnosisInputKind.MONITORING_SIGNAL,
        ref_id=signal.signal_id,
        summary=f"monitoring signal {signal.signal_id} {observed['monitoring_kind']}",
        provenance_ref=",".join(signal.evidence_refs),
        observed=observed,
    )
    return DiagnosisInput(
        project_id=signal.project_id,
        run_id=signal.run_id,
        evidence=(evidence,),
        authority_ref=authority_ref,
        confidence=confidence,
        safety_reviewed=safety_reviewed,
        observed_at_utc=signal.captured_at_utc,
        input_kinds=(DiagnosisInputKind.MONITORING_SIGNAL,),
        signals=observed,
    )


def _input_from_mapping(data: Mapping[str, Any]) -> DiagnosisInput:
    raw_evidence = data.get("evidence", ())
    evidence = tuple(
        item
        if isinstance(item, DiagnosisEvidence)
        else DiagnosisEvidence(
            kind=item["kind"],
            ref_id=str(item["ref_id"]),
            summary=str(item["summary"]),
            provenance_ref=str(item.get("provenance_ref", "")),
            observed=item.get("observed", {}),
        )
        for item in raw_evidence
    )
    return DiagnosisInput(
        project_id=str(data.get("project_id", "")),
        run_id=str(data.get("run_id", "")),
        evidence=evidence,
        authority_ref=str(data.get("authority_ref", "")),
        confidence=data.get("confidence"),
        safety_reviewed=bool(data.get("safety_reviewed", False)),
        observed_at_utc=str(data.get("observed_at_utc", "")),
        input_kinds=tuple(data.get("input_kinds", ())),
        signals=data.get("signals", {}),
    )


def _input_blockers(diagnosis_input: DiagnosisInput) -> list[DiagnosisBlocker]:
    blockers: list[DiagnosisBlocker] = []
    if not diagnosis_input.evidence:
        blockers.append(DiagnosisBlocker.MISSING_EVIDENCE)
    if not any(item.provenance_ref.strip() for item in diagnosis_input.evidence):
        blockers.append(DiagnosisBlocker.MISSING_PROVENANCE)
    if not diagnosis_input.authority_ref.strip():
        blockers.append(DiagnosisBlocker.MISSING_AUTHORITY)
    if not diagnosis_input.safety_reviewed:
        blockers.append(DiagnosisBlocker.SAFETY_STATE_UNAVAILABLE)
    if diagnosis_input.confidence is None:
        blockers.append(DiagnosisBlocker.MISSING_CONFIDENCE)
    else:
        try:
            confidence = float(diagnosis_input.confidence)
        except (TypeError, ValueError):
            blockers.append(DiagnosisBlocker.INVALID_CONFIDENCE)
        else:
            if not isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
                blockers.append(DiagnosisBlocker.INVALID_CONFIDENCE)
    return blockers


def _candidate_causes(diagnosis_input: DiagnosisInput, confidence: float) -> tuple[DiagnosisCandidate, ...]:
    evidence_text = " ".join([item.summary for item in diagnosis_input.evidence]).lower()
    observed = _flatten_observed(diagnosis_input)
    candidates: list[DiagnosisCandidate] = []
    refs = tuple(item.ref_id for item in diagnosis_input.evidence)

    if _has(observed, evidence_text, "policy_block", "policy conflict", "toxicity", "pii_phi"):
        candidates.append(
            _candidate(DiagnosisCause.POLICY_CONFLICT, confidence, "Policy evidence blocked the run.", *refs)
        )
    if _dataset_revision_mismatch(observed) or _has(observed, evidence_text, "data contamination", "contamination"):
        candidates.append(
            _candidate(
                DiagnosisCause.DATA_CONTAMINATION,
                confidence,
                "Dataset evidence indicates contamination or revision drift.",
                *refs,
            )
        )
    if _has(observed, evidence_text, "retrieval_failure", "retrieval miss", "embedding_shift"):
        candidates.append(
            _candidate(
                DiagnosisCause.RETRIEVAL_MISS,
                confidence,
                "Retrieval evidence indicates the answer missed required context.",
                *refs,
            )
        )
    if _has(observed, evidence_text, "stale_source", "source_freshness=stale", "freshness': 'stale", "stale source"):
        candidates.append(
            _candidate(DiagnosisCause.STALE_SOURCE, confidence, "Source health evidence is stale.", *refs)
        )
    if _has(observed, evidence_text, "tool_call_failure", "unavailable_tool", "tool failure", "connection refused"):
        candidates.append(
            _candidate(
                DiagnosisCause.TOOL_FAILURE,
                confidence,
                "Tool evidence indicates an unavailable or failing tool.",
                *refs,
            )
        )
    if _has(observed, evidence_text, "route mismatch", "routing", "wrong agent", "wrong model"):
        candidates.append(
            _candidate(
                DiagnosisCause.ROUTE_MISMATCH,
                confidence,
                "Routing evidence indicates the wrong route, agent, or model.",
                *refs,
            )
        )
    if _has(observed, evidence_text, "runtime_pressure", "runtime capacity", "endpoint_slo", "capacity", "saturation"):
        candidates.append(
            _candidate(
                DiagnosisCause.RUNTIME_CAPACITY,
                confidence,
                "Runtime pressure evidence crossed capacity guardrails.",
                *refs,
            )
        )
    if _has(observed, evidence_text, "context_loss", "lost context", "missing context"):
        candidates.append(
            _candidate(DiagnosisCause.CONTEXT_LOSS, confidence, "Trace evidence indicates context was lost.", *refs)
        )
    if _has(observed, evidence_text, "user_ambiguity", "ambiguous", "which one", "unclear request"):
        candidates.append(
            _candidate(
                DiagnosisCause.USER_AMBIGUITY, confidence, "User correction evidence has unresolved ambiguity.", *refs
            )
        )
    if DiagnosisInputKind.EVAL_FAILURE in diagnosis_input.input_kinds and not candidates:
        candidates.append(
            _candidate(
                DiagnosisCause.EVAL_FAILURE,
                confidence,
                "Eval failure evidence needs a durable eval case or repro.",
                *refs,
            )
        )
    return tuple(candidates)


def _flatten_observed(diagnosis_input: DiagnosisInput) -> str:
    observed_parts: list[str] = []
    for key, value in diagnosis_input.signals.items():
        observed_parts.append(f"{key}={value}")
    for item in diagnosis_input.evidence:
        for key, value in item.observed.items():
            observed_parts.append(f"{key}={value}")
    return " ".join(observed_parts).lower()


def _has(observed: str, text: str, *needles: str) -> bool:
    return any(needle in observed or needle in text for needle in needles)


def _dataset_revision_mismatch(observed: str) -> bool:
    values: dict[str, str] = {}
    for token in observed.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        values[key.strip()] = value.strip(" ,;")
    dataset_revision = values.get("dataset_revision")
    expected_revision = values.get("expected_dataset_revision")
    if not dataset_revision or not expected_revision:
        return False
    return dataset_revision != expected_revision


def _candidate(cause: DiagnosisCause, confidence: float, reason: str, *refs: str) -> DiagnosisCandidate:
    return DiagnosisCandidate(
        cause=cause,
        confidence=confidence,
        reason=reason,
        evidence_refs=tuple(ref for ref in refs if ref),
    )


def _degraded_diagnosis(
    diagnosis_input: DiagnosisInput,
    blockers: tuple[DiagnosisBlocker, ...],
    *,
    candidate: DiagnosisCandidate | None = None,
    created_at_utc: str | None = None,
) -> WorkbenchDiagnosis:
    evidence_refs = tuple(item.ref_id for item in diagnosis_input.evidence)
    primary = candidate or _candidate(
        DiagnosisCause.INSUFFICIENT_EVIDENCE,
        0.0,
        "Diagnosis cannot be trusted until evidence, provenance, authority, confidence, and safety state are present.",
        *evidence_refs,
    )
    return WorkbenchDiagnosis(
        schema_version="1.0",
        diagnosis_id=_stable_id(
            "diagnosis", diagnosis_input.project_id, diagnosis_input.run_id, primary.cause.value, blockers
        ),
        project_id=diagnosis_input.project_id,
        run_id=diagnosis_input.run_id,
        cause=primary.cause,
        confidence=primary.confidence,
        degraded=True,
        actionable=False,
        blockers=blockers,
        evidence_refs=evidence_refs,
        input_kinds=tuple(_coerce_input_kind(kind) for kind in diagnosis_input.input_kinds),
        candidates=(primary,),
        next_artifact=_next_artifact_for(primary.cause, diagnosis_input, actionable=False),
        created_at_utc=created_at_utc or _utc_now_iso(),
    )


def _next_artifact_for(cause: DiagnosisCause, diagnosis_input: DiagnosisInput, *, actionable: bool) -> NextArtifact:
    if not actionable:
        kind = NextArtifactKind.REPRO_CAPSULE
        title = "Capture missing diagnosis evidence"
        rationale = "The graph failed closed because required evidence or authority state was unavailable."
    else:
        kind = _ARTIFACT_BY_CAUSE.get(cause, NextArtifactKind.DOCUMENTED_NO_OP)
        title = f"{kind.value} for {cause.value}"
        rationale = f"Route {cause.value} diagnosis to durable {kind.value} follow-up."
    return NextArtifact(
        kind=kind,
        artifact_id=_stable_id("artifact", diagnosis_input.project_id, diagnosis_input.run_id, cause.value, kind.value),
        title=title,
        rationale=rationale,
        authority_ref=diagnosis_input.authority_ref,
    )


_ARTIFACT_BY_CAUSE: dict[DiagnosisCause, NextArtifactKind] = {
    DiagnosisCause.RETRIEVAL_MISS: NextArtifactKind.EVAL_CASE,
    DiagnosisCause.STALE_SOURCE: NextArtifactKind.SOURCE_REFRESH,
    DiagnosisCause.CONTEXT_LOSS: NextArtifactKind.PROMPT_PATCH,
    DiagnosisCause.TOOL_FAILURE: NextArtifactKind.TOOL_CARD,
    DiagnosisCause.ROUTE_MISMATCH: NextArtifactKind.ROUTE_PROPOSAL,
    DiagnosisCause.POLICY_CONFLICT: NextArtifactKind.POLICY_PROPOSAL,
    DiagnosisCause.DATA_CONTAMINATION: NextArtifactKind.ANNOTATION_TASK,
    DiagnosisCause.RUNTIME_CAPACITY: NextArtifactKind.TOOL_CARD,
    DiagnosisCause.USER_AMBIGUITY: NextArtifactKind.ANNOTATION_TASK,
    DiagnosisCause.EVAL_FAILURE: NextArtifactKind.EVAL_CASE,
    DiagnosisCause.UNKNOWN: NextArtifactKind.DOCUMENTED_NO_OP,
}


__all__ = [
    "DiagnosisBlocker",
    "DiagnosisCandidate",
    "DiagnosisCause",
    "DiagnosisEvidence",
    "DiagnosisInput",
    "DiagnosisInputKind",
    "NextArtifact",
    "NextArtifactKind",
    "WorkbenchDiagnosis",
    "WorkbenchDiagnosisGraph",
    "diagnose_workbench_failure",
    "diagnosis_input_from_monitoring_signal",
]
