"""Failure-intelligence autopsy classifier and append-only store.

The classifier is intentionally deterministic: it uses only explicit failure
signals supplied by trace, console, method-card, source/tool-card, or
mission-control callers. Missing evidence returns a degraded autopsy with a
durable follow-up instead of pretending that the run succeeded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR
from vetinari.workbench import failure_intelligence_serialization as _serialization
from vetinari.workbench.method_library import MethodLibrary, MethodLibraryError
from vetinari.workbench.spine_consumers import record_trace_written

logger = logging.getLogger(__name__)


_PROJECT_ID_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_-]{1,64}")
_TRAVERSAL_MARKERS: tuple[str, ...] = ("/", "\\", "..", "\x00", " ", ";")
_AUTOPSY_STORE_LOCK: threading.Lock = threading.Lock()
_DEFAULT_STORE_PATH: Path = OUTPUTS_DIR / "workbench" / "failure_intelligence" / "autopsies.jsonl"


class FailureKind(str, Enum):
    """Canonical failure classes for bad or degraded Workbench runs."""

    MISSING_CAPABILITY = "missing_capability"
    STALE_SOURCE = "stale_source"
    WEAK_METHOD = "weak_method"
    BAD_PROMPT = "bad_prompt"
    BAD_ROUTING = "bad_routing"
    INSUFFICIENT_EVAL = "insufficient_eval"
    POLICY_CONFLICT = "policy_conflict"
    UNAVAILABLE_RUNTIME = "unavailable_runtime"
    HALLUCINATED_TOOL_ABILITY = "hallucinated_tool_ability"
    DATASET_DRIFT = "dataset_drift"
    USER_AMBIGUITY = "user_ambiguity"


class FollowupKind(str, Enum):
    """Durable artifact families that can prevent repeated failures."""

    EVAL_CASE = "eval_case"
    METHOD_TEST = "method_test"
    TOOL_CARD_UPDATE = "tool_card_update"
    PROMPT_PATCH = "prompt_patch"
    POLICY_CHANGE = "policy_change"
    SOURCE_REFRESH = "source_refresh"
    BENCHMARK_RUN = "benchmark_run"
    CAPABILITY_PACK_ISSUE = "capability_pack_issue"


class FailureIntelligenceError(Exception):
    """Base class for failure-intelligence errors."""


class FailureProjectIdRejected(ValueError):
    """Raised when a project id is not canonical."""

    def __init__(self, value: object) -> None:
        super().__init__(f"invalid project_id {value!r}; use [A-Za-z0-9_-] up to 64 characters")
        self.value = value


ProjectIdRejected = FailureProjectIdRejected


class FailureIntelligenceStoreCorrupt(FailureIntelligenceError):
    """Raised when persisted autopsy state cannot be trusted."""


class FailureIntelligenceStoreUnavailable(FailureIntelligenceError):
    """Raised when the append-only store cannot be read or written."""


@dataclass(frozen=True, slots=True)
class FailedRunContext:
    """Explicit evidence supplied for one bad or degraded run."""

    project_id: str = "default"
    run_id: str = ""
    status: str = "failed"
    task_profile: str | None = None
    prompt: str = ""
    output_summary: str = ""
    error_message: str = ""
    method_kind: str | None = None
    method_card_id: str | None = None
    method_promotion_status: str | None = None
    source_freshness: str | None = None
    stale_source_ids: tuple[str, ...] = ()
    tool_card_ids: tuple[str, ...] = ()
    unavailable_tool_names: tuple[str, ...] = ()
    hallucinated_tool_names: tuple[str, ...] = ()
    policy_rejection: str | None = None
    runtime_unavailable: bool = False
    missing_capability: str | None = None
    eval_count: int | None = None
    eval_failures: tuple[str, ...] = ()
    dataset_id: str | None = None
    dataset_revision: str | None = None
    expected_dataset_revision: str | None = None
    user_request: str = ""
    ambiguity_markers: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"FailedRunContext(project_id={self.project_id!r}, run_id={self.run_id!r}, status={self.status!r})"


@dataclass(frozen=True, slots=True)
class FailureCandidate:
    """One ordered failure-kind candidate with explicit evidence."""

    failure_kind: FailureKind
    confidence: float
    reason: str
    evidence_refs: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"FailureCandidate(failure_kind={self.failure_kind!r}, confidence={self.confidence!r}, reason={self.reason!r})"


@dataclass(frozen=True, slots=True)
class FollowupArtifact:
    """Durable artifact proposed by an autopsy."""

    kind: FollowupKind
    title: str
    description: str
    source_failure_kind: FailureKind

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"FollowupArtifact(kind={self.kind!r}, title={self.title!r}, description={self.description!r})"


@dataclass(frozen=True, slots=True)
class AutopsyResult:
    """Failure autopsy returned by the classifier and stored append-only."""

    autopsy_id: str
    project_id: str
    run_id: str
    status: str
    degraded: bool
    degraded_reason: str | None
    candidates: tuple[FailureCandidate, ...]
    followup: FollowupArtifact
    evidence_refs: tuple[str, ...]
    created_at_utc: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AutopsyResult(autopsy_id={self.autopsy_id!r}, project_id={self.project_id!r}, run_id={self.run_id!r})"


_FOLLOWUP_BY_FAILURE: dict[FailureKind, FollowupKind] = {
    FailureKind.MISSING_CAPABILITY: FollowupKind.CAPABILITY_PACK_ISSUE,
    FailureKind.STALE_SOURCE: FollowupKind.SOURCE_REFRESH,
    FailureKind.WEAK_METHOD: FollowupKind.METHOD_TEST,
    FailureKind.BAD_PROMPT: FollowupKind.PROMPT_PATCH,
    FailureKind.BAD_ROUTING: FollowupKind.PROMPT_PATCH,
    FailureKind.INSUFFICIENT_EVAL: FollowupKind.EVAL_CASE,
    FailureKind.POLICY_CONFLICT: FollowupKind.POLICY_CHANGE,
    FailureKind.UNAVAILABLE_RUNTIME: FollowupKind.CAPABILITY_PACK_ISSUE,
    FailureKind.HALLUCINATED_TOOL_ABILITY: FollowupKind.TOOL_CARD_UPDATE,
    FailureKind.DATASET_DRIFT: FollowupKind.BENCHMARK_RUN,
    FailureKind.USER_AMBIGUITY: FollowupKind.PROMPT_PATCH,
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonicalize_project_id(value: str | None) -> str:
    """Return a canonical project id or reject traversal-bearing input."""
    if not isinstance(value, str):
        raise ProjectIdRejected(value)
    if not value or len(value) > 64 or _PROJECT_ID_RE.fullmatch(value) is None:
        raise ProjectIdRejected(value)
    if any(marker in value for marker in _TRAVERSAL_MARKERS):
        raise ProjectIdRejected(value)
    return value


def _safe_tuple(values: tuple[str, ...] | list[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    return tuple(str(value) for value in values if value)


def _has_text(*values: str | None) -> bool:
    return any(bool(value and value.strip()) for value in values)


def _result_id(project_id: str, run_id: str, candidates: tuple[FailureCandidate, ...], created_at_utc: str) -> str:
    material = "|".join([
        project_id,
        run_id,
        created_at_utc,
        *[candidate.failure_kind.value for candidate in candidates],
    ])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"autopsy-{digest}"


def _evidence(*refs: str | None) -> tuple[str, ...]:
    return tuple(ref for ref in refs if ref)


def _candidate(kind: FailureKind, confidence: float, reason: str, *refs: str | None) -> FailureCandidate:
    return FailureCandidate(
        failure_kind=kind,
        confidence=confidence,
        reason=reason,
        evidence_refs=_evidence(*refs),
    )


def _add_candidate_if(
    candidates: list[FailureCandidate],
    condition: bool,
    kind: FailureKind,
    confidence: float,
    reason: str,
    *refs: str | None,
) -> None:
    if condition:
        candidates.append(_candidate(kind, confidence, reason, *refs))


def _has_dataset_drift_signal(context: FailedRunContext, text: str) -> bool:
    return bool(
        (
            context.dataset_revision
            and context.expected_dataset_revision
            and context.dataset_revision != context.expected_dataset_revision
        )
        or "dataset drift" in text
    )


def _add_late_classification_candidates(
    candidates: list[FailureCandidate],
    context: FailedRunContext,
    text: str,
) -> None:
    _add_candidate_if(
        candidates,
        bool(context.hallucinated_tool_names or context.unavailable_tool_names or "hallucinated tool" in text),
        FailureKind.HALLUCINATED_TOOL_ABILITY,
        0.93,
        "The run attempted or promised a tool capability that was unavailable.",
        *context.hallucinated_tool_names,
        *context.unavailable_tool_names,
    )
    _add_candidate_if(
        candidates,
        _has_dataset_drift_signal(context, text),
        FailureKind.DATASET_DRIFT,
        0.87,
        "The observed dataset revision differs from the expected revision.",
        context.dataset_id,
        context.dataset_revision,
        context.expected_dataset_revision,
    )
    _add_candidate_if(
        candidates,
        bool(context.ambiguity_markers or ("ambiguous" in text or "which one" in text)),
        FailureKind.USER_AMBIGUITY,
        0.76,
        "The user request or trace contains unresolved ambiguity.",
        *context.ambiguity_markers,
    )


def _followup_for(candidate: FailureCandidate, context: FailedRunContext) -> FollowupArtifact:
    followup_kind = _FOLLOWUP_BY_FAILURE[candidate.failure_kind]
    run_label = context.run_id or "unidentified run"
    return FollowupArtifact(
        kind=followup_kind,
        title=f"{followup_kind.value} for {candidate.failure_kind.value}",
        description=f"Create {followup_kind.value} after {run_label}: {candidate.reason}",
        source_failure_kind=candidate.failure_kind,
    )


_serialization.configure_serialization({
    "FailedRunContext": FailedRunContext,
    "FailureCandidate": FailureCandidate,
    "FailureKind": FailureKind,
    "FollowupArtifact": FollowupArtifact,
    "FollowupKind": FollowupKind,
    "AutopsyResult": AutopsyResult,
})
_context_from_mapping = _serialization._context_from_mapping
_result_to_dict = _serialization._result_to_dict
_result_from_dict = _serialization._result_from_dict


class FailureIntelligence:
    """Classify and persist bad-run autopsies."""

    def __init__(
        self,
        *,
        store_path: Path | str = _DEFAULT_STORE_PATH,
        method_library: MethodLibrary | None = None,
    ) -> None:
        self._store_path = Path(store_path)
        self._method_library = method_library

    def autopsy_failed_run(self, context: FailedRunContext | dict[str, Any]) -> AutopsyResult:
        """Alias for callers that frame the operation as an autopsy."""
        return self.classify(context)

    def classify(self, context: FailedRunContext | dict[str, Any]) -> AutopsyResult:
        """Return an ordered, evidence-backed autopsy result.

        Returns:
            AutopsyResult value produced by classify().
        """
        if isinstance(context, dict):
            context = _context_from_mapping(context)
        project_id = _canonicalize_project_id(context.project_id)
        candidates = self._classify_candidates(context)
        degraded = False
        degraded_reason = None
        if not candidates:
            degraded = True
            degraded_reason = "missing_evidence"
            candidates = (
                _candidate(
                    FailureKind.USER_AMBIGUITY,
                    0.0,
                    "No explicit failure evidence was supplied; create a prompt patch or eval that captures the missing signal.",
                    "missing-evidence",
                ),
            )
        primary = candidates[0]
        if primary.confidence < 0.5:
            degraded = True
            degraded_reason = degraded_reason or "low_confidence"
        created_at_utc = _utc_now_iso()
        evidence_refs = context.evidence_refs or tuple(
            ref for candidate in candidates for ref in candidate.evidence_refs
        )
        return AutopsyResult(
            autopsy_id=_result_id(project_id, context.run_id, candidates, created_at_utc),
            project_id=project_id,
            run_id=context.run_id,
            status=context.status,
            degraded=degraded,
            degraded_reason=degraded_reason,
            candidates=candidates,
            followup=_followup_for(primary, context),
            evidence_refs=evidence_refs,
            created_at_utc=created_at_utc,
        )

    def record_autopsy(self, result: AutopsyResult) -> AutopsyResult:
        """Append one autopsy result without rewriting prior records.

        Returns:
            Outcome produced by record_autopsy().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        record = json.dumps(_result_to_dict(result), sort_keys=True)
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            with _AUTOPSY_STORE_LOCK, self._store_path.open("a", encoding="utf-8") as handle:
                handle.write(record)
                handle.write("\n")
            # spine_consumers invokes get_spine() and absorbs observability failures.
            record_trace_written(
                trace_id=result.autopsy_id,
                query_hash="failure_intelligence",
                project_id=result.project_id,
            )
        except OSError as exc:
            raise FailureIntelligenceStoreUnavailable(f"autopsy store unavailable: {self._store_path}") from exc
        return result

    def list_autopsies(self, *, project_id: str = "default", limit: int | None = None) -> tuple[AutopsyResult, ...]:
        """Return stored autopsies for one project, failing closed on damage.

        Returns:
            Collection of autopsies values.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        canonical = _canonicalize_project_id(project_id)
        with _AUTOPSY_STORE_LOCK:
            if not self._store_path.exists():
                return ()
            try:
                lines = self._store_path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise FailureIntelligenceStoreUnavailable(f"autopsy store unavailable: {self._store_path}") from exc
        results: list[AutopsyResult] = []
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                decoded = json.loads(line)
                if not isinstance(decoded, dict):
                    raise TypeError("autopsy record is not an object")
                result = _result_from_dict(decoded)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise FailureIntelligenceStoreCorrupt(
                    f"autopsy store corrupt at {self._store_path}:{line_number}",
                ) from exc
            if result.project_id == canonical:
                results.append(result)
        if limit is not None:
            results = results[: max(0, limit)]
        return tuple(results)

    def get_autopsy(self, *, project_id: str = "default", autopsy_id: str) -> AutopsyResult | None:
        """Return one stored autopsy by id.

        Returns:
            Resolved autopsy value.
        """
        for result in self.list_autopsies(project_id=project_id):
            if result.autopsy_id == autopsy_id:
                return result
        return None

    def _classify_candidates(self, context: FailedRunContext) -> tuple[FailureCandidate, ...]:
        candidates: list[FailureCandidate] = []
        text = " ".join([context.prompt, context.output_summary, context.error_message, context.user_request]).lower()
        _add_candidate_if(
            candidates,
            bool(context.missing_capability or "missing capability" in text or "not implemented" in text),
            FailureKind.MISSING_CAPABILITY,
            0.94,
            "The run names a capability that is absent or not implemented.",
            context.missing_capability,
            "missing-capability",
        )
        _add_candidate_if(
            candidates,
            bool(context.source_freshness == "stale" or context.stale_source_ids),
            FailureKind.STALE_SOURCE,
            0.9,
            "A source card or trace input was marked stale.",
            *context.stale_source_ids,
            context.source_freshness,
        )
        _add_candidate_if(
            candidates,
            self._has_negative_method_signal(context),
            FailureKind.WEAK_METHOD,
            0.88,
            "The selected method has measured negative evidence for this task profile.",
            context.method_card_id,
            context.method_kind,
        )
        _add_candidate_if(
            candidates,
            "prompt" in text and ("unclear" in text or "bad" in text or "underspecified" in text),
            FailureKind.BAD_PROMPT,
            0.78,
            "Prompt evidence points to unclear or underspecified instructions.",
            "prompt-evidence",
        )
        _add_candidate_if(
            candidates,
            "routing" in text or "wrong agent" in text or "wrong model" in text,
            FailureKind.BAD_ROUTING,
            0.8,
            "Run evidence indicates the wrong route, agent, or model was selected.",
            "routing-evidence",
        )
        _add_candidate_if(
            candidates,
            context.eval_count == 0 or "no eval" in text or "insufficient eval" in text,
            FailureKind.INSUFFICIENT_EVAL,
            0.84,
            "The run lacks a discriminating eval for the claimed behavior.",
            "eval-count-0" if context.eval_count == 0 else "eval-evidence",
        )
        _add_candidate_if(
            candidates,
            bool(context.policy_rejection or ("policy" in text and ("conflict" in text or "blocked" in text))),
            FailureKind.POLICY_CONFLICT,
            0.91,
            "Policy evidence blocked or contradicted the attempted workflow.",
            context.policy_rejection,
        )
        _add_candidate_if(
            candidates,
            context.runtime_unavailable or "runtime unavailable" in text or "connection refused" in text,
            FailureKind.UNAVAILABLE_RUNTIME,
            0.89,
            "The required runtime, service, or local substrate was unavailable.",
            "runtime-unavailable",
        )
        _add_late_classification_candidates(candidates, context, text)
        return tuple(candidates)

    def _has_negative_method_signal(self, context: FailedRunContext) -> bool:
        if context.method_promotion_status == "measured_negative":
            return True
        if not context.method_kind and not context.method_card_id:
            return False
        library = self._method_library
        if library is None:
            return False
        try:
            negatives = library.list_negative_methods(project_id=context.project_id, task_profile=context.task_profile)
        except MethodLibraryError:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return False
        for card in negatives:
            if context.method_card_id and card.method_card_id == context.method_card_id:
                return True
            if context.method_kind and card.kind.value == context.method_kind:
                return True
        return False


__all__ = [
    "_AUTOPSY_STORE_LOCK",
    "_PROJECT_ID_RE",
    "AutopsyResult",
    "FailedRunContext",
    "FailureCandidate",
    "FailureIntelligence",
    "FailureIntelligenceError",
    "FailureIntelligenceStoreCorrupt",
    "FailureIntelligenceStoreUnavailable",
    "FailureKind",
    "FailureProjectIdRejected",
    "FollowupArtifact",
    "FollowupKind",
    "ProjectIdRejected",
]
