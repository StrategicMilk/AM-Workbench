"""Finalization helpers for pipeline stage runtime results."""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


def finalize_pipeline_runtime_result(
    *,
    result_dict: dict[str, Any],
    context: dict[str, Any],
    graph: Any,
    exec_results: dict[str, Any],
    final_output: Any,
    goal: str,
    total_time: int,
    _pipeline_span: Any,
    _corr_ctx: Any,
    _intake_tier: Any,
    _intake_features: Any,
    goal_verification_report: Any,
) -> dict[str, Any]:
    """Apply telemetry, tracing, tier learning, and conversation side effects.

    Returns:
        Value produced for the caller.
    """
    _add_decision_trailers(result_dict, context)
    _close_pipeline_span(_pipeline_span, exec_results)
    _exit_correlation_context(_corr_ctx)
    _record_tier_outcome(_intake_tier, _intake_features, exec_results, goal_verification_report)
    quality_score = _feed_spc_metrics(result_dict, exec_results, total_time)
    _track_conversation(context, graph, goal, final_output, quality_score)
    return result_dict


def _add_decision_trailers(result_dict: dict[str, Any], context: dict[str, Any]) -> None:
    try:
        import vetinari.git.trailers as git_trailers

        pipeline_trace = context.get("_trace_id") or context.get("_exec_id")
        trailers = git_trailers.generate_trailers(trace_id=str(pipeline_trace)) if pipeline_trace else None
        if trailers:
            result_dict["decision_trailers"] = trailers
    except Exception:
        logger.warning("Decision trailer generation skipped; trailer system unavailable")


def _close_pipeline_span(pipeline_span: Any, exec_results: dict[str, Any]) -> None:
    if pipeline_span is None:
        return
    try:
        import vetinari.observability.otel_genai as otel_genai

        status = "ok" if exec_results.get(StatusEnum.FAILED.value, 0) == 0 else "error"
        otel_genai.get_genai_tracer().end_agent_span(pipeline_span, status=status)
    except (ImportError, AttributeError):
        logger.warning("Failed to close GenAI pipeline span")


def _exit_correlation_context(corr_ctx: Any) -> None:
    if corr_ctx is None:
        return
    try:
        corr_ctx.__exit__(None, None, None)
    except (AttributeError, TypeError):
        logger.warning("Failed to exit CorrelationContext after pipeline", exc_info=True)


def _record_tier_outcome(
    intake_tier: Any,
    intake_features: Any,
    exec_results: dict[str, Any],
    goal_verification_report: Any,
) -> None:
    if intake_tier is None or intake_features is None:
        return
    if goal_verification_report is None:
        logger.info("[Pipeline] Thompson tier outcome skipped: goal verification did not produce a quality score")
        return
    try:
        import vetinari.learning.model_selector as model_selector

        quality = _tier_quality(exec_results, goal_verification_report)
        rework = getattr(goal_verification_report, "corrective_rounds", 0)
        model_selector.get_thompson_selector().update_tier(
            pattern_key=intake_features.pattern_key,
            tier_used=intake_tier.value,
            quality_score=quality,
            rework_count=rework,
        )
        logger.info(
            "[Pipeline] Thompson tier outcome recorded: tier=%s, quality=%.2f, rework=%d",
            intake_tier.value,
            quality,
            rework,
        )
    except Exception:
        logger.warning("Thompson tier outcome recording failed (non-fatal)", exc_info=True)


def _tier_quality(exec_results: dict[str, Any], goal_verification_report: Any) -> float:
    if goal_verification_report is not None:
        return goal_verification_report.compliance_score
    completed = exec_results.get(StatusEnum.COMPLETED.value, 0)
    failed = exec_results.get(StatusEnum.FAILED.value, 0)
    return completed / max(completed + failed, 1)


def _feed_spc_metrics(result_dict: dict[str, Any], exec_results: dict[str, Any], total_time: int) -> Any:
    try:
        import vetinari.workflow as workflow

        spc = workflow.get_spc_monitor()
        quality_score = result_dict.get("review_result", {}).get("quality_score")
        if isinstance(quality_score, (int, float)):
            spc.update("quality_score", float(quality_score))
        spc.update("latency_ms", float(total_time))
        total_tokens = _total_tokens_used(exec_results)
        if total_tokens > 0:
            spc.update("token_count", float(total_tokens))
        logger.debug(
            "[Pipeline] SPC metrics fed: quality=%s, latency=%dms, tokens=%d",
            quality_score,
            total_time,
            total_tokens,
        )
        return quality_score
    except Exception:
        logger.warning("SPC metric feed failed (non-fatal)", exc_info=True)
        return None


def _total_tokens_used(exec_results: dict[str, Any]) -> int:
    total = 0
    for task_result in exec_results.get("task_results", {}).values():
        if isinstance(task_result, dict):
            total += int(task_result.get("tokens_used", 0))
    return total


def _track_conversation(context: dict[str, Any], graph: Any, goal: str, final_output: Any, quality_score: Any) -> None:
    try:
        import vetinari.async_support.conversation as conversation_store

        conv = conversation_store.get_conversation_store()
        session_id = context.get("session_id") or graph.plan_id
        with contextlib.suppress(ValueError, KeyError):
            conv.create_session(session_id)
        conv.add_message(session_id, "user", goal)
        conv.add_message(
            session_id,
            "assistant",
            str(final_output)[:2000] if final_output else "(no output)",
            metadata={"plan_id": graph.plan_id, "quality_score": quality_score},
        )
    except Exception:
        logger.warning("ConversationStore tracking failed (non-fatal)", exc_info=True)
