"""Pipeline runtime stage helpers for execution, review, and finalization."""

from __future__ import annotations

import contextlib
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

import vetinari.context.window_manager as window_manager
import vetinari.learning.self_refinement as self_refinement
import vetinari.observability.checkpoints as pipeline_checkpoints
import vetinari.observability.step_evaluator as step_evaluator
import vetinari.validation as validation
from vetinari.awareness.confidence import ConfidenceResult
from vetinari.boundary_guards import account_evidence_drop
from vetinari.events import QualityGateResult
from vetinari.orchestration.pipeline_events import PipelineStage
from vetinari.orchestration.pipeline_stages_runtime_contracts import PipelineStageRuntimeOwner
from vetinari.orchestration.pipeline_stages_runtime_finalization import finalize_pipeline_runtime_result
from vetinari.types import ConfidenceAction, ConfidenceLevel, StatusEnum

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _StageRuntimeInputs:
    runner: Any
    goal: str
    graph: Any
    context: dict[str, Any]
    stages: dict[str, Any]
    start_time: float
    corr_ctx: Any
    pipeline_span: Any
    task_handler: Callable[..., Any] | None
    project_id: str | None
    intake_tier: Any
    intake_features: Any
    clock: Callable[[], float] = time.time

    def __repr__(self) -> str:
        return (
            f"_StageRuntimeInputs(goal={self.goal!r}, project_id={self.project_id!r}, stage_count={len(self.stages)!r})"
        )


@dataclass(frozen=True, slots=True)
class _ExecutionStageData:
    exec_results: dict[str, Any]
    event_bus: Any
    execution_id: Any


def _save_stage_checkpoint(
    context: dict[str, Any],
    execution_id: str,
    step_name: str,
    step_index: int,
    status: str,
    output_snapshot: dict[str, Any],
    quality_score: float | None = None,
) -> None:
    with contextlib.suppress(Exception):
        trace_id = str(context.get("_trace_id") or execution_id)
        pipeline_checkpoints.get_checkpoint_store().save_checkpoint(
            pipeline_checkpoints.PipelineCheckpoint(
                trace_id=trace_id,
                execution_id=str(context.get("_exec_id") or execution_id),
                step_name=step_name,
                step_index=step_index,
                status=status,
                output_snapshot=output_snapshot,
                quality_score=quality_score,
            )
        )


def _run_execution_stage(args: _StageRuntimeInputs, owner: PipelineStageRuntimeOwner) -> _ExecutionStageData:
    graph = args.graph
    owner._emit(PipelineStage.EXECUTION, "stage_started", {"total_tasks": len(graph.nodes)})
    logger.info("[Pipeline] Stage 5: Parallel Execution")
    exec_results = args.runner._execute_via_agent_graph_or_fallback(graph, args.task_handler, args.context)
    args.stages["execution"] = exec_results
    completed = exec_results.get(StatusEnum.COMPLETED.value, 0)
    failed = exec_results.get(StatusEnum.FAILED.value, 0)
    owner._emit(PipelineStage.EXECUTION, "stage_completed", {"completed": completed, "failed": failed})
    pipeline_task_id = str(args.context.get("_exec_id") or args.context.get("_trace_id") or "")
    if pipeline_task_id:
        try:
            from vetinari.orchestration.pipeline_state import get_pipeline_state_store

            get_pipeline_state_store().mark_stage_complete(
                pipeline_task_id, "execution", {"completed": completed, "failed": failed}
            )
        except Exception:
            logger.warning("Pipeline state checkpoint skipped for execution stage", exc_info=True)
    _save_stage_checkpoint(
        args.context,
        pipeline_task_id,
        "execution",
        4,
        "completed" if completed > 0 else "failed",
        {"completed": completed, "failed": failed},
    )
    execution_id = args.context.get("_exec_id", graph.plan_id)
    event_bus = owner._get_pipeline_event_bus()
    args.runner._publish_task_execution_events(
        bus=event_bus,
        graph=graph,
        start_time=args.start_time,
        execution_id=execution_id,
    )
    args.runner._record_post_execution_system_updates(graph=graph)
    return _ExecutionStageData(exec_results=exec_results, event_bus=event_bus, execution_id=execution_id)


def _apply_stage_constraints(
    args: _StageRuntimeInputs,
    owner: PipelineStageRuntimeOwner,
    exec_results: dict[str, Any],
) -> None:
    exec_valid, exec_issues = owner._validate_stage_boundary(
        "execution",
        exec_results,
        min_keys=[StatusEnum.COMPLETED.value],
    )
    if not exec_valid:
        logger.warning("[Pipeline] Execution validation failed: %s", exec_issues)
    exec_quality = None
    if isinstance(exec_results, dict):
        for task_result in exec_results.get("task_results", {}).values():
            if isinstance(task_result, dict) and "quality_score" in task_result:
                exec_quality = task_result["quality_score"]
                break
    constraints_ok, constraint_violations = owner._check_stage_constraints(
        args.context.get("agent_type", "WORKER"),
        args.context.get("mode"),
        exec_quality,
    )
    if not constraints_ok:
        args.stages["constraint_violations"] = constraint_violations


def _record_context_window_after_execution(args: _StageRuntimeInputs) -> None:
    try:
        model_id = args.context.get("model_id", "default")
        manager = window_manager.get_window_manager(str(model_id))
        saved = manager.stage_boundary_compress("execution->review")
        window_state = manager.get_state()
        if manager.usage_ratio > 0.85:
            evicted = manager.page_out(count=10)
            logger.info(
                "[Pipeline] Context window %.0f%% full after execution - paged out %d messages",
                manager.usage_ratio * 100,
                len(evicted),
            )
        args.stages["context_window_after_execution"] = {
            "used_tokens": window_state.used_tokens,
            "max_tokens": window_state.max_tokens,
            "tokens_saved_by_compression": saved,
        }
    except (ImportError, AttributeError):
        logger.debug("Context window management skipped at execution->review boundary - manager unavailable")


def _evaluate_step_adherence(args: _StageRuntimeInputs) -> None:
    try:
        evaluator = step_evaluator.get_step_evaluator()
        plan_dict = {"plan_id": args.graph.plan_id, "tasks": [node.to_dict() for node in args.graph.nodes.values()]}
        exec_results_for_eval = {
            task_id: {"status": "completed" if node.status == StatusEnum.COMPLETED else "failed"}
            for task_id, node in args.graph.nodes.items()
        }
        adherence = evaluator.evaluate_all(plan_dict, exec_results_for_eval)
        args.stages["step_evaluation"] = {"overall_score": adherence.overall_score, "passed": adherence.passed}
        logger.info("[Pipeline] StepEvaluator: overall=%.2f, passed=%s", adherence.overall_score, adherence.passed)
    except Exception:
        logger.warning("StepEvaluator unavailable, skipping plan adherence check", exc_info=True)


def _check_context_budget(args: _StageRuntimeInputs, stage_name: str) -> None:
    context_manager = args.context.get("_context_manager")
    if context_manager is None:
        return
    try:
        manager = window_manager.get_window_manager(args.context.get("_budget_model_id", "default"))
        messages = [
            window_manager.WindowConversationMessage(role=message["role"], content=message["content"])
            for message in manager.get_messages()
        ]
        if messages:
            _messages, budget_check = context_manager.check_budget(stage_name, messages)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[Pipeline] Post-%s context budget: %s", stage_name, budget_check.status.value)
    except Exception:
        logger.warning("Context budget check failed after %s stage - continuing", stage_name, exc_info=True)


def _coerce_confidence_result(task_result: dict[str, Any]) -> ConfidenceResult | None:
    existing = task_result.get("confidence_result")
    if isinstance(existing, ConfidenceResult):
        return existing
    level = task_result.get("confidence_level")
    action = task_result.get("confidence_action")
    score = task_result.get("confidence_score")
    if level is None or action is None or score is None:
        return None
    try:
        return ConfidenceResult(
            score=float(score),
            level=level if isinstance(level, ConfidenceLevel) else ConfidenceLevel(str(level)),
            action=action if isinstance(action, ConfidenceAction) else ConfidenceAction(str(action)),
            explanation=str(task_result.get("confidence_explanation") or "Runtime task confidence metadata"),
            factors=dict(task_result.get("confidence_factors") or {}),
            source=str(task_result.get("confidence_source") or "runtime_task_metadata"),
            metadata=dict(task_result.get("confidence_metadata") or {}),
        )
    except (TypeError, ValueError):
        logger.warning("Invalid task confidence metadata for runtime routing: %r", task_result)
        return None


def _task_output_key(task_result: dict[str, Any]) -> str | None:
    for key in ("output", "result"):
        if key in task_result and task_result[key] not in (None, ""):
            return key
    return None


def _apply_confidence_routing(exec_results: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    try:
        from vetinari.orchestration.pipeline_confidence import apply_confidence_routing

        task_results = exec_results.get("task_results")
        if not isinstance(task_results, dict):
            return exec_results
        routed_count = 0
        for task_id, task_result in task_results.items():
            if not isinstance(task_result, dict):
                continue
            output_key = _task_output_key(task_result)
            confidence = _coerce_confidence_result(task_result)
            if output_key is None or confidence is None:
                task_result.setdefault(
                    "confidence_routing", {"applied": False, "reason": "missing_output_or_confidence"}
                )
                continue
            routed_output, routed_confidence = apply_confidence_routing(
                str(task_result[output_key]),
                confidence,
                refine_fn=context.get("confidence_refine_fn"),
                confidence_fn=context.get("confidence_measure_fn"),
                sample_fn=context.get("confidence_sample_fn"),
                defer_fn=context.get("confidence_defer_fn"),
            )
            task_result[output_key] = routed_output
            task_result["confidence_result"] = routed_confidence
            task_result["confidence_level"] = routed_confidence.level.value
            task_result["confidence_action"] = routed_confidence.action.value
            task_result["confidence_score"] = routed_confidence.score
            task_result["confidence_routing"] = {"applied": True, "task_id": task_id}
            routed_count += 1
        exec_results["confidence_routing"] = {"applied": routed_count > 0, "routed_tasks": routed_count}
        return exec_results
    except Exception:
        logger.warning("Confidence routing unavailable - proceeding without confidence gating", exc_info=True)
        return exec_results


def _run_self_refinement(
    args: _StageRuntimeInputs, owner: PipelineStageRuntimeOwner, exec_results: dict[str, Any]
) -> None:
    owner._emit(PipelineStage.REFINEMENT, "stage_started", {"tier": args.context.get("intake_tier", "standard")})
    if args.context.get("intake_tier") != "custom":
        return
    try:
        refiner = self_refinement.get_self_refiner()
        for task_id, task_result in exec_results.get("task_results", {}).items():
            if not isinstance(task_result, dict) or task_result.get("status") != StatusEnum.COMPLETED.value:
                continue
            output = task_result.get("output", "")
            if not output:
                continue
            refined = refiner.refine(
                task_description=args.goal,
                initial_output=str(output),
                task_type=str(args.context.get("mode", "general") or "general"),
                model_id=str(args.context.get("model_id", "default") or "default"),
                importance=0.8,
            )
            if refined.improved:
                task_result["output"] = refined.output
                logger.info("[Pipeline] Self-refinement improved task %s (rounds=%d)", task_id, refined.rounds_used)
        args.stages["self_refinement"] = {"applied": True, "tier": "custom"}
    except Exception:
        logger.warning("Self-refinement unavailable, skipping", exc_info=True)
        args.stages["self_refinement"] = {"applied": False, "reason": "unavailable"}


def _halt_after_execution_result(args: _StageRuntimeInputs, exec_results: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_id": args.graph.plan_id,
        "goal": args.goal,
        "completed": exec_results.get(StatusEnum.COMPLETED.value, 0),
        "failed": exec_results.get(StatusEnum.FAILED.value, 0),
        "error": "Pipeline halted by Andon signal after execution stage",
        "stages": args.stages,
        "total_time_ms": int((args.clock() - args.start_time) * 1000),
    }


def _validate_build_outputs(
    args: _StageRuntimeInputs, owner: PipelineStageRuntimeOwner, exec_results: dict[str, Any]
) -> None:
    if not isinstance(exec_results, dict) or args.context.get("mode") != "build":
        return
    for task_id, task_result in exec_results.get("task_results", {}).items():
        if not isinstance(task_result, dict) or not task_result.get("code"):
            continue
        sandbox_ok, sandbox_detail = owner._sandbox_validate_code_output(task_result["code"])
        task_result["sandbox_passed"] = sandbox_ok
        task_result["sandbox_detail"] = sandbox_detail
        if not sandbox_ok:
            logger.warning("[Pipeline] Sandbox validation failed for task %s: %s", task_id, sandbox_detail)


def _run_review_stage(
    args: _StageRuntimeInputs, owner: PipelineStageRuntimeOwner, exec_results: dict[str, Any]
) -> dict[str, Any]:
    owner._emit(PipelineStage.REVIEW, "stage_started")
    logger.info("[Pipeline] Stage 6: Output Review")
    review_result = owner._review_outputs(exec_results, args.goal, args.context)
    if "passed" not in review_result:
        review_result = {**review_result, "passed": True}
    args.stages["review"] = review_result
    owner._emit(
        PipelineStage.REVIEW,
        "stage_completed",
        {"passed": review_result.get("passed", True), "quality_score": review_result.get("quality_score")},
    )
    checkpoint_id = str(args.context.get("_trace_id") or args.context.get("_exec_id") or "")
    review_quality = float(review_result.get("quality_score") or 0.0) or None
    _save_stage_checkpoint(
        args.context,
        checkpoint_id,
        "review",
        5,
        "completed" if review_result.get("passed", True) else "failed",
        {
            "passed": review_result.get("passed", True),
            "quality_score": review_result.get("quality_score"),
            "issues_count": len(review_result.get("issues", [])),
        },
        quality_score=review_quality,
    )
    return cast(dict[str, Any], review_result)


def _maybe_run_posthoc_audit(
    args: _StageRuntimeInputs,
    owner: PipelineStageRuntimeOwner,
    exec_results: dict[str, Any],
    review_result: dict[str, Any],
) -> None:
    if not review_result.get("passed") or secrets.randbelow(10) != 0:
        return
    try:
        logger.info("[PostHocAudit] Running random post-hoc audit on completed task")
        posthoc_review = owner._review_outputs(exec_results, args.goal, args.context)
        posthoc_quality = posthoc_review.get("quality_score", 0.5)
        original_quality = review_result.get("quality_score", 0.5)
        if abs(posthoc_quality - original_quality) > 0.2:
            logger.warning(
                "[PostHocAudit] Quality drift detected: original=%.2f, posthoc=%.2f (delta=%.2f)",
                original_quality,
                posthoc_quality,
                posthoc_quality - original_quality,
            )
    except Exception:
        logger.warning("Post-hoc audit failed - non-fatal, primary review result stands")


def _publish_quality_gate(
    event_bus: Any,
    graph: Any,
    review_result: dict[str, Any],
    *,
    timestamp: float,
) -> None:
    review_score = review_result.get("quality_score", 0.0)
    review_issues = review_result.get("issues", [])
    event_bus.publish(
        QualityGateResult(
            event_type="QualityGateResult",
            timestamp=timestamp,
            task_id=graph.plan_id,
            passed=bool(review_result.get("passed")),
            score=float(review_score) if isinstance(review_score, (int, float)) else 0.0,
            issues=[str(issue) for issue in review_issues[:20]],
        )
    )


def _run_inspector_extract(args: _StageRuntimeInputs, review_result: dict[str, Any]) -> None:
    if not review_result or not args.context.get("diff"):
        return
    try:
        from vetinari.agents.inspector_extract import extract_implicit_decisions, log_extracted_decisions

        candidates = extract_implicit_decisions(args.context["diff"])
        if candidates:
            logged_ids = log_extracted_decisions(candidates)
            args.stages["inspector_extract"] = {
                "candidates_found": len(candidates),
                "decisions_logged": len(logged_ids),
            }
    except Exception:
        logger.warning("Inspector Extract skipped - extraction unavailable, proceeding without implicit decisions")


def _run_assembly_stage(
    args: _StageRuntimeInputs,
    owner: PipelineStageRuntimeOwner,
    exec_results: dict[str, Any],
    review_result: dict[str, Any],
) -> Any:
    owner._emit(PipelineStage.ASSEMBLY, "stage_started")
    logger.info("[Pipeline] Stage 7: Final Assembly")
    final_output = owner._assemble_final_output(exec_results, review_result, args.goal)
    gate_issues = args.stages.get("gate_issues", [])
    if args.stages.get("gate_blocked", False) and gate_issues:
        final_output = f"[INSPECTOR GATE FAILED - {len(gate_issues)} issue(s) found]\n\n{final_output}"
        logger.warning("[Pipeline] Final output annotated with Inspector gate failure")
    args.stages["final_assembly"] = {"output_length": len(str(final_output))}
    owner._emit(PipelineStage.ASSEMBLY, "stage_completed", {"output_length": len(str(final_output))})
    checkpoint_id = str(args.context.get("_trace_id") or args.context.get("_exec_id") or "")
    _save_stage_checkpoint(
        args.context,
        checkpoint_id,
        "assembly",
        6,
        "completed",
        {"output_length": len(str(final_output))},
    )
    return final_output


def _run_goal_verification_stage(
    args: _StageRuntimeInputs, owner: PipelineStageRuntimeOwner, exec_results: dict[str, Any], final_output: Any
) -> Any:
    if not owner.enable_correction_loop:
        return None
    owner._emit(PipelineStage.VERIFICATION, "stage_started")
    logger.info("[Pipeline] Stage 8: Goal Verification + Correction Loop")
    goal_verification_report = None
    try:
        verifier = validation.get_goal_verifier()
        task_outputs = [{"output": str(value)} for value in exec_results.get("task_results", {}).values() if value]
        initial_report = verifier.verify(
            project_id=args.project_id or args.graph.plan_id,
            goal=args.goal,
            final_output=str(final_output),
            required_features=args.context.get("required_features"),
            things_to_avoid=args.context.get("things_to_avoid"),
            task_outputs=task_outputs,
        )
        corrective_tasks = initial_report.get_corrective_tasks()
        if corrective_tasks and not initial_report.fully_compliant:
            logger.info(
                "[Pipeline] Goal verification incomplete (score=%.2f), running %d corrective task(s)",
                initial_report.compliance_score,
                len(corrective_tasks),
            )
            goal_verification_report = owner._execute_corrections(
                corrective_tasks=corrective_tasks,
                plan=_verification_plan_dict(args, final_output, task_outputs),
                goal=args.goal,
                context=args.context,
            )
        else:
            goal_verification_report = initial_report
        args.stages["goal_verification"] = _goal_verification_stage_dict(goal_verification_report)
    except Exception as exc:
        item = {"goal": args.goal, "plan_id": args.graph.plan_id, "project_id": args.project_id}
        account_evidence_drop(item, "goal_verification", logger=logger)
        logger.warning("[Pipeline] Goal verification stage failed: %s", exc)
        raise
    owner._emit(PipelineStage.VERIFICATION, "stage_completed", _goal_verification_event(goal_verification_report))
    return goal_verification_report


def _verification_plan_dict(
    args: _StageRuntimeInputs, final_output: Any, task_outputs: list[dict[str, str]]
) -> dict[str, Any]:
    return {
        "project_id": args.project_id or args.graph.plan_id,
        "required_features": args.context.get("required_features", []),
        "things_to_avoid": args.context.get("things_to_avoid", []),
        "final_output": str(final_output),
        "task_outputs": task_outputs,
    }


def _goal_verification_stage_dict(goal_verification_report: Any) -> dict[str, Any]:
    return {
        "compliance_score": goal_verification_report.compliance_score,
        "fully_compliant": goal_verification_report.fully_compliant,
        "missing_features": goal_verification_report.missing_features,
    }


def _goal_verification_event(goal_verification_report: Any) -> dict[str, Any]:
    return {
        "compliance_score": goal_verification_report.compliance_score if goal_verification_report else None,
        "fully_compliant": goal_verification_report.fully_compliant if goal_verification_report else None,
    }


def _finalize_runtime_result(
    args: _StageRuntimeInputs,
    exec_results: dict[str, Any],
    review_result: dict[str, Any],
    final_output: Any,
    goal_verification_report: Any,
) -> dict[str, Any]:
    total_time = int((args.clock() - args.start_time) * 1000)
    result_dict: dict[str, Any] = {
        "plan_id": args.graph.plan_id,
        "goal": args.goal,
        "completed": exec_results.get(StatusEnum.COMPLETED.value, 0),
        "failed": exec_results.get(StatusEnum.FAILED.value, 0),
        "outputs": exec_results.get("task_results", {}),
        "review_result": review_result,
        "final_output": final_output,
        "stages": args.stages,
        "total_time_ms": total_time,
        "inspector_gate_passed": not args.stages.get("gate_blocked"),
    }
    if goal_verification_report is not None:
        result_dict["goal_verification"] = goal_verification_report.to_dict()
    return finalize_pipeline_runtime_result(
        result_dict=result_dict,
        context=args.context,
        graph=args.graph,
        exec_results=exec_results,
        final_output=final_output,
        goal=args.goal,
        total_time=total_time,
        _pipeline_span=args.pipeline_span,
        _corr_ctx=args.corr_ctx,
        _intake_tier=args.intake_tier,
        _intake_features=args.intake_features,
        goal_verification_report=goal_verification_report,
    )


def _run_runtime_stages(args: _StageRuntimeInputs) -> dict[str, Any]:
    owner = cast(PipelineStageRuntimeOwner, args.runner)
    execution = _run_execution_stage(args, owner)
    exec_results = execution.exec_results
    _apply_stage_constraints(args, owner, exec_results)
    _record_context_window_after_execution(args)
    _evaluate_step_adherence(args)
    _check_context_budget(args, "execution")
    exec_results = _apply_confidence_routing(exec_results, args.context)
    _run_self_refinement(args, owner, exec_results)
    if owner.is_paused():
        logger.warning("[Pipeline] Andon halt detected after execution - aborting before review")
        return _halt_after_execution_result(args, exec_results)
    owner._emit(PipelineStage.REFINEMENT, "stage_completed")
    _validate_build_outputs(args, owner, exec_results)
    review_result = _run_review_stage(args, owner, exec_results)
    _maybe_run_posthoc_audit(args, owner, exec_results, review_result)
    _publish_quality_gate(execution.event_bus, args.graph, review_result, timestamp=args.clock())
    args.runner._run_review_gate(graph=args.graph, stages=args.stages, review_result=review_result)
    _check_context_budget(args, "review")
    _run_inspector_extract(args, review_result)
    final_output = _run_assembly_stage(args, owner, exec_results, review_result)
    goal_verification_report = _run_goal_verification_stage(args, owner, exec_results, final_output)
    return _finalize_runtime_result(args, exec_results, review_result, final_output, goal_verification_report)
