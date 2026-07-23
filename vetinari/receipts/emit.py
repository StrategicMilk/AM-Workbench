"""Helpers that build and append WorkReceipts at well-known emission points.

Three emission flavours:

- ``record_agent_completion`` is called from ``base_agent_completion.complete_task``
  after every Foreman / Worker / Inspector task. The agent_type maps to the
  receipt kind (FOREMAN -> PLAN_ROUND, WORKER -> WORKER_TASK,
  INSPECTOR -> INSPECTOR_PASS).
- ``record_training_step`` is called from ``vetinari.training.pipeline.TrainingPipeline.run``
  after a training run completes. Fallback runs (``_is_fallback=True``) are
  skipped to avoid inflating training counts (anti-pattern: Fallback as success).
- ``record_release_step`` is called by the release doctor after each step
  records a ``ReleaseClaimRecord`` so the Control Center reflects release
  progress without polling the proof file.

All three return the appended ``WorkReceipt`` for caller visibility.  None
of them raise on bus or store failure: a failing receipt emission must
never crash agent execution; the fallback is a structured WARNING log.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.privacy.envelope import privacy_receipt
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore
from vetinari.types import AgentType, EvidenceBasis

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.agents.base_agent import BaseAgent
    from vetinari.agents.contracts import AgentResult, AgentTask


# AgentType -> WorkReceiptKind mapping.  The factory pipeline produces
# exactly one receipt kind per agent role.
_AGENT_KIND_MAP: dict[AgentType, WorkReceiptKind] = {
    AgentType.FOREMAN: WorkReceiptKind.PLAN_ROUND,
    AgentType.WORKER: WorkReceiptKind.WORKER_TASK,
    AgentType.INSPECTOR: WorkReceiptKind.INSPECTOR_PASS,
}

_DEFAULT_PROJECT_ID = "default"


def _record_emission_failure(*, kind: str) -> None:
    """Increment a counter when a receipt emission fails silently.

    Receipt emission helpers swallow exceptions so they never crash
    agent execution, but a silent failure leaves the Control Center
    blind. The counter (vetinari.receipts.emission_failures) makes
    those silent failures observable in metrics dashboards.

    The metrics subsystem must never crash receipt emission either,
    and a WARNING here would spam logs whenever the metrics backend
    hiccups; ``contextlib.suppress`` is the right idiom — the counter
    increment is fire-and-forget. Operators who need to debug the
    metrics path will see a WARNING from inside ``get_metrics()``
    itself, not from this helper.
    """
    import contextlib

    with contextlib.suppress(Exception):
        from vetinari.metrics import get_metrics

        get_metrics().increment("vetinari.receipts.emission_failures", kind=kind)


def _summary(text: str | None, limit: int = 200) -> str:
    """Trim *text* to *limit* characters with an ellipsis if truncated.

    Accepts ``None`` defensively because callers occasionally pass an
    optional task field; coerces to an empty string in that case.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _privacy_safe_summary(text: str | None, limit: int = 200) -> str:
    """Redact sensitive markers before a summary is stored durably."""
    summary = _summary(text, limit=limit)
    if not summary:
        return ""
    try:
        from vetinari.security.redaction import redact_text

        return _summary(redact_text(summary), limit=limit)
    except Exception:
        logger.warning("Receipt summary redaction unavailable; storing conservative placeholder", exc_info=True)
        return "[REDACTION_UNAVAILABLE]"


def receipt_privacy_receipt(receipt: WorkReceipt) -> dict[str, Any]:
    """Build the privacy envelope for a durable WorkReceipt row.

    Args:
        receipt: WorkReceipt whose project, kind, and user-awaiting state define privacy scope.

    Returns:
        Privacy receipt metadata with operational or subject-data classification and erasure token.
    """
    subject_id = receipt.project_id if receipt.awaiting_user else None
    return privacy_receipt(
        privacy_class="subject_data" if receipt.awaiting_user else "operational",
        subject_id=subject_id,
        source=f"work_receipt:{receipt.kind.value}",
        erasure_token=f"work_receipt:{receipt.project_id}:{receipt.receipt_id}",
        redaction_applied=receipt.awaiting_user,
    )


def _coerce_project_id(task: AgentTask | None, fallback: str = _DEFAULT_PROJECT_ID) -> str:
    """Pull the project_id from an AgentTask context, with safe fallback.

    Args:
        task: The AgentTask whose context may carry a ``project_id``.
        fallback: Value to use when no project_id is present.

    Returns:
        A non-empty project_id string.
    """
    if task is None:
        return fallback
    ctx = getattr(task, "context", None) or {}
    pid = ctx.get("project_id") if isinstance(ctx, dict) else None
    if isinstance(pid, str) and pid.strip():
        return pid.strip()
    return fallback


def _build_outcome_from_score(
    *,
    success: bool,
    score: float,
    source: str,
    scoring_available: bool = True,
    issues: Iterable[str] = (),
    suggestions: Iterable[str] = (),
) -> OutcomeSignal:
    """Build an OutcomeSignal that reflects a quality-scored agent outcome.

    The basis is ``LLM_JUDGMENT`` when scoring ran and the work
    succeeded; ``UNSUPPORTED`` is used for execution failures and for
    the case where scoring itself crashed (``scoring_available=False``)
    so consumers fail-closed on ambiguous outputs and never mistake a
    score-of-zero-because-scorer-died for a score-of-zero-because-it-was-bad.
    """
    if not success:
        return OutcomeSignal(
            passed=False,
            score=0.0,
            basis=EvidenceBasis.UNSUPPORTED,
            issues=tuple(issues) or ("agent execution did not succeed",),
            suggestions=tuple(suggestions),
            provenance=Provenance(
                source=source,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
            ),
        )
    if not scoring_available:
        # Work succeeded but the quality scorer could not produce a
        # signal. Mark UNSUPPORTED rather than LLM_JUDGMENT so the
        # downstream consumer cannot treat score=0.0 as a real verdict.
        return OutcomeSignal(
            passed=True,
            score=0.0,
            basis=EvidenceBasis.UNSUPPORTED,
            issues=tuple(issues) or ("quality scoring unavailable for this task",),
            suggestions=tuple(suggestions),
            provenance=Provenance(
                source=source,
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
            ),
        )
    return OutcomeSignal(
        passed=True,
        score=float(score),
        basis=EvidenceBasis.LLM_JUDGMENT,
        issues=tuple(issues),
        suggestions=tuple(suggestions),
        provenance=Provenance(
            source=source,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
        ),
    )


def record_agent_completion(
    *,
    agent: BaseAgent,
    task: AgentTask,
    result: AgentResult,
    score: float,
    scoring_available: bool = True,
    store: WorkReceiptStore | None = None,
) -> WorkReceipt | None:
    """Emit a WorkReceipt for a completed agent task.

    Returns:
        Value produced for the caller.
    """
    try:
        kind = _AGENT_KIND_MAP.get(agent.agent_type)
        if kind is None:
            logger.warning(
                "No receipt kind mapping for agent_type=%s — skipping receipt emission",
                agent.agent_type,
            )
            _record_emission_failure(kind="unknown_agent_type")
            return None

        meta = _agent_receipt_metadata(agent.agent_type, result.metadata or {})
        receipt = _build_agent_completion_receipt(
            agent=agent,
            task=task,
            result=result,
            kind=kind,
            score=score,
            scoring_available=scoring_available,
            awaiting_user=meta["awaiting_user"],
            awaiting_reason=meta["awaiting_reason"],
            linked_claim_ids=meta["linked_claim_ids"],
        )
        (store or WorkReceiptStore()).append(receipt)
        return receipt
    except Exception:
        logger.warning(
            "Failed to emit WorkReceipt for agent=%s task=%s — continuing",
            getattr(agent, "agent_type", "<unknown>"),
            getattr(task, "task_id", "<unknown>"),
            exc_info=True,
        )
        _record_emission_failure(kind="agent_completion")
        return None


def _agent_receipt_metadata(agent_type: AgentType, meta: dict[str, Any]) -> dict[str, Any]:
    awaiting_user = bool(meta.get("awaiting_user"))
    awaiting_reason: str | None = meta.get("awaiting_reason")
    if awaiting_user and agent_type is AgentType.WORKER:
        logger.warning("Worker attempted to set awaiting_user=True; suppressed")
        awaiting_user = False
        awaiting_reason = None
    return {
        "awaiting_user": awaiting_user,
        "awaiting_reason": awaiting_reason,
        "linked_claim_ids": tuple(meta.get("linked_claim_ids", ())),
    }


def _build_agent_completion_receipt(
    *,
    agent: BaseAgent,
    task: AgentTask,
    result: AgentResult,
    kind: WorkReceiptKind,
    score: float,
    scoring_available: bool,
    awaiting_user: bool,
    awaiting_reason: str | None,
    linked_claim_ids: tuple[str, ...],
) -> WorkReceipt:
    outcome = _build_outcome_from_score(
        success=result.success,
        score=score,
        scoring_available=scoring_available,
        source=f"vetinari.agents.{agent.agent_type.value.lower()}",
        issues=tuple(result.errors or ()),
    )
    output_str = result.output if isinstance(result.output, str) else str(result.output)
    return WorkReceipt(
        project_id=_coerce_project_id(task),
        agent_id=getattr(agent, "name", agent.agent_type.value),
        agent_type=agent.agent_type,
        kind=kind,
        outcome=outcome,
        inputs_summary=_privacy_safe_summary(task.description or task.prompt or ""),
        outputs_summary=_privacy_safe_summary(output_str),
        awaiting_user=awaiting_user,
        awaiting_reason=awaiting_reason,
        linked_claim_ids=linked_claim_ids,
    )


def record_training_step(
    *,
    project_id: str,
    run_id: str,
    base_model: str,
    algorithm: str,
    epochs: int,
    training_examples: int,
    success: bool,
    eval_score: float = 0.0,
    error: str = "",
    is_fallback: bool = False,
    store: WorkReceiptStore | None = None,
) -> WorkReceipt | None:
    """Emit a TRAINING_STEP receipt for one training-run completion.

    Returns:
        Value produced for the caller.
    """
    if is_fallback:
        logger.info(
            "Skipping TRAINING_STEP receipt for fallback run %s (algorithm=%s) — "
            "fallbacks are not recorded as completed training",
            run_id,
            algorithm,
        )
        return None

    try:
        receipt = _build_training_step_receipt(
            project_id=project_id,
            run_id=run_id,
            base_model=base_model,
            algorithm=algorithm,
            epochs=epochs,
            training_examples=training_examples,
            success=success,
            eval_score=eval_score,
            error=error,
        )

        (store or WorkReceiptStore()).append(receipt)
        return receipt
    except Exception:
        logger.warning(
            "Failed to emit TRAINING_STEP receipt for run %s — continuing",
            run_id,
            exc_info=True,
        )
        _record_emission_failure(kind="training_step")
        return None


def _build_training_step_receipt(
    *,
    project_id: str,
    run_id: str,
    base_model: str,
    algorithm: str,
    epochs: int,
    training_examples: int,
    success: bool,
    eval_score: float,
    error: str,
) -> WorkReceipt:
    tool_evidence = ()
    if success:
        tool_evidence = (
            ToolEvidence(
                tool_name="qlora_trainer",
                command=(
                    f"training_step run_id={run_id} algorithm={algorithm} epochs={epochs} examples={training_examples}"
                ),
                exit_code=0,
                stdout_snippet=f"eval_score={float(eval_score):.3f}",
                passed=True,
            ),
        )
    outcome = OutcomeSignal(
        passed=success,
        score=float(eval_score) if success else 0.0,
        basis=EvidenceBasis.TOOL_EVIDENCE if success else EvidenceBasis.UNSUPPORTED,
        tool_evidence=tool_evidence,
        issues=(error,) if error else (),
        provenance=Provenance(
            source="vetinari.training.pipeline",
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            tool_name="qlora_trainer",
        ),
    )
    inputs = f"base_model={base_model} | algo={algorithm} | epochs={epochs} | examples={training_examples}"
    outputs = f"run_id={run_id} | success={success}" + (f" | eval_score={eval_score:.3f}" if success else "")
    return WorkReceipt(
        project_id=project_id,
        agent_id=f"training-runner:{run_id}",
        agent_type=AgentType.TRAINING,
        kind=WorkReceiptKind.TRAINING_STEP,
        outcome=outcome,
        inputs_summary=_privacy_safe_summary(inputs),
        outputs_summary=_privacy_safe_summary(outputs),
    )


def record_release_step(
    *,
    project_id: str,
    version: str,
    step_name: str,
    success: bool,
    proof_path: Path | str | None = None,
    linked_claim_ids: Iterable[str] = (),
    error: str = "",
    store: WorkReceiptStore | None = None,
) -> WorkReceipt | None:
    """Emit a RELEASE_STEP receipt for one release-doctor stage.

    The release doctor calls this after each pipeline step (build, install,
    doctor, smoke, sign) so the Control Center can show release progress
    without polling the proof file.

    Args:
        project_id: Project owning this release.
        version: Release version string (e.g. ``"0.9.0"``).
        step_name: Name of the release step (e.g. ``"build"``,
            ``"smoke"``, ``"sign"``).
        success: Whether the step succeeded.
        proof_path: Optional path to the proof artifact for the
            outputs_summary.
        linked_claim_ids: Identifiers of ClaimsLedger records emitted
            during this step.
        error: Error message if the step failed; ``""`` otherwise.
        store: Optional WorkReceiptStore override.

    Returns:
        The appended WorkReceipt, or ``None`` if emission failed.
    """
    try:
        tool_evidence = ()
        if success:
            tool_evidence = (
                ToolEvidence(
                    tool_name="release_doctor",
                    command=f"release_step version={version} step={step_name}",
                    exit_code=0,
                    stdout_snippet=f"proof_path={proof_path}" if proof_path is not None else "release step passed",
                    passed=True,
                ),
            )
        outcome = OutcomeSignal(
            passed=success,
            score=1.0 if success else 0.0,
            basis=EvidenceBasis.TOOL_EVIDENCE if success else EvidenceBasis.UNSUPPORTED,
            tool_evidence=tool_evidence,
            issues=(error,) if error else (),
            provenance=Provenance(
                source="scripts.release_doctor",
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                tool_name="release_doctor",
            ),
        )

        outputs = f"version={version} | step={step_name} | success={success}"
        if proof_path is not None:
            outputs += f" | proof={proof_path}"

        receipt = WorkReceipt(
            project_id=project_id,
            agent_id=f"release-doctor:{version}",
            # Release doctor is an auxiliary runner, not a factory-pipeline
            # agent — labeled with AgentType.RELEASE per ADR-0103.
            agent_type=AgentType.RELEASE,
            kind=WorkReceiptKind.RELEASE_STEP,
            outcome=outcome,
            inputs_summary=_privacy_safe_summary(f"release pipeline step: {step_name}"),
            outputs_summary=_privacy_safe_summary(outputs),
            linked_claim_ids=tuple(linked_claim_ids),
        )

        (store or WorkReceiptStore()).append(receipt)
        return receipt
    except Exception:
        logger.warning(
            "Failed to emit RELEASE_STEP receipt for version=%s step=%s — continuing",
            version,
            step_name,
            exc_info=True,
        )
        _record_emission_failure(kind="release_step")
        return None


def record_workbench_event(
    *,
    project_id: str,
    event_name: str,
    success: bool,
    actor_id: str = "workbench",
    inputs_summary: str = "",
    outputs_summary: str = "",
    evidence_ref: str = "",
    error: str = "",
    linked_claim_ids: Iterable[str] = (),
    store: WorkReceiptStore | None = None,
) -> WorkReceipt | None:
    """Emit a WORKBENCH receipt for subsystem-scoped work.

    Args:
        project_id: Project this Workbench operation belongs to.
        event_name: Concrete Workbench operation name.
        success: Whether the operation completed successfully.
        actor_id: Concrete Workbench actor or service name.
        inputs_summary: Human-readable input summary.
        outputs_summary: Human-readable output summary.
        evidence_ref: Optional artifact or spine reference backing success.
        error: Failure reason when ``success`` is false.
        linked_claim_ids: Claim ids cited by this workbench operation.
        store: Optional WorkReceiptStore override.

    Returns:
        The appended WorkReceipt, or ``None`` if emission failed.
    """
    try:
        issues = (error,) if error else ()
        if not success and not issues:
            issues = ("workbench operation did not succeed",)
        tool_evidence = ()
        if success:
            tool_evidence = (
                ToolEvidence(
                    tool_name=event_name,
                    command=f"workbench_event event={event_name} actor={actor_id}",
                    exit_code=0,
                    stdout_snippet=evidence_ref or outputs_summary or "workbench event passed",
                    passed=True,
                ),
            )
        outcome = OutcomeSignal(
            passed=success,
            score=1.0 if success else 0.0,
            basis=EvidenceBasis.TOOL_EVIDENCE if success else EvidenceBasis.UNSUPPORTED,
            tool_evidence=tool_evidence,
            issues=issues,
            provenance=Provenance(
                source=f"vetinari.workbench.{event_name}",
                timestamp_utc=datetime.now(timezone.utc).isoformat(),
                tool_name=event_name,
            ),
        )
        receipt = WorkReceipt(
            project_id=project_id,
            agent_id=f"workbench:{actor_id}",
            agent_type=AgentType.WORKBENCH,
            kind=WorkReceiptKind.WORKBENCH_EVENT,
            outcome=outcome,
            inputs_summary=_privacy_safe_summary(inputs_summary or event_name),
            outputs_summary=_privacy_safe_summary(
                outputs_summary or f"event={event_name} success={success} evidence={evidence_ref}"
            ),
            linked_claim_ids=tuple(linked_claim_ids),
        )
        (store or WorkReceiptStore()).append(receipt)
        return receipt
    except Exception:
        logger.warning(
            "Failed to emit WORKBENCH receipt for project=%s event=%s — continuing",
            project_id,
            event_name,
            exc_info=True,
        )
        _record_emission_failure(kind="workbench_event")
        return None


__all__ = [
    "receipt_privacy_receipt",
    "record_agent_completion",
    "record_release_step",
    "record_training_step",
    "record_workbench_event",
]
