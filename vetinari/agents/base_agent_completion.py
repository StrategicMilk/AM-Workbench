"""Task completion logic for BaseAgent.

Contains the ``complete_task`` function extracted from ``base_agent.py`` to
keep that file under the 550-line limit. The function runs post-execution
quality, feedback, learning, memory, and receipt subsystems.

Pipeline role: Execute -> Completion -> Verify -> Learn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import AgentResult, AgentTask
from vetinari.boundary_guards import assert_dependency_success
from vetinari.constants import TRUNCATE_OUTPUT_SUMMARY
from vetinari.guards import GateError
from vetinari.types import ConfidenceAction

logger = logging.getLogger(__name__)
_AUTO_CURATION_INTERVAL = 100
_EPISODE_OUTPUT_SUMMARY_LIMIT = 300
_TRAINING_PROMPT_PREFIX_LIMIT = 500


if TYPE_CHECKING:
    from vetinari.agents.base_agent import BaseAgent


def _guard_quality_gate_dependency(result: Any, quality_gate_id: str | None, failed_gates: set[str] | None) -> Any:
    if quality_gate_id is None and failed_gates is None:
        return None
    assert_dependency_success(quality_gate_id or "quality_gate", failed_gates or set())
    return result


def _is_agent_result_like(result: Any) -> bool:
    """Accept AgentResult objects across isolated test-module reload boundaries."""
    if isinstance(result, AgentResult):
        return True
    if type(result).__name__ != "AgentResult":
        return False
    return all(hasattr(result, field_name) for field_name in ("success", "output", "errors", "metadata"))


def _optional_numeric_agent_state(agent: BaseAgent, attribute: str) -> int | float | None:
    """Return a typed numeric signal without treating mock/dynamic attributes as data."""
    value = getattr(agent, attribute, None)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return None


@dataclass(frozen=True, slots=True)
class _CompletionScoring:
    output_str: str
    model_id: str
    task_type: str
    score: Any

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"output_str={self.output_str!r}, "
            f"model_id={self.model_id!r}, "
            f"task_type={self.task_type!r}, "
            f"score={self.score!r}"
            ")"
        )


def complete_task(
    agent: BaseAgent | None = None,
    task: AgentTask | None = None,
    result: AgentResult | Any = None,
    *,
    quality_gate_id: str | None = None,
    failed_gates: set[str] | None = None,
) -> AgentTask | Any:
    """Run all post-execution subsystems and mark the task complete.

    Called by ``BaseAgent._execute_safely`` after a successful execution.
    Stamps the task with a completion timestamp, runs quality scoring and
    feedback recording, enforces quality gates, and stores the execution in
    training data and episodic memory.

    Args:
        agent: The BaseAgent instance whose subsystems should be used.
        task: The completed task; mutated in-place with timestamps and scores.
        result: The AgentResult produced by the execution function.
        quality_gate_id: Optional quality gate identifier to enforce.
        failed_gates: Optional set of failed quality gate identifiers.

    Returns:
        The mutated task with completion metadata populated.

    Raises:
        ValueError: If ``task`` is missing when an ``agent`` is supplied.
    """
    guard_result = _guard_quality_gate_dependency(result, quality_gate_id, failed_gates)
    if guard_result is not None:
        return guard_result
    if agent is None or task is None or not _is_agent_result_like(result):
        raise TypeError("complete_task requires agent, task, and AgentResult outside guard-only validation")

    _attach_quality_gate(agent, task, result)

    if not (result.success and result.output):
        _stamp_task_completion(task, result)
        _emit_task_completed_trace(agent, task, result)
        _emit_completion_receipt(agent, task, result, score=0.0, scoring_available=False)
        return task

    scoring = _score_task_completion(agent, task, result)
    if scoring is None:
        _stamp_task_completion(task, result)
        _emit_task_completed_trace(agent, task, result)
        _emit_completion_receipt(agent, task, result, score=0.0, scoring_available=False)
        return task

    _record_drift_detection(agent, result, scoring.score.overall_score)
    _enforce_quality_gate(agent, task, result, scoring.score.overall_score)
    _stamp_task_completion(task, result)
    _emit_task_completed_trace(agent, task, result)
    _record_prompt_evolver_result(agent, scoring.score.overall_score)
    _record_training_data(agent, task, result, scoring)
    episode_id = _record_episode_memory(agent, task, result, scoring)
    _record_feedback_loop(agent, task, result, scoring, episode_id)
    _record_difficulty_feedback(task, result, scoring, episode_id)
    _record_unified_memory(agent, task, result, scoring)
    _emit_completion_receipt(agent, task, result, score=scoring.score.overall_score)
    return task


def _stamp_task_completion(task: AgentTask, result: AgentResult) -> None:
    task.completed_at = datetime.now(timezone.utc).isoformat()
    task.result = result.output
    if not result.success:
        task.error = "; ".join(result.errors)


def _emit_task_completed_trace(agent: BaseAgent, task: AgentTask, result: AgentResult) -> None:
    try:
        from vetinari.structured_logging import log_event

        log_event(
            "info",
            f"agent.{agent.agent_type.value}",
            "task_completed",
            task_id=task.task_id,
            success=result.success,
            agent=agent.agent_type.value,
        )
    except Exception:
        logger.warning("Failed to emit structured trace span for task_completed", exc_info=True)


def _attach_quality_gate(agent: BaseAgent, task: AgentTask, result: AgentResult) -> None:
    if not (result.success and result.output):
        return

    from vetinari.agents.base_agent import _get_agent_constraints

    constraints = _get_agent_constraints(agent.agent_type.value)
    if constraints and constraints.quality_gate and not hasattr(task, "_quality_gate"):
        task._quality_gate = constraints.quality_gate


def _task_type_for_completion(agent: BaseAgent) -> str:
    raw_task_type = getattr(agent, "_current_task_type", None)
    return raw_task_type if isinstance(raw_task_type, str) and raw_task_type else agent.agent_type.value.lower()


def _stringify_output(output: object) -> str:
    import json as _json

    if isinstance(output, str):
        return output
    return _json.dumps(output, default=str)[:TRUNCATE_OUTPUT_SUMMARY]


def _resolve_model_id(agent: BaseAgent) -> str:
    model_id = agent._last_inference_model_id or agent.default_model or ""
    if not model_id and agent._adapter_manager:
        try:
            loaded_models = getattr(agent._adapter_manager, "_loaded_models", {})
            if loaded_models:
                model_id = next(iter(loaded_models))
        except Exception:
            logger.warning("Could not resolve model_id from adapter manager")
    return model_id or "default"


def _score_task_completion(
    agent: BaseAgent,
    task: AgentTask,
    result: AgentResult,
) -> _CompletionScoring | None:
    try:
        from vetinari.learning.quality_scorer import get_quality_scorer

        output_str = _stringify_output(result.output)
        model_id = _resolve_model_id(agent)
        task_type = _task_type_for_completion(agent)
        scorer = get_quality_scorer()
        scorer._adapter_manager = agent._adapter_manager
        score = scorer.score(
            task_id=task.task_id,
            model_id=model_id,
            task_type=task_type,
            task_description=task.description or "",
            output=output_str,
            inference_confidence=_optional_numeric_agent_state(agent, "_last_inference_confidence"),
            use_llm=False,
        )
        return _CompletionScoring(output_str=output_str, model_id=model_id, task_type=task_type, score=score)
    except Exception as exc:
        logger.error("Quality scoring failed during task completion", exc_info=True)
        raise GateError("scoring", "quality scorer failed", exc) from exc


def _record_drift_detection(agent: BaseAgent, result: AgentResult, quality_score: float) -> None:
    try:
        from vetinari.agents.drift_detector import get_drift_detector

        session_id = getattr(agent, "_session_id", "default")
        detector = get_drift_detector()
        detector.record_score(agent.agent_type, session_id, quality_score)
        drift_report = detector.check_drift(agent.agent_type, session_id)
        if drift_report:
            result.metadata["drift_detected"] = True
            result.metadata["drift_magnitude"] = drift_report.drift_magnitude
    except Exception:
        # non-blocking: drift detection is observability-only.
        logger.warning(
            "Drift detection failed for %s (non-fatal)",
            agent.agent_type.value,
            exc_info=True,
        )


def _enforce_quality_gate(agent: BaseAgent, task: AgentTask, result: AgentResult, quality_score: float) -> None:
    effective_score = _effective_quality_score(result, quality_score)
    self_reflection = result.metadata.get("self_reflection", {})
    skip_gate = bool(self_reflection.get("is_improved"))
    if hasattr(task, "_quality_gate") and task._quality_gate and not skip_gate:
        _check_registered_quality_gate(agent, result, effective_score)
        if not result.success:
            assert_dependency_success(str(task._quality_gate), {str(task._quality_gate)})
    _mark_critical_quality_escalation(agent, result, effective_score, skip_gate)


def _effective_quality_score(result: AgentResult, quality_score: float) -> float:
    if result.metadata.get("self_check_gate_hint") != "stricter":
        return quality_score

    effective_score = max(0.0, quality_score - 0.1)
    logger.warning(
        "Quality gate using adjusted score %.2f (raw=%.2f) due to self_check failure",
        effective_score,
        quality_score,
    )
    return effective_score


def _check_registered_quality_gate(agent: BaseAgent, result: AgentResult, effective_score: float) -> None:
    try:
        from vetinari.constraints.registry import get_constraint_registry

        passed, reason = get_constraint_registry().check_quality_gate(agent.agent_type.value, effective_score)
        if not passed:
            logger.warning(
                "Quality gate failed for %s - marking execution as failed: %s",
                agent.agent_type.value,
                reason,
            )
            result.success = False
            result.errors.append(f"Quality gate failed: {reason}")
    except Exception as exc:
        logger.error(
            "Failed to check quality gate for %s - blocking result",
            agent.agent_type.value,
            exc_info=True,
        )
        raise GateError(
            "quality_gate",
            f"registry unavailable for {agent.agent_type.value}",
            exc,
        ) from exc


def _mark_critical_quality_escalation(
    agent: BaseAgent,
    result: AgentResult,
    effective_score: float,
    skip_gate: bool,
) -> None:
    confidence_decision = getattr(agent, "_last_confidence_decision", None)
    confidence_action = getattr(confidence_decision, "action", None)
    if confidence_action in (ConfidenceAction.BEST_OF_N, ConfidenceAction.DEFER_TO_HUMAN):
        logger.warning("Confidence gate requested escalation/retry via %s", confidence_action.value)
        result.metadata["quality_escalation_required"] = True
        result.metadata["confidence_escalation_action"] = confidence_action.value
        result.errors.append(f"Confidence gate requested {confidence_action.value}; retry or escalation required")
    if skip_gate:
        return
    try:
        from vetinari.constants import CRITICAL_QUALITY_THRESHOLD

        if effective_score < CRITICAL_QUALITY_THRESHOLD:
            logger.warning(
                "Quality score %.2f is below critical threshold %.2f - marking result for escalation/retry",
                effective_score,
                CRITICAL_QUALITY_THRESHOLD,
            )
            result.metadata["quality_escalation_required"] = True
            result.metadata["quality_escalation_score"] = effective_score
            result.errors.append(
                f"Quality score {effective_score:.2f} below critical threshold "
                f"{CRITICAL_QUALITY_THRESHOLD:.2f} - retry or escalation required"
            )
    except Exception:
        logger.warning("Quality escalation check failed (non-fatal)", exc_info=True)


def _record_prompt_evolver_result(agent: BaseAgent, quality_score: float) -> None:
    try:
        from vetinari.learning.prompt_evolver import get_prompt_evolver

        variant_id = getattr(agent, "_last_variant_id", None) or "default"
        if variant_id and variant_id != "default":
            get_prompt_evolver().record_result(agent.agent_type.value, variant_id, quality_score)
    except Exception:
        logger.warning(
            "Failed to record prompt evolver result for %s",
            agent.agent_type.value,
            exc_info=True,
        )


def _record_training_data(
    agent: BaseAgent,
    task: AgentTask,
    result: AgentResult,
    scoring: _CompletionScoring,
) -> None:
    try:
        from vetinari.learning.training_data import get_training_collector

        collector = get_training_collector()
        collector.record(
            task=task.description or "",
            prompt=agent.get_system_prompt()[:_TRAINING_PROMPT_PREFIX_LIMIT]
            + "\n\n"
            + (task.prompt or task.description or ""),
            response=scoring.output_str,
            score=scoring.score.overall_score,
            model_id=scoring.model_id,
            task_type=scoring.task_type,
            agent_type=agent.agent_type.value,
            success=result.success,
            latency_ms=int(getattr(agent, "_last_latency_ms", 0) or 0),
            tokens_used=getattr(agent, "_last_tokens_used", 0) or 0,
            prompt_variant_id=getattr(agent, "_last_variant_id", "") or "",
            trace_id=getattr(agent, "_last_trace_id", "") or "",
        )
        _maybe_trigger_auto_curation(collector.count_records())
    except Exception:
        logger.warning("Failed to record execution to training data collector", exc_info=True)


def _maybe_trigger_auto_curation(total_records: int) -> None:
    if total_records <= 0 or total_records % _AUTO_CURATION_INTERVAL != 0:
        return

    import threading as _threading

    def _run_curation() -> None:
        try:
            from vetinari.training.pipeline import DataCurator

            DataCurator().curate(min_score=0.8, max_examples=5000)
            logger.info("[TrainingData] Auto-curation triggered at %d records", total_records)
        except Exception as exc:
            logger.warning("Auto-curation failed (non-fatal): %s", exc)

    _threading.Thread(target=_run_curation, daemon=True, name="auto-curator").start()


def _record_episode_memory(
    agent: BaseAgent,
    task: AgentTask,
    result: AgentResult,
    scoring: _CompletionScoring,
) -> str | None:
    try:
        from vetinari.learning.episode_memory import get_episode_memory

        return get_episode_memory().record(
            task_description=task.description or "",
            agent_type=agent.agent_type.value,
            task_type=scoring.task_type,
            output_summary=scoring.output_str[:_EPISODE_OUTPUT_SUMMARY_LIMIT],
            quality_score=scoring.score.overall_score,
            success=result.success,
            model_id=scoring.model_id,
        )
    except Exception:
        logger.warning("Failed to record to episodic memory", exc_info=True)
        return None


def _record_feedback_loop(
    agent: BaseAgent,
    task: AgentTask,
    result: AgentResult,
    scoring: _CompletionScoring,
    episode_id: str | None,
) -> None:
    try:
        from vetinari.learning.feedback_loop import get_feedback_loop

        get_feedback_loop().record_outcome(
            task_id=task.task_id,
            model_id=scoring.model_id,
            task_type=scoring.task_type,
            quality_score=scoring.score.overall_score,
            latency_ms=_optional_numeric_agent_state(agent, "_last_latency_ms"),
            success=result.success,
            episode_id=episode_id,
            confidence=_optional_numeric_agent_state(agent, "_last_inference_confidence"),
            input_tokens=_optional_numeric_agent_state(agent, "_last_input_tokens"),
            output_tokens=_optional_numeric_agent_state(agent, "_last_output_tokens"),
        )
    except Exception:
        logger.warning("Feedback loop recording failed during task completion", exc_info=True)


def _record_difficulty_feedback(
    task: AgentTask,
    result: AgentResult,
    scoring: _CompletionScoring,
    episode_id: str | None,
) -> None:
    try:
        from vetinari.learning.difficulty_feedback import record_difficulty_feedback
        from vetinari.models.model_router_scoring import assess_difficulty

        predicted = assess_difficulty(task.description or "", scoring.task_type)
        record_difficulty_feedback(
            task_type=scoring.task_type,
            predicted=predicted,
            signals={
                "quality_score": scoring.score.overall_score,
                "success": result.success,
                "duration_ms": getattr(result, "duration_ms", 0.0),
                "retries": getattr(result, "retries", 0),
                "rejections": getattr(result, "rejections", 0),
            },
            episode_id=episode_id,
        )
    except Exception:
        logger.warning("Failed to record difficulty calibration feedback", exc_info=True)


def _record_unified_memory(
    agent: BaseAgent,
    task: AgentTask,
    result: AgentResult,
    scoring: _CompletionScoring,
) -> None:
    try:
        from vetinari.memory.shared import get_shared_memory

        get_shared_memory().store.record_episode(
            task_description=task.description or "",
            agent_type=agent.agent_type.value,
            task_type=scoring.task_type,
            output_summary=scoring.output_str[:_EPISODE_OUTPUT_SUMMARY_LIMIT],
            quality_score=scoring.score.overall_score,
            success=result.success,
            model_id=scoring.model_id,
        )
    except Exception:
        logger.warning("Failed to record episode to unified memory", exc_info=True)


def _emit_completion_receipt(
    agent: BaseAgent,
    task: AgentTask,
    result: AgentResult,
    *,
    score: float,
    scoring_available: bool = True,
) -> None:
    """Emit a WorkReceipt for the completed task.

    Wraps ``record_agent_completion`` so the import is local to this helper and
    a failure in receipt emission never crashes ``complete_task``.

    Args:
        agent: The BaseAgent instance whose work this receipt records.
        task: The completed task.
        result: The AgentResult from execution.
        score: Quality score. Use ``0.0`` for early returns where scoring did
            not run.
        scoring_available: True when the quality scorer produced ``score``.
    """
    try:
        from vetinari.receipts import record_agent_completion

        record_agent_completion(
            agent=agent,
            task=task,
            result=result,
            score=score,
            scoring_available=scoring_available,
        )
    except Exception:
        logger.warning("Failed to emit WorkReceipt during task completion", exc_info=True)
