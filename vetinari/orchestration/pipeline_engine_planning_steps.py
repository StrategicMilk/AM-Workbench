"""Plan-generation helpers for pipeline engine execution.

This module owns the planning context enrichment, plan-boundary validation, and
handoff into model assignment after intake and prevention pass.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from vetinari.boundary_guards import account_evidence_drop
from vetinari.orchestration.pipeline_events import PipelineStage

from .pipeline_engine_model_assignment import assign_models_and_run_execution


def _build_plan_constraints(
    constraints: dict[str, Any] | None,
    context: dict[str, Any],
    memory_context: Any,
) -> dict[str, Any]:
    """Merge request metadata and memory into plan-generation constraints."""
    plan_constraints = dict(constraints or {})
    for key in (
        "category",
        "tech_stack",
        "priority",
        "platforms",
        "required_features",
        "things_to_avoid",
        "expected_outputs",
    ):
        if key in context and key not in plan_constraints:
            plan_constraints[key] = context[key]
    if memory_context:
        plan_constraints["memory_context"] = memory_context
    return plan_constraints


def _load_accepted_adrs(plan_constraints: dict[str, Any], logger: logging.Logger) -> None:
    """Add accepted ADR summaries to planning constraints when available."""
    try:
        from vetinari.adr import ADRSystem

        accepted_adrs = ADRSystem().list_adrs(status="accepted", limit=10)
        if not accepted_adrs:
            return
        plan_constraints["prior_adr_decisions"] = [
            {"id": adr.adr_id, "title": adr.title, "category": adr.category, "decision": adr.decision[:300]}
            for adr in accepted_adrs
        ]
        logger.info("[Pipeline] Injected %d ADR decision(s) into planning context", len(accepted_adrs))
    except Exception:
        logger.warning("ADR loading for planning context failed (non-fatal)", exc_info=True)


def _select_meta_strategy(enriched_goal: str, context: dict[str, Any], logger: logging.Logger) -> None:
    """Record a MetaAdapter strategy for the current planning request."""
    try:
        from vetinari.learning.meta_adapter import get_meta_adapter

        strategy = get_meta_adapter().select_strategy(
            task_description=enriched_goal[:300],
            task_type=context.get("task_type", "general"),
        )
        context["strategy"] = strategy.to_dict()
        logger.info("MetaAdapter selected strategy: %s", strategy.source)
    except Exception:
        logger.warning("MetaAdapter strategy selection failed - using defaults")


def _load_workflow_recommendations(enriched_goal: str, stages: dict[str, Any], logger: logging.Logger) -> None:
    """Record optional workflow learner recommendations."""
    try:
        from vetinari.learning.workflow_learner import get_workflow_learner

        recommendations = get_workflow_learner().get_recommendations(enriched_goal)
        if recommendations.get("confidence", 0) > 0.5:
            stages["workflow_recommendations"] = recommendations
            logger.info(
                "WorkflowLearner: domain=%s, confidence=%.2f",
                recommendations.get("domain"),
                recommendations.get("confidence"),
            )
    except Exception:
        logger.warning("WorkflowLearner recommendations failed - using defaults")


def _detect_scope_creep(enriched_goal: str, graph: Any, stages: dict[str, Any], logger: logging.Logger) -> None:
    """Flag plan nodes that drift from the original goal."""
    try:
        from vetinari.drift.goal_tracker import create_goal_tracker

        tracker = create_goal_tracker(enriched_goal)
        creep_items = tracker.detect_scope_creep(list(graph.nodes.values()))
        if creep_items:
            logger.warning(
                "[Pipeline] Scope creep detected - %d/%d tasks have low goal relevance (plan_id=%s). Tasks: %s",
                len(creep_items),
                len(graph.nodes),
                graph.plan_id,
                [getattr(item, "task_id", str(item)) for item in creep_items[:5]],
            )
            stages["plan"]["scope_creep_count"] = len(creep_items)
    except ImportError:
        logger.debug("GoalTracker not available - scope-creep detection skipped")
    except Exception:
        logger.warning(
            "Scope-creep detection failed for plan %s - proceeding without drift guard", graph.plan_id, exc_info=True
        )


def _generate_planning_graph(
    self: Any,
    enriched_goal: str,
    constraints: dict[str, Any] | None,
    context: dict[str, Any],
    stages: dict[str, Any],
    logger: logging.Logger,
) -> Any:
    """Generate the execution graph after adding planning context."""
    memory_context = object.__getattribute__(self, "_retrieve_memory_for_planning")(enriched_goal)
    if memory_context:
        logger.info("[Pipeline] Enriched planning with %d memory entries", len(memory_context))
    plan_constraints = _build_plan_constraints(constraints, context, memory_context)
    _load_accepted_adrs(plan_constraints, logger)
    _select_meta_strategy(enriched_goal, context, logger)
    _load_workflow_recommendations(enriched_goal, stages, logger)
    graph = object.__getattribute__(self, "plan_generator").generate_plan(
        enriched_goal,
        plan_constraints,
        max_depth=object.__getattribute__(self, "_variant_manager").get_config().max_planning_depth,
    )
    stages["plan"] = {"plan_id": graph.plan_id, "tasks": len(graph.nodes)}
    _detect_scope_creep(enriched_goal, graph, stages, logger)
    return graph


def _record_plan_generation_stage(
    self: Any,
    graph: Any,
    context: dict[str, Any],
    pipeline_task_id: str,
    state_store: Any,
    contextlib_module: Any,
    save_pipeline_checkpoint: Any,
    logger: logging.Logger,
) -> None:
    """Emit and checkpoint the completed plan-generation stage."""
    self._emit(PipelineStage.PLAN_GEN, "stage_completed", {"plan_id": graph.plan_id, "task_count": len(graph.nodes)})
    if state_store and pipeline_task_id:
        item = {"pipeline_task_id": pipeline_task_id, "stage": "plan_gen", "plan_id": graph.plan_id}
        try:
            state_store.mark_stage_complete(
                pipeline_task_id,
                "plan_gen",
                {"plan_id": graph.plan_id, "tasks": len(graph.nodes)},
            )
        except Exception:
            account_evidence_drop(item, "plan_stage_checkpoint", logger=logger)
            raise
    save_pipeline_checkpoint(
        trace_id=str(context.get("_trace_id") or pipeline_task_id),
        execution_id=str(context.get("_exec_id") or pipeline_task_id),
        step_name="plan_gen",
        step_index=2,
        status="completed",
        output_snapshot={"plan_id": graph.plan_id, "task_count": len(graph.nodes)},
    )


def _set_correlation_plan_id(corr_ctx: Any, graph: Any, logger: logging.Logger) -> None:
    """Attach the generated plan identifier to structured log context."""
    if corr_ctx is None:
        return
    try:
        corr_ctx.set_plan_id(graph.plan_id)
    except (AttributeError, TypeError):
        logger.warning("Failed to set plan_id on CorrelationContext", exc_info=True)


def _plan_validation_error_result(
    goal: str,
    graph: Any,
    stages: dict[str, Any],
    start_time: float,
    plan_issues: list[str],
) -> dict[str, Any]:
    """Build the public result shape for a failed plan-boundary check."""
    return {
        "plan_id": graph.plan_id,
        "goal": goal,
        "completed": 0,
        "failed": 1,
        "error": f"Plan validation failed: {plan_issues}",
        "stages": stages,
        "total_time_ms": int((time.time() - start_time) * 1000),
    }


def _close_failed_plan_context(
    pipeline_span: Any,
    corr_ctx: Any,
    contextlib_module: Any,
    logger: logging.Logger,
) -> None:
    """Close optional observability contexts after a plan-boundary failure."""
    if pipeline_span is not None:
        try:
            from vetinari.observability.otel_genai import get_genai_tracer

            get_genai_tracer().end_agent_span(pipeline_span, status="error")
        except (ImportError, AttributeError):
            logger.warning("Failed to close GenAI span", exc_info=True)
    if corr_ctx is not None:
        with contextlib_module.suppress(Exception):
            corr_ctx.__exit__(None, None, None)


def _validate_plan_stage(
    self: Any,
    goal: str,
    graph: Any,
    stages: dict[str, Any],
    start_time: float,
    pipeline_span: Any,
    corr_ctx: Any,
    contextlib_module: Any,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    """Validate the generated plan before model assignment."""
    plan_valid, plan_issues = object.__getattribute__(self, "_validate_stage_boundary")(
        "plan",
        stages["plan"],
        min_keys=["plan_id", "tasks"],
    )
    if plan_valid:
        return None
    logger.warning("[Pipeline] Plan validation failed: %s", plan_issues)
    _close_failed_plan_context(pipeline_span, corr_ctx, contextlib_module, logger)
    return _plan_validation_error_result(goal, graph, stages, start_time, plan_issues)


def _inject_task_node_context(context: dict[str, Any], graph: Any, logger: logging.Logger) -> None:
    """Copy project metadata and request specs into each task node."""
    project_meta = {
        key: context[key]
        for key in (
            "category",
            "tech_stack",
            "priority",
            "platforms",
            "required_features",
            "things_to_avoid",
            "expected_outputs",
        )
        if key in context
    }
    if project_meta:
        for node in graph.nodes.values():
            node.input_data.setdefault("project_context", project_meta)
    request_spec = context.get("request_spec")
    if request_spec:
        for node in graph.nodes.values():
            node.input_data.setdefault("request_spec", request_spec)
        logger.debug(
            "[Pipeline] Injected request_spec into %d task node(s) (complexity=%s)",
            len(graph.nodes),
            request_spec.get("estimated_complexity", "?"),
        )


def _after_plan_halt_result(goal: str, graph: Any, stages: dict[str, Any], start_time: float) -> dict[str, Any]:
    """Build the public result shape for an Andon halt after planning."""
    return {
        "plan_id": graph.plan_id,
        "goal": goal,
        "completed": 0,
        "failed": 0,
        "error": "Pipeline halted by Andon signal after planning stage",
        "stages": stages,
        "total_time_ms": int((time.time() - start_time) * 1000),
    }


def _finish_pipeline_planning(args: Any, planning_context: Any) -> dict[str, Any]:
    """Generate, validate, annotate, and dispatch the execution graph."""
    self_obj = args.self_obj
    self_obj._emit(PipelineStage.PLAN_GEN, "stage_started")
    args.logger.info("[Pipeline] Stage 2-3: Plan Generation & Decomposition")
    graph = _generate_planning_graph(
        self_obj,
        planning_context.enriched_goal,
        args.constraints,
        args.context,
        args.stages,
        args.logger,
    )
    _record_plan_generation_stage(
        self_obj,
        graph,
        args.context,
        planning_context.pipeline_task_id,
        planning_context.state_store,
        args.contextlib_module,
        args.save_pipeline_checkpoint,
        args.logger,
    )
    _set_correlation_plan_id(args.corr_ctx, graph, args.logger)
    validation_error = _validate_plan_stage(
        self_obj,
        args.goal,
        graph,
        args.stages,
        args.start_time,
        args.pipeline_span,
        args.corr_ctx,
        args.contextlib_module,
        args.logger,
    )
    if validation_error is not None:
        return validation_error
    _inject_task_node_context(args.context, graph, args.logger)
    if object.__getattribute__(self_obj, "is_paused")():
        args.logger.warning("[Pipeline] Andon halt detected after planning - aborting")
        return _after_plan_halt_result(args.goal, graph, args.stages, args.start_time)
    return assign_models_and_run_execution(
        self_obj,
        goal=args.goal,
        graph=graph,
        context=args.context,
        stages=args.stages,
        start_time=args.start_time,
        _corr_ctx=args.corr_ctx,
        _pipeline_span=args.pipeline_span,
        task_handler=args.task_handler,
        project_id=args.project_id,
        _intake_tier=args.intake_tier,
        _intake_features=args.intake_features,
        _pipeline_task_id=planning_context.pipeline_task_id,
        _state_store=planning_context.state_store,
        contextlib_module=args.contextlib_module,
        save_pipeline_checkpoint=args.save_pipeline_checkpoint,
        logger=args.logger,
    )
