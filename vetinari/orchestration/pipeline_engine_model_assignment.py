"""Pipeline model-assignment and execution-stage helpers."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any, cast

from vetinari.boundary_guards import assert_dependency_success
from vetinari.orchestration.pipeline_events import PipelineStage

logger = logging.getLogger(__name__)


def _query_context_graph(stages: dict[str, Any], logger: logging.Logger) -> None:
    """Populate model-assignment awareness fields when available."""
    try:
        from vetinari.awareness.context_graph import get_context_graph
        from vetinari.types import ContextQuadrant

        ctx_graph = get_context_graph()
        ctx_snapshot = ctx_graph.get_context([
            ContextQuadrant.SELF,
            ContextQuadrant.USER,
            ContextQuadrant.ENVIRONMENT,
        ])
        stages["context_graph"] = {
            "vram_utilization": ctx_snapshot.get(ContextQuadrant.SELF, "vram_utilization"),
            "quality_trend": ctx_snapshot.get(ContextQuadrant.SELF, "quality_trend"),
            "project_tech_stack": ctx_snapshot.get(ContextQuadrant.ENVIRONMENT, "project_tech_stack"),
        }
        logger.info("[Pipeline] Context graph queried: %s", ctx_snapshot)
    except Exception:
        logger.warning("Context graph unavailable for model assignment; proceeding without awareness", exc_info=True)


def _select_cost_optimized_model(self: Any, node: Any, assigned: str, logger: logging.Logger) -> str:
    """Apply CostOptimizer to an already routed model assignment."""
    try:
        from vetinari.learning.cost_optimizer import get_cost_optimizer

        router = getattr(self, "model_router", None)
        if router is not None and hasattr(router, "models"):
            candidate_models = list(router.models.keys()) or [assigned]
        else:
            candidate_models = [assigned]
        optimized_model = get_cost_optimizer().select_cheapest_adequate(
            task_type=node.task_type or "general",
            candidate_models=candidate_models,
        )
        return optimized_model or assigned
    except Exception:
        logger.warning("CostOptimizer selection failed; using Thompson selection")
        return assigned


def _assign_models(
    self: Any,
    graph: Any,
    context: dict[str, Any],
    stages: dict[str, Any],
    logger: logging.Logger,
) -> dict[str, int]:
    """Route each graph node to a model and count selection confidence."""
    get_bound = object.__getattribute__
    intake_confidence = float(context.get("intake_confidence", 1.0))
    low_confidence_mode = intake_confidence < 0.5
    if low_confidence_mode:
        logger.info(
            "[Pipeline] Low intake confidence (%.2f); preferring high-capability model for all tasks",
            intake_confidence,
        )
    for node in graph.nodes.values():
        assigned = get_bound(self, "_route_model_for_task")(node)
        if low_confidence_mode:
            node.input_data["require_high_capability"] = True
        node.input_data["assigned_model"] = _select_cost_optimized_model(self, node, assigned, logger)
        logger.debug("  Task %s (%s) -> %s", node.id, node.task_type, node.input_data["assigned_model"])

    stages["model_assignment"] = {nid: n.input_data.get("assigned_model") for nid, n in graph.nodes.items()}
    conf_levels: dict[str, int] = {}
    for node in graph.nodes.values():
        level = node.input_data.get("_selection_confidence_level", "unknown")
        conf_levels[level] = conf_levels.get(level, 0) + 1
    stages["model_assignment_confidence"] = conf_levels
    return conf_levels


def _log_assignment_decisions(graph: Any, context: dict[str, Any], logger: logging.Logger) -> None:
    """Write model-selection decisions to the optional decision journal."""
    try:
        from vetinari.observability.decision_journal import get_decision_journal
        from vetinari.types import ConfidenceLevel, DecisionType

        journal = get_decision_journal()
        trace_id = context.get("_trace_id") or context.get("_exec_id")
        conf_map = {
            "high": ConfidenceLevel.HIGH,
            "medium": ConfidenceLevel.MEDIUM,
            "low": ConfidenceLevel.LOW,
            "very_low": ConfidenceLevel.VERY_LOW,
        }
        for node_id, node in graph.nodes.items():
            sel_confidence = node.input_data.get("_selection_confidence_level", "medium")
            journal.log_decision(
                decision_type=DecisionType.MODEL_SELECTION,
                chosen=node.input_data.get("assigned_model", "unknown"),
                confidence=conf_map.get(sel_confidence, ConfidenceLevel.MEDIUM),
                reasoning=node.input_data.get("_selection_confidence_explanation", ""),
                trace_id=str(trace_id) if trace_id else None,
                metadata={"task_id": node_id, "task_type": node.task_type or "general"},
            )
    except Exception:
        logger.warning("Decision journal unavailable for model assignment logging", exc_info=True)


def _complete_model_assignment_stage(
    self: Any,
    graph: Any,
    context: dict[str, Any],
    stages: dict[str, Any],
    conf_levels: dict[str, int],
    state_store: Any,
    pipeline_task_id: str,
    contextlib_module: Any,
    save_pipeline_checkpoint: Callable[..., None],
) -> None:
    """Emit completion telemetry and checkpoint the model-assignment stage."""
    self._emit(
        PipelineStage.MODEL_ASSIGN,
        "stage_completed",
        {"assignments": len(graph.nodes), "confidence_levels": conf_levels},
    )
    if state_store and pipeline_task_id:
        with contextlib_module.suppress(Exception):
            state_store.mark_stage_complete(pipeline_task_id, "model_assignment")
    save_pipeline_checkpoint(
        trace_id=str(context.get("_trace_id") or pipeline_task_id),
        execution_id=str(context.get("_exec_id") or pipeline_task_id),
        step_name="model_assignment",
        step_index=3,
        status="completed",
        output_snapshot={
            "assignments": {nid: n.input_data.get("assigned_model") for nid, n in graph.nodes.items()},
            "confidence_levels": conf_levels,
        },
    )


def _block_result(
    goal: str,
    graph: Any,
    stages: dict[str, Any],
    start_time: float,
    error: str,
) -> dict[str, Any]:
    """Build a standard pre-execution block result."""
    return {
        "plan_id": graph.plan_id,
        "goal": goal,
        "completed": 0,
        "failed": 0,
        "error": error,
        "stages": stages,
        "total_time_ms": int((time.time() - start_time) * 1000),
    }


def _check_low_confidence_block(
    goal: str,
    graph: Any,
    stages: dict[str, Any],
    start_time: float,
    logger: logging.Logger,
) -> dict[str, Any] | None:
    """Ask the autonomy governor whether low-confidence assignments may run."""
    low_conf_tasks: list[str] = []
    for node_id, node in graph.nodes.items():
        if node.input_data.get("_selection_confidence_level") in ("low", "very_low"):
            low_conf_tasks.append(node_id)
            logger.warning(
                "[Pipeline] Low model-selection confidence for task %s: %s",
                node_id,
                node.input_data.get("_selection_confidence_explanation", "no explanation"),
            )
    if not low_conf_tasks:
        return None
    try:
        from vetinari.autonomy.governor import get_governor
        from vetinari.types import PermissionDecision

        perm = get_governor().request_permission(
            "low_confidence_execution",
            details={
                "low_confidence_tasks": low_conf_tasks,
                "task_count": len(low_conf_tasks),
                "total_tasks": len(graph.nodes),
            },
        )
        if perm != PermissionDecision.APPROVE:
            logger.warning(
                "[Pipeline] Autonomy governor blocked execution (%s); %d/%d tasks have low confidence",
                perm.value,
                len(low_conf_tasks),
                len(graph.nodes),
            )
            return _block_result(
                goal,
                graph,
                stages,
                start_time,
                f"Execution blocked: {len(low_conf_tasks)} task(s) have low model-selection confidence",
            )
    except Exception:
        logger.warning("Autonomy governor unavailable for confidence check; proceeding", exc_info=True)
        assert_dependency_success(False, dependency_id="authorization_evidence")
    return None


def _select_budget_model_id(assigned_models: list[str]) -> str:
    """Choose the most constrained assigned model for shared budget checks."""
    cleaned = [str(model) for model in assigned_models if model]
    if not cleaned:
        return "default"
    try:
        from vetinari.context import window_manager

        return min(cleaned, key=lambda model_id: window_manager.get_window_manager(model_id).window_size)
    except Exception:
        logger.warning("Could not resolve model window sizes; using lexical fallback assignment", exc_info=True)
        return min(cleaned)


def _init_context_budget(context: dict[str, Any], stages: dict[str, Any], logger: logging.Logger) -> None:
    """Initialize execution-stage context-budget tracking when available."""
    try:
        from vetinari.context.pipeline_integration import create_pipeline_context_manager

        assigned_models = list(stages.get("model_assignment", {}).values())
        budget_model_id = _select_budget_model_id(assigned_models)
        context["_budget_model_id"] = budget_model_id
        context["_budget_model_assignments"] = dict(stages.get("model_assignment", {}))
        context["_context_manager"] = create_pipeline_context_manager(budget_model_id)
        logger.info("[Pipeline] Context budget tracker initialized for model %s", budget_model_id)
    except Exception:
        logger.warning("Context budget tracker setup failed; proceeding without budget management", exc_info=True)


def assign_models_and_run_execution(
    self: Any,
    *,
    goal: str,
    graph: Any,
    context: dict[str, Any],
    stages: dict[str, Any],
    start_time: float,
    _corr_ctx: Any,
    _pipeline_span: Any,
    task_handler: Callable[..., Any] | None,
    project_id: str | None,
    _intake_tier: Any,
    _intake_features: Any,
    _pipeline_task_id: str,
    _state_store: Any,
    contextlib_module: Any,
    save_pipeline_checkpoint: Callable[..., None],
    logger: logging.Logger,
) -> dict[str, Any]:
    """Assign models, enforce confidence gates, and enter execution stages.

    Returns:
        Value produced for the caller.
    """
    self._emit(PipelineStage.MODEL_ASSIGN, "stage_started")
    logger.info("[Pipeline] Stage 4: Model Assignment")

    _query_context_graph(stages, logger)
    conf_levels = _assign_models(self, graph, context, stages, logger)
    _log_assignment_decisions(graph, context, logger)
    _complete_model_assignment_stage(
        self,
        graph,
        context,
        stages,
        conf_levels,
        _state_store,
        _pipeline_task_id,
        contextlib_module,
        save_pipeline_checkpoint,
    )

    blocked_result = _check_low_confidence_block(goal, graph, stages, start_time, logger)
    if blocked_result is not None:
        return blocked_result

    if object.__getattribute__(self, "is_paused")():
        logger.warning("[Pipeline] Andon halt detected before execution; aborting")
        return _block_result(goal, graph, stages, start_time, "Pipeline halted by Andon signal before execution stage")

    _init_context_budget(context, stages, logger)

    return cast(
        dict[str, Any],
        object.__getattribute__(self, "_run_execution_stages")(
            goal,
            graph,
            context,
            stages,
            start_time,
            _corr_ctx,
            _pipeline_span,
            task_handler,
            project_id,
            _intake_tier,
            _intake_features,
        ),
    )
