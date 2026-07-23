"""Pipeline execution setup steps used by the public engine facade.

This module owns the pre-execution half of a pipeline run: trace setup,
intake tier handling, prevention, planning, and plan validation support.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from vetinari.boundary_guards import account_evidence_drop
from vetinari.orchestration.pipeline_events import PipelineStage
from vetinari.types import StatusEnum

from .pipeline_engine_planning_steps import _finish_pipeline_planning


@dataclass(frozen=True, slots=True)
class _PipelineExecutionInputs:
    """Arguments passed from the public execution compatibility wrapper."""

    self_obj: Any
    goal: str
    constraints: dict[str, Any] | None
    context: dict[str, Any]
    stages: dict[str, Any]
    start_time: float
    corr_ctx: Any
    pipeline_span: Any
    intake_tier: Any
    intake_features: Any
    task_handler: Callable[..., Any] | None
    project_id: str | None
    model_id: str | None
    contextlib_module: Any
    log_event_fn: Callable[..., None]
    logger: logging.Logger
    logger_name: str
    save_pipeline_checkpoint: Callable[..., None]

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"self_obj={self.self_obj!r}, "
            f"goal={self.goal!r}, "
            f"constraints={self.constraints!r}, "
            f"context={self.context!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class _PipelinePlanningContext:
    """Resolved context produced before plan generation starts."""

    pipeline_task_id: str
    state_store: Any
    enriched_goal: str


def _ensure_pipeline_trace(self: Any, context: dict[str, Any]) -> str:
    """Ensure the mutable context has a stable trace identifier."""
    pipeline_task_id = str(context.get("_exec_id") or context.get("_trace_id") or "")
    if not context.get("_trace_id"):
        context["_trace_id"] = str(uuid.uuid4())
    self._current_trace_id = str(context["_trace_id"])
    return pipeline_task_id


def _load_pipeline_resume_state(
    pipeline_task_id: str,
    stages: dict[str, Any],
    logger: logging.Logger,
) -> Any:
    """Load prior pipeline checkpoint metadata for observability."""
    if not pipeline_task_id:
        return None
    try:
        from vetinari.orchestration.pipeline_state import get_pipeline_state_store

        state_store = get_pipeline_state_store()
        resume_point = state_store.get_resume_point(pipeline_task_id)
        if resume_point is not None:
            completed_stages = state_store.get_completed_stages(pipeline_task_id)
            logger.info(
                "[Pipeline] Task %s has prior checkpoint (%d stages recorded). "
                "Mid-pipeline resume is not implemented - re-executing from start.",
                pipeline_task_id,
                len(completed_stages),
            )
            stages["last_run_stage_hint"] = completed_stages[-1] if completed_stages else None
        return state_store
    except Exception:
        logger.warning("Pipeline state store unavailable - no resume capability", exc_info=True)
        return None


def _route_system_decision(
    self: Any,
    enriched_goal: str,
    context: dict[str, Any],
    stages: dict[str, Any],
    intake_tier: Any,
    logger: logging.Logger,
) -> None:
    """Record optional System 1/System 2 routing metadata."""
    try:
        from vetinari.routing.system_router import route_system

        decision = route_system(
            description=enriched_goal,
            intake_tier=intake_tier.value if intake_tier is not None else None,
            complexity=context.get("intake_features", {}).get("complexity"),
            confidence=context.get("intake_confidence", 0.5),
            involves_code_generation=True,
        )
        context["system_decision"] = decision.to_dict()
        stages["system_routing"] = {
            "system_type": decision.system_type.value,
            "model_tier": decision.model_tier.value,
            "skip_foreman": decision.skip_foreman,
            "skip_inspector": decision.skip_inspector,
        }
    except Exception:
        logger.warning("System routing unavailable, proceeding with full pipeline", exc_info=True)


def _handle_intake_tier(
    self: Any,
    goal: str,
    enriched_goal: str,
    context: dict[str, Any],
    stages: dict[str, Any],
    start_time: float,
    corr_ctx: Any,
    pipeline_span: Any,
    intake_tier: Any,
    intake_features: Any,
    task_handler: Callable[..., Any] | None,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    """Handle express and clarification intake tiers before full planning."""
    if intake_tier is None:
        return None
    get_bound = object.__getattribute__
    try:
        from vetinari.orchestration.intake import CONFIDENCE_THRESHOLD, PipelinePaused
        from vetinari.orchestration.intake import Tier as IntakeTier

        if intake_tier == IntakeTier.EXPRESS:
            logger.info("[Pipeline] Express lane: skipping planning, direct to Builder")
            execute_express = cast(Callable[..., dict[str, Any]], get_bound(self, "_execute_express"))
            return execute_express(
                enriched_goal,
                context,
                stages,
                start_time,
                corr_ctx,
                pipeline_span,
                task_handler=task_handler,
            )
        needs_clarify = intake_tier == IntakeTier.CUSTOM or (
            intake_features is not None and intake_features.confidence < CONFIDENCE_THRESHOLD
        )
        if not needs_clarify:
            return None
        clarify_result = get_bound(self, "_run_clarification")(enriched_goal, context)
        if not isinstance(clarify_result, dict):
            return None
        if clarify_result.get("needs_user_input"):
            paused = PipelinePaused(
                questions=clarify_result.get("pending_questions", []),
                pipeline_state={"goal": goal, "tier": intake_tier.value, "context": context},
                tier=intake_tier.value,
                goal=goal,
                confidence=intake_features.confidence if intake_features else 0.0,
            )
            stages["clarification"] = {"paused": True, "questions": len(paused.questions)}
            logger.info("[Pipeline] Paused for clarification: %d questions", len(paused.questions))
            return cast(dict[str, Any], paused.to_dict())
        context.update({key: value for key, value in clarify_result.items() if key.startswith("clarification_")})
        stages["clarification"] = {"paused": False, "enriched": True}
        return None
    except Exception:
        logger.warning("Tier routing/clarification failed, proceeding with full pipeline", exc_info=True)
        return None


def _build_request_spec(
    self: Any,
    enriched_goal: str,
    constraints: dict[str, Any] | None,
    context: dict[str, Any],
    stages: dict[str, Any],
    logger: logging.Logger,
) -> None:
    """Build and attach the request specification used by downstream tasks."""
    get_bound = object.__getattribute__
    try:
        from vetinari.orchestration.request_spec import Tier as SpecTier
        from vetinari.orchestration.request_spec import get_spec_builder

        spec_tier = SpecTier(context.get("intake_tier", "standard")) if "intake_tier" in context else SpecTier.STANDARD
        category = (context.get("category") or "").strip()
        if not category:
            category = get_bound(self, "_analyze_input")(enriched_goal, constraints or {}).get("goal_type", "general")
        request_spec = get_spec_builder().build(goal=enriched_goal, tier=spec_tier, category=category)
        request_spec_payload = request_spec.to_dict()
        context["request_spec"] = request_spec_payload
        context.setdefault("acceptance_criteria", list(request_spec.acceptance_criteria))
        context.setdefault("referenced_files", list(request_spec.scope))
        stages["request_spec"] = {
            "confidence": request_spec.confidence,
            "complexity": request_spec.estimated_complexity,
            "scope_files": len(request_spec.scope),
            "criteria_count": len(request_spec.acceptance_criteria),
        }
        logger.info(
            "[Pipeline] RequestSpec built: complexity=%d, confidence=%.2f, scope=%d files",
            request_spec.estimated_complexity,
            request_spec.confidence,
            len(request_spec.scope),
        )
    except Exception:
        logger.warning("RequestSpec builder unavailable, proceeding without spec", exc_info=True)


def _run_prevention_stage(
    self: Any,
    enriched_goal: str,
    context: dict[str, Any],
    stages: dict[str, Any],
    pipeline_task_id: str,
    state_store: Any,
    contextlib_module: Any,
    save_pipeline_checkpoint: Callable[..., None],
    logger: logging.Logger,
) -> dict[str, Any] | None:
    """Run the prevention gate and return a blocking result on failure."""
    self._emit(PipelineStage.PREVENTION, "stage_started")
    prevention_passed = object.__getattribute__(self, "_run_prevention_gate")(enriched_goal, context)
    stages["prevention_gate"] = {"passed": prevention_passed}
    self._emit(PipelineStage.PREVENTION, "stage_completed", {"passed": prevention_passed})
    if state_store and pipeline_task_id:
        item = {"pipeline_task_id": pipeline_task_id, "stage": "prevention_gate", "passed": prevention_passed}
        try:
            state_store.mark_stage_complete(pipeline_task_id, "prevention_gate", {"passed": prevention_passed})
        except Exception:
            account_evidence_drop(item, "execution_state", logger=logger)
            raise
    save_pipeline_checkpoint(
        trace_id=str(context.get("_trace_id") or pipeline_task_id),
        execution_id=str(context.get("_exec_id") or pipeline_task_id),
        step_name="prevention_gate",
        step_index=0,
        status="completed" if prevention_passed else "failed",
        output_snapshot={"passed": prevention_passed},
    )
    if prevention_passed:
        return None
    logger.warning("[Pipeline] Prevention gate failed - blocking execution")
    stages["prevention_gate"][StatusEnum.BLOCKED.value] = True
    return {"success": False, "error": "Prevention gate check failed", "stages": stages}


def _run_input_analysis_stage(
    self: Any,
    goal: str,
    enriched_goal: str,
    constraints: dict[str, Any] | None,
    context: dict[str, Any],
    stages: dict[str, Any],
    pipeline_task_id: str,
    state_store: Any,
    contextlib_module: Any,
    save_pipeline_checkpoint: Callable[..., None],
    logger: logging.Logger,
) -> dict[str, Any]:
    """Run input analysis and persist its replay checkpoint."""
    self._emit(PipelineStage.INTAKE, "stage_started", {"goal": goal[:200]})
    logger.info("[Pipeline] Stage 1: Input Analysis for goal: %s", goal[:80])
    analysis = object.__getattribute__(self, "_analyze_input")(enriched_goal, constraints or {})
    stages["input_analysis"] = analysis
    if state_store and pipeline_task_id:
        item = {"pipeline_task_id": pipeline_task_id, "stage": "input_analysis"}
        try:
            state_store.mark_stage_complete(pipeline_task_id, "input_analysis")
        except Exception:
            account_evidence_drop(item, "execution_state", logger=logger)
            raise
    save_pipeline_checkpoint(
        trace_id=str(context.get("_trace_id") or pipeline_task_id),
        execution_id=str(context.get("_exec_id") or pipeline_task_id),
        step_name="input_analysis",
        step_index=1,
        status="completed",
        output_snapshot={
            key: value for key, value in analysis.items() if key in ("goal_type", "complexity", "requires_planning")
        },
    )
    return analysis


def _prepare_pipeline_planning_context(
    args: _PipelineExecutionInputs,
) -> tuple[_PipelinePlanningContext | None, dict[str, Any] | None]:
    """Run setup, intake, prevention, and input analysis stages."""
    self_obj = args.self_obj
    pipeline_task_id = _ensure_pipeline_trace(self_obj, args.context)
    state_store = _load_pipeline_resume_state(pipeline_task_id, args.stages, args.logger)
    enriched_goal = object.__getattribute__(self_obj, "_enrich_goal")(args.goal, args.context)
    _route_system_decision(self_obj, enriched_goal, args.context, args.stages, args.intake_tier, args.logger)
    intake_result = _handle_intake_tier(
        self_obj,
        args.goal,
        enriched_goal,
        args.context,
        args.stages,
        args.start_time,
        args.corr_ctx,
        args.pipeline_span,
        args.intake_tier,
        args.intake_features,
        args.task_handler,
        args.logger,
    )
    if intake_result is not None:
        return None, intake_result

    _build_request_spec(self_obj, enriched_goal, args.constraints, args.context, args.stages, args.logger)
    blocked = _run_prevention_stage(
        self_obj,
        enriched_goal,
        args.context,
        args.stages,
        pipeline_task_id,
        state_store,
        args.contextlib_module,
        args.save_pipeline_checkpoint,
        args.logger,
    )
    if blocked is not None:
        return None, blocked
    _run_input_analysis_stage(
        self_obj,
        args.goal,
        enriched_goal,
        args.constraints,
        args.context,
        args.stages,
        pipeline_task_id,
        state_store,
        args.contextlib_module,
        args.save_pipeline_checkpoint,
        args.logger,
    )
    return _PipelinePlanningContext(pipeline_task_id, state_store, enriched_goal), None


def _execute_pipeline_steps(args: _PipelineExecutionInputs) -> dict[str, Any]:
    """Run all pre-model-assignment pipeline steps for the facade."""
    if object.__getattribute__(args.self_obj, "is_paused")():
        args.logger.warning("Pipeline is paused (Andon halt) - skipping execution")
        args.log_event_fn(
            "warning",
            args.logger_name,
            "pipeline_paused",
            event_type="pipeline_paused",
            reason="andon_halt",
        )
        return {"status": "paused", "reason": "Andon halt active"}

    planning_context, early_result = _prepare_pipeline_planning_context(args)
    if early_result is not None:
        return early_result
    if planning_context is None:
        return {
            "success": False,
            "error": "Pipeline planning context unavailable",
            "stages": args.stages,
        }
    return _finish_pipeline_planning(args, planning_context)
