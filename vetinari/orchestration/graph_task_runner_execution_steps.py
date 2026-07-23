"""Execution setup helpers for one graph task node before the retry loop."""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vetinari.exceptions import SecurityError
from vetinari.orchestration.graph_task_runner_helpers import (
    _get_agent_circuit_breaker_cls,
    _get_agent_control_state_fn,
    _get_agent_monitor_fn,
    _get_andon_system_fn,
    _get_blackboard_fn,
    _get_bottleneck_identifier_fn,
    _get_circuit_breaker_cls,
    _get_constraint_registry_fn,
    _get_context_condenser_fn,
    _get_cost_predictor_cls,
    _get_execution_context_api,
    _get_log_event_fn,
    _get_policy_enforcer_fn,
    _get_task_manifest_context_cls,
    _module_is_available,
)
from vetinari.orchestration.graph_types import TaskNode
from vetinari.safety.protected_mutation import UnconfirmedDestructiveAction, enforce_task_intent
from vetinari.types import AgentType

if TYPE_CHECKING:
    from vetinari.agents.contracts import AgentResult, AgentTask

logger = logging.getLogger(__name__)


class TaskConstraintViolation(RuntimeError):
    """Raised when a task violates mode or task-type constraints."""


@dataclass(frozen=True, slots=True)
class _TaskNodeRunContext:
    runner: Any
    node: TaskNode
    prior_results: dict[str, AgentResult] | None
    task: Any
    agent_type: AgentType
    start_ms: float

    def __repr__(self) -> str:
        task_id = getattr(self.task, "id", None)
        return f"_TaskNodeRunContext(node={self.node!r}, task_id={task_id!r}, agent_type={self.agent_type!r})"


def _agent_type_value(agent_type: AgentType | str) -> str:
    return agent_type.value if hasattr(agent_type, "value") else str(agent_type)


def _release_wip_claim(ctx: _TaskNodeRunContext) -> None:
    if ctx.runner._wip_tracker is None:
        return
    agent_type_str = _agent_type_value(ctx.agent_type).upper()
    ctx.runner._wip_tracker.release_task(agent_type_str, str(ctx.task.id))
    with contextlib.suppress(Exception):
        ctx.runner._wip_tracker.complete_pool_task(agent_type_str, str(ctx.task.id))


def _start_wip_claim(ctx: _TaskNodeRunContext) -> AgentResult | None:
    if ctx.runner._wip_tracker is None:
        return None
    agent_type_str = _agent_type_value(ctx.agent_type)
    if ctx.runner._wip_tracker.start_task(agent_type_str.upper(), str(ctx.task.id)):
        with contextlib.suppress(Exception):
            ctx.runner._wip_tracker.start_pool_task(agent_type_str.upper(), str(ctx.task.id))
        return None
    logger.info("[AgentGraph] WIP limit reached for %s - task %s queued", agent_type_str, ctx.task.id)
    ctx.runner._wip_tracker.enqueue(agent_type_str.upper(), str(ctx.task.id))
    return AgentResult(
        success=False,
        output=None,
        errors=[f"WIP limit reached for {agent_type_str} - task queued"],
    )


def _check_operator_pause(ctx: _TaskNodeRunContext) -> AgentResult | None:
    try:
        if not _module_is_available("vetinari.orchestration.agent_control"):
            raise ModuleNotFoundError("vetinari.orchestration.agent_control")
        control = _get_agent_control_state_fn()()
        agent_type_str = _agent_type_value(ctx.agent_type)
        if agent_type_str not in control["paused"] and str(ctx.task.id) not in control["paused"]:
            return None
        pause_reason = (
            control["paused"].get(agent_type_str, {}).get("reason")
            or control["paused"].get(str(ctx.task.id), {}).get("reason")
            or "paused by operator"
        )
        logger.info("[AgentGraph] Agent %s paused - task %s deferred: %s", agent_type_str, ctx.task.id, pause_reason)
        _release_wip_claim(ctx)
        return AgentResult(
            success=False,
            output=None,
            errors=[f"Agent {agent_type_str} is paused: {pause_reason}"],
        )
    except ModuleNotFoundError:
        logger.debug("Agent control API not available - skipping pause check")
        return None


def _check_destructive_intent(ctx: _TaskNodeRunContext) -> AgentResult | None:
    try:
        enforce_task_intent(ctx.task, intent=None)
        return None
    except UnconfirmedDestructiveAction as exc:
        logger.warning(
            "[AgentGraph] Destructive task %s blocked: missing ConfirmedIntent (%s)",
            ctx.task.id,
            exc,
        )
        _release_wip_claim(ctx)
        return AgentResult(success=False, output=None, errors=[f"Task requires ConfirmedIntent: {exc}"])


def _emit_task_started(ctx: _TaskNodeRunContext) -> None:
    try:
        _get_log_event_fn()(
            "info",
            "vetinari.orchestration.agent_graph",
            "task_started",
            task_id=str(ctx.task.id),
            agent_type=str(ctx.agent_type),
            status="running",
        )
    except Exception:
        logger.warning("Failed to emit task_started structured event for %s", ctx.task.id, exc_info=True)


@dataclass(frozen=True, slots=True)
class _TaskDoneEmitter:
    ctx: _TaskNodeRunContext

    def __call__(self, final_result: AgentResult) -> AgentResult:
        if self.ctx.runner._wip_tracker is not None:
            agent_type_str = _agent_type_value(self.ctx.agent_type).upper()
            self.ctx.runner._wip_tracker.complete_task(agent_type_str, str(self.ctx.task.id))
            with contextlib.suppress(Exception):
                self.ctx.runner._wip_tracker.complete_pool_task(agent_type_str, str(self.ctx.task.id))
        if final_result.success:
            _resume_andon_scope(self.ctx)
        success = final_result.success
        duration_ms = round(time.time() * 1000 - self.ctx.start_ms, 2)
        _record_bottleneck_metrics(self.ctx, duration_ms, success)
        event_name = "task_completed" if success else "task_failed"
        try:
            _get_log_event_fn()(
                "info" if success else "warning",
                "vetinari.orchestration.agent_graph",
                event_name,
                task_id=str(self.ctx.task.id),
                agent_type=_agent_type_value(self.ctx.agent_type),
                duration_ms=duration_ms,
                status="completed" if success else "failed",
            )
        except Exception:
            logger.warning("Failed to emit %s structured event for %s", event_name, self.ctx.task.id, exc_info=True)
        return final_result


def _resume_andon_scope(ctx: _TaskNodeRunContext) -> None:
    scope_str = _agent_type_value(ctx.agent_type)
    try:
        andon = _get_andon_system_fn()()
        if andon.is_scope_paused(scope_str):
            andon.resume_scope(scope_str)
            logger.info("[AgentGraph] Andon scope %r resumed after successful task %s", scope_str, ctx.task.id)
    except Exception as exc:
        logger.warning(
            "Andon scope resume skipped for scope %r after task %s - andon unavailable: %s",
            scope_str,
            ctx.task.id,
            exc,
        )


def _record_bottleneck_metrics(ctx: _TaskNodeRunContext, duration_ms: float, success: bool) -> None:
    try:
        bottleneck = _get_bottleneck_identifier_fn()()
        bottleneck.update_metrics(_agent_type_value(ctx.agent_type), duration_ms, success)
    except Exception:
        logger.warning("Bottleneck metric update failed for %s - skipping", ctx.agent_type)


def _apply_resource_constraints(ctx: _TaskNodeRunContext) -> None:
    try:
        registry = _get_constraint_registry_fn()()
        constraints = registry.get_constraints_for_agent(_agent_type_value(ctx.agent_type))
        if constraints and constraints.resources:
            constrained_retries = min(ctx.node.max_retries, constraints.resources.max_retries)
            if constrained_retries < ctx.node.max_retries:
                logger.debug(
                    "[AgentGraph] Capping retries for %s from %d to %d (constraint)",
                    ctx.agent_type,
                    ctx.node.max_retries,
                    constrained_retries,
                )
                ctx.node.max_retries = constrained_retries
    except Exception:
        logger.warning("Failed to apply agent constraints for %s", ctx.agent_type, exc_info=True)


def _validate_mode_and_task_type(ctx: _TaskNodeRunContext) -> None:
    registry = _get_constraint_registry_fn()()
    agent_str = _agent_type_value(ctx.agent_type)
    task_meta = ctx.task.metadata if hasattr(ctx.task, "metadata") and ctx.task.metadata else {}
    mode = task_meta.get("mode") if isinstance(task_meta, dict) else None
    task_type_meta = task_meta.get("task_type") if isinstance(task_meta, dict) else None
    _log_mode_constraint_violation(registry, agent_str, mode, ctx)
    _log_task_type_constraint_violation(registry, agent_str, task_type_meta, ctx)


def _log_mode_constraint_violation(registry: Any, agent_str: str, mode: Any, ctx: _TaskNodeRunContext) -> None:
    if not mode:
        return
    mode_valid, mode_reason = registry.validate_mode(agent_str, mode)
    if not mode_valid:
        raise TaskConstraintViolation(
            f"Mode constraint violation for {ctx.agent_type} on task {ctx.task.id}: mode={mode!r} - {mode_reason}"
        )


def _log_task_type_constraint_violation(
    registry: Any,
    agent_str: str,
    task_type_meta: Any,
    ctx: _TaskNodeRunContext,
) -> None:
    """Log a task-type constraint violation when the registry rejects it."""
    if not task_type_meta:
        return
    type_valid, type_reason = registry.validate_task_type(agent_str, task_type_meta)
    if not type_valid:
        raise TaskConstraintViolation(
            f"Task-type constraint violation for {ctx.agent_type} on task "
            f"{ctx.task.id}: task_type={task_type_meta!r} - {type_reason}"
        )


def _resolve_agent_or_delegate(
    ctx: _TaskNodeRunContext,
    emit_task_done: Callable[[AgentResult], AgentResult],
) -> tuple[Any | None, AgentResult | None]:
    """Resolve the assigned agent or delegate through the blackboard path."""
    if ctx.agent_type in ctx.runner._agents:
        return ctx.runner._agents[ctx.agent_type], None
    delegated = _get_blackboard_fn()().delegate(ctx.task, ctx.runner._agents) or AgentResult(
        success=False,
        output=None,
        errors=[f"No agent registered for type: {ctx.agent_type}"],
    )
    return None, emit_task_done(delegated)


def _enforce_permissions(
    ctx: _TaskNodeRunContext, emit_task_done: Callable[[AgentResult], AgentResult]
) -> AgentResult | None:
    """Run agent and context permission checks before execution."""
    try:
        tool_permission, enforce_agent_permissions, _get_context_manager = _get_execution_context_api()
        enforce_agent_permissions(ctx.agent_type, tool_permission.MODEL_INFERENCE)
    except (PermissionError, SecurityError) as perm_err:
        logger.warning("[AgentGraph] Agent permission denied: %s", perm_err)
        return emit_task_done(AgentResult(success=False, output=None, errors=[str(perm_err)]))
    except Exception:
        logger.warning("Agent permission check not configured, allowing execution")
    try:
        tool_permission, _enforce_agent_permissions, get_context_manager = _get_execution_context_api()
        get_context_manager().enforce_permission(
            tool_permission.MODEL_INFERENCE,
            f"agent_execute:{ctx.agent_type.value}",
        )
    except PermissionError:
        logger.warning(
            "[AgentGraph] Permission denied for %s - MODEL_INFERENCE not allowed",
            ctx.agent_type.value,
        )
        return emit_task_done(
            AgentResult(
                success=False,
                output=None,
                errors=[f"Permission denied: MODEL_INFERENCE required for {ctx.agent_type.value}"],
            )
        )
    except Exception:
        logger.warning("Context manager not configured, allowing execution")
    return None


def _dependency_context(ctx: _TaskNodeRunContext) -> dict[str, Any]:
    """Build condensed dependency context for a task."""
    task_context: dict[str, Any] = dict(ctx.task.context if hasattr(ctx.task, "context") else {})
    if not ctx.prior_results or not ctx.task.dependencies:
        return task_context
    try:
        condenser = _get_context_condenser_fn()()
        dep_summaries = _condensed_dependency_summaries(ctx, condenser)
    except Exception:
        logger.warning("[AgentGraph] Context condenser unavailable, using raw summaries")
        dep_summaries = {
            dep_id: {
                "success": ctx.prior_results[dep_id].success,
                "output_summary": str(ctx.prior_results[dep_id].output)[:500],
            }
            for dep_id in ctx.task.dependencies
            if dep_id in ctx.prior_results
        }
    task_context["dependency_results"] = dep_summaries
    return task_context


def _condensed_dependency_summaries(ctx: _TaskNodeRunContext, condenser: Any) -> dict[str, dict[str, Any]]:
    """Return dependency summaries compressed for handoff between agents."""
    summaries: dict[str, dict[str, Any]] = {}
    for dep_id in ctx.task.dependencies:
        if not ctx.prior_results or dep_id not in ctx.prior_results:
            continue
        dep_result = ctx.prior_results[dep_id]
        source_agent = "UNKNOWN"
        if hasattr(ctx.runner, "_execution_plan") and ctx.runner._execution_plan:
            dep_node = ctx.runner._execution_plan.nodes.get(dep_id)
            if dep_node:
                source_agent = dep_node.agent_type.value
        summaries[dep_id] = {
            "success": dep_result.success,
            "output_summary": condenser.condense_for_handoff(
                source_agent,
                ctx.agent_type.value,
                dep_result.output,
                dep_result.metadata,
            ),
        }
    return summaries


def _manifest_prefix(ctx: _TaskNodeRunContext, task_context: dict[str, Any]) -> str:
    """Build the prompt prefix that lists dependency and constraint context."""
    try:
        dep_results_dict: dict[str, Any] = {}
        if ctx.prior_results:
            dep_results_dict = {
                task_id: str(result.output)[:500] if result.output else ""
                for task_id, result in ctx.prior_results.items()
            }
        manifest = _get_task_manifest_context_cls().build_for_task(
            task_id=str(ctx.task.id),
            task_description=ctx.task.description or "",
            goal=task_context.get("goal", ""),
            completed_results=dep_results_dict,
            dependency_ids=list(ctx.task.dependencies) if ctx.task.dependencies else [],
            constraints=task_context.get("constraints", {}),
        )
        return str(manifest.format_for_prompt())
    except Exception:
        logger.warning("TaskContextManifest unavailable for task %s", ctx.task.id)
        return ""


def _build_agent_task(ctx: _TaskNodeRunContext, agent: Any) -> AgentTask:
    """Build the AgentTask passed into the retry loop."""
    task_context = _dependency_context(ctx)
    manifest_prefix = _manifest_prefix(ctx, task_context)
    task_prompt = f"{manifest_prefix}\n{ctx.task.description}" if manifest_prefix else ctx.task.description
    agent_task = AgentTask.from_task(ctx.task, task_prompt)
    if hasattr(agent_task, "context"):
        agent_task.context.update(task_context)
    _incorporate_prior_results(ctx, agent, agent_task)
    return agent_task


def _incorporate_prior_results(ctx: _TaskNodeRunContext, agent: Any, agent_task: AgentTask) -> None:
    """Let agents add dependency results through their own hook."""
    if not hasattr(agent, "_incorporate_prior_results"):
        return
    try:
        dep_results = agent._incorporate_prior_results(agent_task)
        if dep_results:
            agent_task.context["incorporated_results"] = dep_results
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[AgentGraph] Agent %s incorporated %d dependency result(s): %s",
                    ctx.agent_type.value,
                    len(dep_results),
                    ", ".join(dep_results.keys()),
                )
    except Exception as exc:
        logger.warning("[AgentGraph] _incorporate_prior_results failed: %s", exc)


def _get_backend_circuit_breaker(
    ctx: _TaskNodeRunContext,
    emit_task_done: Callable[[AgentResult], AgentResult],
) -> tuple[Any, AgentResult | None]:
    """Resolve and pre-check the backend circuit breaker."""
    try:
        if not _module_is_available("vetinari.resilience"):
            raise ModuleNotFoundError("vetinari.resilience")
        breaker_key = f"agent_{ctx.agent_type.value}"
        if not hasattr(ctx.runner, "_circuit_breakers"):
            ctx.runner._circuit_breakers = {}
        if breaker_key not in ctx.runner._circuit_breakers:
            ctx.runner._circuit_breakers[breaker_key] = _get_circuit_breaker_cls()(breaker_key)
        breaker = ctx.runner._circuit_breakers[breaker_key]
        if breaker.allow_request():
            return breaker, None
        logger.warning("[AgentGraph] Circuit breaker OPEN for %s, skipping task %s", ctx.agent_type.value, ctx.task.id)
        result = AgentResult(success=False, output=None, errors=[f"Circuit breaker open for {ctx.agent_type.value}"])
        return breaker, emit_task_done(result)
    except ModuleNotFoundError:
        logger.warning("Exception handled by  get backend circuit breaker fallback", exc_info=True)
        return None, None


def _get_agent_circuit_breaker(
    ctx: _TaskNodeRunContext,
    emit_task_done: Callable[[AgentResult], AgentResult],
) -> tuple[Any, AgentResult | None]:
    """Resolve and pre-check the per-agent task circuit breaker."""
    try:
        if not _module_is_available("vetinari.agents.agent_circuit_breaker"):
            raise ModuleNotFoundError("vetinari.agents.agent_circuit_breaker")
        breaker_key = f"agent_task_{ctx.agent_type.value}"
        if not hasattr(ctx.runner, "_agent_circuit_breakers"):
            ctx.runner._agent_circuit_breakers = {}
        if breaker_key not in ctx.runner._agent_circuit_breakers:
            ctx.runner._agent_circuit_breakers[breaker_key] = _get_agent_circuit_breaker_cls()(breaker_key)
        breaker = ctx.runner._agent_circuit_breakers[breaker_key]
        if breaker.allow_request():
            return breaker, None
        logger.warning(
            "[AgentGraph] Agent circuit breaker OPEN for %s - task %s bypassed", ctx.agent_type.value, ctx.task.id
        )
        result = AgentResult(
            success=False,
            output=None,
            errors=[f"Agent circuit breaker open for {ctx.agent_type.value} - too many consecutive failures"],
        )
        return breaker, emit_task_done(result)
    except ModuleNotFoundError:
        logger.warning("Exception handled by  get agent circuit breaker fallback", exc_info=True)
        return None, None


def _log_cost_estimate(ctx: _TaskNodeRunContext) -> None:
    """Log optional cost prediction for a task."""
    try:
        estimate = _get_cost_predictor_cls()().predict(
            task_type=_agent_type_value(ctx.agent_type), complexity=3, scope_size=1
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[AgentGraph] Cost estimate for %s: %d tokens, %.3fs, $%.4f (confidence=%.2f)",
                ctx.task.id,
                estimate.tokens,
                estimate.latency_seconds,
                estimate.cost_usd,
                estimate.confidence,
            )
    except Exception:
        logger.warning("Cost prediction unavailable for %s", ctx.task.id)


def _check_policy_enforcer(
    ctx: _TaskNodeRunContext,
    emit_task_done: Callable[[AgentResult], AgentResult],
) -> AgentResult | None:
    """Run policy enforcement before task execution."""
    try:
        decision = _get_policy_enforcer_fn()().check_action(
            agent_type=ctx.agent_type.value,
            action="execute_task",
            context={"task_id": ctx.task.id, "task_type": ctx.task.task_type},
        )
        if decision.allowed:
            return None
        logger.warning("[AgentGraph] Policy enforcer blocked task %s: %s", ctx.task.id, decision.reason)
        return emit_task_done(AgentResult(success=False, output=None, errors=[f"Policy blocked: {decision.reason}"]))
    except Exception:
        logger.warning("PolicyEnforcer unavailable for pre-execution check")
        return None


def _register_agent_monitor(ctx: _TaskNodeRunContext) -> Any:
    """Register task execution with the optional agent monitor."""
    try:
        monitor = _get_agent_monitor_fn()()
        monitor.register_agent(f"{ctx.agent_type.value}:{ctx.task.id}", timeout_seconds=300, max_steps=50)
        return monitor
    except Exception:
        logger.warning("AgentMonitor unavailable - executing without safety monitoring")
        return None


def _execute_task_node_steps(
    runner: Any,
    node: TaskNode,
    prior_results: dict[str, AgentResult] | None,
) -> AgentResult:
    """Execute one task node through setup checks and the retry loop."""
    from vetinari.agents.contracts import AgentResult, AgentTask

    globals()["AgentResult"] = AgentResult
    globals()["AgentTask"] = AgentTask
    ctx = _TaskNodeRunContext(runner, node, prior_results, node.task, node.task.assigned_agent, time.time() * 1000)
    for early_result in (_start_wip_claim(ctx), _check_operator_pause(ctx), _check_destructive_intent(ctx)):
        if early_result is not None:
            return early_result
    _emit_task_started(ctx)
    emit_task_done = _TaskDoneEmitter(ctx)
    _apply_resource_constraints(ctx)
    _validate_mode_and_task_type(ctx)
    agent, delegated_result = _resolve_agent_or_delegate(ctx, emit_task_done)
    if delegated_result is not None or agent is None:
        return delegated_result or AgentResult(success=False, output=None, errors=["Agent resolution failed"])
    permission_result = _enforce_permissions(ctx, emit_task_done)
    if permission_result is not None:
        return permission_result
    agent_task = _build_agent_task(ctx, agent)
    circuit_breaker, breaker_result = _get_backend_circuit_breaker(ctx, emit_task_done)
    if breaker_result is not None:
        return breaker_result
    agent_circuit_breaker, agent_breaker_result = _get_agent_circuit_breaker(ctx, emit_task_done)
    if agent_breaker_result is not None:
        return agent_breaker_result
    _log_cost_estimate(ctx)
    policy_result = _check_policy_enforcer(ctx, emit_task_done)
    if policy_result is not None:
        return policy_result
    return runner._run_task_attempt_loop(
        node=node,
        agent=agent,
        agent_type=ctx.agent_type,
        agent_task=agent_task,
        _monitor=_register_agent_monitor(ctx),
        _cb=circuit_breaker,
        _agent_cb=agent_circuit_breaker,
        _emit_task_done=emit_task_done,
    )
