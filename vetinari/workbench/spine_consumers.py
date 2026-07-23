"""Typed convenience wrappers over WorkbenchSpine.append_* for workbench writes."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from vetinari.types import AgentType, ShardKind
from vetinari.workbench.assets import AssetKind, WorkbenchAsset
from vetinari.workbench.evals import EvalKind, EvalResult, EvalScore
from vetinari.workbench.leases import LeaseStatus, WorkbenchLease
from vetinari.workbench.metadata_spine import get_workbench_spine as get_spine
from vetinari.workbench.metadata_spine_records import WorkbenchSpineCorrupt
from vetinari.workbench.proposals import Promotion
from vetinari.workbench.runs import RunKind, RunStatus, WorkbenchRun
from vetinari.workbench.traces import TraceSpan, WorkbenchTrace

logger = logging.getLogger(__name__)

_SENSITIVE_DETAIL_RE = re.compile(
    r"(?i)((?:token|secret|password|api[_-]?key)\s*=\s*[A-Za-z0-9._-]+|bearer\s+[A-Za-z0-9._-]+|[A-Za-z]:[\\/][^\s]+|/[^\s]+)"
)


@dataclass(frozen=True, slots=True)
class SpineConsumerResult:
    """Operator-visible outcome for a metadata-spine consumer write."""

    action: str
    accepted_clean: bool
    classification: str
    evidence: str

    def __repr__(self) -> str:
        return (
            "SpineConsumerResult("
            f"action={self.action!r}, accepted_clean={self.accepted_clean!r}, "
            f"classification={self.classification!r})"
        )


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _apply_redaction(record: dict[str, Any], redact_fields: list[str] | None) -> dict[str, Any]:
    if not redact_fields:
        return dict(record)
    redacted = dict(record)
    for field in redact_fields:
        if field in redacted:
            redacted[field] = "<redacted>"
    return redacted


def _asset_kind(kind: str) -> AssetKind:
    try:
        return AssetKind(str(kind))
    except ValueError:
        logger.warning("Unknown workbench asset kind %r; recording as tool", kind)
        return AssetKind.TOOL


def _run_kind(kind: str) -> RunKind:
    try:
        return RunKind(str(kind))
    except ValueError:
        logger.warning("Unknown workbench run kind %r; recording as agent_run", kind)
        return RunKind.AGENT_RUN


def _clean_result(action: str) -> SpineConsumerResult:
    return SpineConsumerResult(
        action=action,
        accepted_clean=True,
        classification="clean",
        evidence="metadata spine append accepted",
    )


def _safe_evidence(exc: Exception) -> str:
    detail = str(exc)
    if not detail:
        return exc.__class__.__name__
    redacted = _SENSITIVE_DETAIL_RE.sub("<redacted>", detail)
    return redacted[:240]


def classify_spine_failure(exc: Exception) -> str:
    """Classify damaged metadata-spine state without rewriting it as clean.

    Returns:
        Stable fail-closed classification string for operator evidence.
    """
    if isinstance(exc, WorkbenchSpineCorrupt):
        reason = exc.reason.lower()
        if "truncated" in reason or "incomplete" in reason:
            return "partial_write"
        if "parse" in reason or "decode" in reason or "corrupt" in reason:
            return "corrupt_state"
        if "unreadable" in reason:
            return "unreadable_state"
        if "missing" in reason:
            return "missing_state"
        if "integrity_check" in reason or "sqlite" in reason:
            return "sqlite_corrupt"
        if "mismatch" in reason or "diverged" in reason or "stale" in reason:
            return "stale_state"
        return "fail_closed_spine_state"
    if isinstance(exc, OSError):
        return "unreadable_state"
    if isinstance(exc, ValueError):
        return "invalid_spine_record"
    return "fail_closed_spine_state"


def _warn_spine_failure(action: str, exc: Exception) -> SpineConsumerResult:
    classification = classify_spine_failure(exc)
    logger.warning(
        "metadata spine %s failed closed; classification=%s; observability record skipped: %s",
        action,
        classification,
        _safe_evidence(exc),
    )
    return SpineConsumerResult(
        action=action,
        accepted_clean=False,
        classification=classification,
        evidence=_safe_evidence(exc),
    )


def _append_or_classify(action: str, append: Any) -> SpineConsumerResult:
    try:
        append()
    except (ImportError, WorkbenchSpineCorrupt, OSError, ValueError, RuntimeError) as exc:
        logger.warning("metadata spine %s failed before clean acceptance", action)
        return _warn_spine_failure(action, exc)
    return _clean_result(action)


def record_asset_written(
    asset_id: str,
    kind: str,
    project_id: str,
    path: str | None = None,
    *,
    redact_fields: list[str] | None = None,
) -> SpineConsumerResult:
    """Record that a workbench asset was written to persistent storage.

    Args:
        asset_id: Asset identifier.
        kind: Asset kind string.
        project_id: Project scope.
        path: Optional path associated with the asset.
        redact_fields: Field names to redact before spine write.

    Returns:
        Clean or fail-closed outcome for the consumer write.
    """
    record = _apply_redaction(
        {"asset_id": asset_id, "kind": kind, "project_id": project_id, "path": path or ""},
        redact_fields,
    )
    return _append_or_classify(
        "asset write",
        lambda: get_spine().append_asset(
            WorkbenchAsset(
                asset_id=str(record["asset_id"]),
                kind=_asset_kind(str(record["kind"])),
                name=str(record["asset_id"]),
                revision="1",
                created_at_utc=_now(),
                provenance={
                    "source": "vetinari.workbench.spine_consumers",
                    "project_id": str(record["project_id"]),
                    "path": str(record["path"]),
                },
            )
        ),
    )


def record_run_started(
    run_id: str,
    kind: str,
    project_id: str,
    *,
    redact_fields: list[str] | None = None,
) -> SpineConsumerResult:
    """Record that a workbench run started.

    Args:
        run_id: Run identifier.
        kind: Run kind string.
        project_id: Project scope.
        redact_fields: Field names to redact before spine write.

    Returns:
        Clean or fail-closed outcome for the consumer write.
    """
    record = _apply_redaction({"run_id": run_id, "kind": kind, "project_id": project_id}, redact_fields)
    return _append_or_classify(
        "run start",
        lambda: get_spine().append_run(
            WorkbenchRun(
                run_id=str(record["run_id"]),
                kind=_run_kind(str(record["kind"])),
                status=RunStatus.RUNNING,
                started_at_utc=_now(),
                finished_at_utc="",
                actor_agent_type=AgentType.WORKBENCH,
                asset_revisions=(),
                lease_id="",
                shard_kind=ShardKind.STANDARD,
                project_id=str(record["project_id"]),
            )
        ),
    )


def record_run_completed(
    run_id: str,
    kind: str,
    project_id: str,
    status: str = "completed",
    *,
    redact_fields: list[str] | None = None,
) -> SpineConsumerResult:
    """Record that a workbench run reached a terminal state.

    Args:
        run_id: Run identifier.
        kind: Run kind string.
        project_id: Project scope.
        status: Terminal status string.
        redact_fields: Field names to redact before spine write.

    Returns:
        Clean or fail-closed outcome for the consumer write.
    """
    record = _apply_redaction(
        {"run_id": run_id, "kind": kind, "project_id": project_id, "status": status},
        redact_fields,
    )
    run_status = RunStatus.SUCCEEDED if str(record["status"]) == "completed" else RunStatus.BLOCKED
    now = _now()
    return _append_or_classify(
        "run completion",
        lambda: get_spine().append_run(
            WorkbenchRun(
                run_id=str(record["run_id"]),
                kind=_run_kind(str(record["kind"])),
                status=run_status,
                started_at_utc=now,
                finished_at_utc=now,
                actor_agent_type=AgentType.WORKBENCH,
                asset_revisions=(),
                lease_id="",
                shard_kind=ShardKind.STANDARD,
                project_id=str(record["project_id"]),
            )
        ),
    )


def record_run_failed(
    run_id: str,
    kind: str,
    project_id: str,
    error: str = "",
    *,
    redact_fields: list[str] | None = None,
) -> SpineConsumerResult:
    """Record that a workbench run failed.

    Args:
        run_id: Run identifier.
        kind: Run kind string.
        project_id: Project scope.
        error: Failure detail to include in observability context.
        redact_fields: Field names to redact before spine write.

    Returns:
        Clean or fail-closed outcome for the consumer write.
    """
    record = _apply_redaction(
        {"run_id": run_id, "kind": kind, "project_id": project_id, "error": error},
        redact_fields,
    )
    now = _now()
    return _append_or_classify(
        "run failure",
        lambda: get_spine().append_run(
            WorkbenchRun(
                run_id=str(record["run_id"]),
                kind=_run_kind(str(record["kind"])),
                status=RunStatus.FAILED,
                started_at_utc=now,
                finished_at_utc=now,
                actor_agent_type=AgentType.WORKBENCH,
                asset_revisions=(),
                lease_id="",
                shard_kind=ShardKind.STANDARD,
                project_id=str(record["project_id"]),
            )
        ),
    )


def record_trace_written(
    trace_id: str,
    query_hash: str,
    project_id: str,
    rag_revision_id: str = "",
    *,
    redact_fields: list[str] | None = None,
) -> SpineConsumerResult:
    """Record that a trace-like workbench artifact was written.

    Args:
        trace_id: Trace identifier.
        query_hash: Query or tool hash.
        project_id: Project scope.
        rag_revision_id: Optional RAG revision id.
        redact_fields: Field names to redact before spine write.

    Returns:
        Clean or fail-closed outcome for the consumer write.
    """
    record = _apply_redaction(
        {
            "trace_id": trace_id,
            "query_hash": query_hash,
            "project_id": project_id,
            "rag_revision_id": rag_revision_id,
        },
        redact_fields,
    )
    now = _now()
    return _append_or_classify(
        "trace write",
        lambda: get_spine().append_trace(
            WorkbenchTrace(
                trace_id=str(record["trace_id"]),
                run_id=f"trace-run-{record['project_id']}",
                root_span_id="root",
                spans=(
                    TraceSpan(
                        span_id="root",
                        parent_span_id=None,
                        tool_name=str(record["query_hash"] or "trace"),
                        started_at_utc=now,
                        finished_at_utc=now,
                        inputs_hash=str(record["rag_revision_id"]),
                        outputs_hash="",
                        error="",
                        duration_ms=0,
                    ),
                ),
                captured_at_utc=now,
            )
        ),
    )


def record_eval_written(
    eval_id: str,
    project_id: str,
    score: float | None = None,
    *,
    redact_fields: list[str] | None = None,
) -> SpineConsumerResult:
    """Record that an evaluation result was written.

    Args:
        eval_id: Evaluation identifier.
        project_id: Project scope.
        score: Optional score value.
        redact_fields: Field names to redact before spine write.

    Returns:
        Clean or fail-closed outcome for the consumer write.
    """
    record = _apply_redaction({"eval_id": eval_id, "project_id": project_id, "score": score}, redact_fields)
    return _append_or_classify(
        "eval write",
        lambda: get_spine().append_eval(
            EvalResult(
                eval_id=str(record["eval_id"]),
                kind=EvalKind.LIVE_TRACE_DERIVED,
                run_id=f"eval-run-{record['project_id']}",
                asset_id=f"eval-asset-{record['project_id']}",
                asset_revision="1",
                scores=(
                    EvalScore(
                        metric_name="score",
                        value=float(record["score"] if record["score"] is not None else 0.0),
                        threshold=0.0,
                        passed=True,
                    ),
                ),
                captured_at_utc=_now(),
            )
        ),
    )


def record_lease_acquired(
    lease_id: str,
    resource_kind: str,
    project_id: str,
    *,
    redact_fields: list[str] | None = None,
) -> SpineConsumerResult:
    """Record that a scheduler or serving lease was acquired.

    Args:
        lease_id: Lease identifier.
        resource_kind: Resource kind string.
        project_id: Project scope.
        redact_fields: Field names to redact before spine write.

    Returns:
        Clean or fail-closed outcome for the consumer write.
    """
    record = _apply_redaction(
        {"lease_id": lease_id, "resource_kind": resource_kind, "project_id": project_id},
        redact_fields,
    )

    def _append_lease() -> None:
        from vetinari.runtime.workbench_scheduler import Lane

        get_spine().append_lease(
            WorkbenchLease(
                lease_id=str(record["lease_id"]),
                lane=Lane.INTERACTIVE,
                status=LeaseStatus.GRANTED,
                lease_handle=str(record["resource_kind"]),
                granted_at_utc=_now(),
                released_at_utc="",
                requested_for_run_id=f"lease-run-{record['project_id']}",
                vram_share=0.0,
            )
        )

    return _append_or_classify("lease acquisition", _append_lease)


def record_promotion(
    run_id: str,
    project_id: str,
    promoted_model_id: str,
    *,
    redact_fields: list[str] | None = None,
) -> SpineConsumerResult:
    """Record that a model or artifact promotion decision was written.

    Args:
        run_id: Source run id.
        project_id: Project scope.
        promoted_model_id: Promoted model or artifact id.
        redact_fields: Field names to redact before spine write.

    Returns:
        Clean or fail-closed outcome for the consumer write.
    """
    record = _apply_redaction(
        {"run_id": run_id, "project_id": project_id, "promoted_model_id": promoted_model_id},
        redact_fields,
    )
    return _append_or_classify(
        "promotion",
        lambda: get_spine().record_promotion(
            Promotion(
                promotion_id=f"promotion-{record['run_id']}-{record['promoted_model_id']}",
                proposal_id=f"proposal-{record['promoted_model_id']}",
                accepted=True,
                decided_at_utc=_now(),
                decided_by=str(record["project_id"]),
                rationale="",
            )
        ),
    )


__all__ = [
    "SpineConsumerResult",
    "classify_spine_failure",
    "record_asset_written",
    "record_eval_written",
    "record_lease_acquired",
    "record_promotion",
    "record_run_completed",
    "record_run_failed",
    "record_run_started",
    "record_trace_written",
]
