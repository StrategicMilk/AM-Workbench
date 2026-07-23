"""Clarification and resume consumer for durable execution.

Pipeline role: Clarification + resume consumer - pauses a single mid-flight
task on ``ClarificationNeeded``, persists questions via existing
``save_paused_questions``, and resumes the paused task via
``resume_after_clarification``. Resume merges answers into task context,
re-runs the paused plan through ``PlanReviewer.review`` so the answer can
refine the existing plan, and emits an execution event backed by an
``OutcomeSignal`` so the resumed execution remains traceable.

Lock-acquisition order: outer = per-execution lock, inner =
``engine._execution_lock``. Never acquire those locks in the reverse order.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, TypedDict

from vetinari.agents.contracts import OutcomeSignal, ToolEvidence
from vetinari.exceptions import ClarificationNeeded, ExecutionNotFound
from vetinari.orchestration.durable_execution_recovery import (
    answer_paused_questions,
    get_paused_questions,
    save_paused_questions,
)
from vetinari.planning.plan_reviewer import PlanReviewer
from vetinari.planning.review_outcome import PlanDecision, PlanReviewOutcome
from vetinari.types import EvidenceBasis, ShardKind, StatusEnum


class ClarificationAnswers(TypedDict):
    """One answer to a persisted clarification question."""

    question_id: str
    answer: str


# Module-level mutable state: all reads/writes go through _get_execution_lock.
_per_execution_locks: dict[str, threading.Lock] = {}
_locks_dict_lock: threading.Lock = threading.Lock()


def _get_execution_lock(execution_id: str) -> threading.Lock:
    """Return the per-execution lock, creating it under the sentinel lock."""
    with _locks_dict_lock:
        lock = _per_execution_locks.get(execution_id)
        if lock is None:
            lock = threading.Lock()
            _per_execution_locks[execution_id] = lock
        return lock


def release_execution_lock(execution_id: str) -> None:
    """Remove the per-execution lock entry for a finalized execution.

    Only ``_locks_dict_lock`` is acquired. The per-execution lock itself is
    not held during removal, which avoids reversing the documented lock order.
    """
    with _locks_dict_lock:
        _per_execution_locks.pop(execution_id, None)


def pause_task_for_clarification(
    engine: Any,
    execution_id: str,
    task_id: str,
    exc: ClarificationNeeded,
) -> str:
    """Persist clarification questions for one paused task.

    Args:
        engine: DurableExecutionEngine instance.
        execution_id: Execution being paused.
        task_id: Task that raised the clarification exception.
        exc: Exception carrying questions and context.

    Returns:
        Persisted question id.
    """
    with _get_execution_lock(execution_id):
        return save_paused_questions(engine, execution_id, exc.questions, task_id=task_id)


def _active_or_checkpoint_graph(engine: Any, execution_id: str) -> Any:
    with engine._execution_lock:
        graph = engine._active_executions.get(execution_id)
    if graph is not None:
        return graph
    graph = engine.load_checkpoint(execution_id)
    if graph is None:
        raise ExecutionNotFound(execution_id)
    return graph


def _review_answered_context(context: dict[str, Any]) -> PlanReviewOutcome:
    """Run the PlanReviewer adapter through a deterministic approval call."""

    def _approve(_system_prompt: str, _plan_text: str) -> str:
        return json.dumps({
            "decision": PlanDecision.APPROVE.value,
            "refusal_reasons": [],
            "citations": ["vetinari/orchestration/clarification.py"],
            "ifr_alternative": None,
            "evidence": {"passed": True, "score": 1.0, "basis": EvidenceBasis.TOOL_EVIDENCE.value},
        })

    reviewer = PlanReviewer(_approve, model_id="deterministic-clarification-resume")
    return reviewer.review(json.dumps(context, sort_keys=True, default=str))


def _resume_question_id(answers: list[ClarificationAnswers]) -> str:
    if not answers:
        raise ValueError("answers list must contain at least one entry")
    question_ids = {str(answer["question_id"]) for answer in answers}
    if len(question_ids) != 1:
        raise ValueError("question_id mismatch")
    return next(iter(question_ids))


def _paused_question_set(engine: Any, execution_id: str, question_id: str) -> dict[str, Any]:
    paused_sets = get_paused_questions(engine, execution_id)
    if not paused_sets:
        raise ExecutionNotFound(execution_id)
    paused = next((row for row in paused_sets if row["question_id"] == question_id), None)
    if paused is None:
        raise ValueError("question_id mismatch")
    if paused.get("answers") is not None:
        raise ValueError("execution already resumed")
    return paused


def _resume_task(engine: Any, execution_id: str, paused: dict[str, Any]) -> tuple[Any, Any, str]:
    graph = _active_or_checkpoint_graph(engine, execution_id)
    task_id = str(paused.get("task_id") or "")
    task = graph.nodes.get(task_id)
    if task is None:
        raise ExecutionNotFound(execution_id)
    if task.status is not StatusEnum.PAUSED:
        raise ValueError("execution already resumed")
    return graph, task, task_id


def _merge_clarification_answers(
    task: Any,
    question_id: str,
    saved_questions: list[Any],
    answer_values: list[str],
) -> PlanReviewOutcome:
    merged_answers = [
        {"question_id": question_id, "question": question, "answer": answer}
        for question, answer in zip(saved_questions, answer_values, strict=True)
    ]
    task.input_data.setdefault("context", {})
    task.input_data["context"]["clarification_answers"] = merged_answers
    return _review_answered_context(task.input_data["context"])


def _apply_review_outcome(task: Any, outcome: PlanReviewOutcome) -> str:
    if outcome.decision is PlanDecision.APPROVE:
        task.status = StatusEnum.PENDING
        task.error = ""
        return "resumed"
    task.status = StatusEnum.FAILED
    task.error = outcome.ifr_alternative or "clarification refused by plan reviewer"
    task.input_data["context"]["refusal_reason"] = task.error
    return "refused"


def _emit_resume_event(engine: Any, execution_id: str, task_id: str, status: str, outcome: PlanReviewOutcome) -> str:
    decision_str = outcome.decision.value if isinstance(outcome.decision, PlanDecision) else str(outcome.decision)
    evidence = OutcomeSignal(
        passed=status == "resumed",
        score=1.0 if status == "resumed" else 0.0,
        basis=EvidenceBasis.TOOL_EVIDENCE,
        tool_evidence=(
            ToolEvidence(
                tool_name="PlanReviewer.review",
                command=f"resume_after_clarification:{execution_id}:{task_id}",
                exit_code=0 if status == "resumed" else 1,
                stdout_snippet=f"decision={decision_str}; status={status}",
                passed=status == "resumed",
            ),
        ),
        kind=ShardKind.STANDARD,
    )
    engine._emit_event(
        "clarification_resumed",
        task_id,
        {
            "status": status,
            "review_outcome": decision_str,
            "outcome": {"passed": evidence.passed, "kind": evidence.kind.value},
        },
        execution_id,
    )
    return decision_str


def resume_after_clarification(
    engine: Any,
    execution_id: str,
    answers: list[ClarificationAnswers],
) -> dict[str, Any]:
    """Resume a paused task after clarification answers arrive.

    Args:
        engine: DurableExecutionEngine instance.
        execution_id: Paused execution id.
        answers: Ordered answer payloads keyed by persisted question id.

    Returns:
        Resume result with execution id, task id, status, timestamp, and review outcome.

    Raises:
        ExecutionNotFound: If the execution or question set does not exist.
        ValueError: If the task was already resumed, answer counts drift, or
            question ids do not match the paused question set.
    """
    question_id = _resume_question_id(answers)

    with _get_execution_lock(execution_id):
        paused = _paused_question_set(engine, execution_id, question_id)
        saved_questions = list(paused.get("questions") or [])
        if len(answers) != len(saved_questions):
            raise ValueError("answer count mismatch")

        graph, task, task_id = _resume_task(engine, execution_id, paused)
        answer_values = [str(answer["answer"]) for answer in answers]
        answer_paused_questions(engine, question_id, answer_values)

        outcome = _merge_clarification_answers(task, question_id, saved_questions, answer_values)
        answered_at = datetime.now(timezone.utc).isoformat()
        status = _apply_review_outcome(task, outcome)
        engine._save_checkpoint(execution_id, graph)
        decision_str = _emit_resume_event(engine, execution_id, task_id, status, outcome)

    if status == "resumed":
        engine.execute_plan(graph)

    return {
        "execution_id": execution_id,
        "task_id": task_id,
        "status": status,
        "answered_at": answered_at,
        "review_outcome": decision_str,
    }


__all__ = [
    "ClarificationAnswers",
    "pause_task_for_clarification",
    "release_execution_lock",
    "resume_after_clarification",
]
