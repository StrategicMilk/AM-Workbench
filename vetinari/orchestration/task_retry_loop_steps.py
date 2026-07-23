"""Attempt-level helpers for graph task retry execution."""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vetinari.boundary_guards import account_evidence_drop
from vetinari.constants import TRUNCATE_OUTPUT_PREVIEW
from vetinari.types import AgentType

if TYPE_CHECKING:
    from vetinari.agents.contracts import AgentResult, AgentTask

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _TaskAttemptLoopContext:
    """Shared state for a task retry loop."""

    runner: Any
    node: Any
    agent: Any
    agent_type: AgentType
    agent_task: AgentTask
    monitor: Any
    circuit_breaker: Any
    agent_circuit_breaker: Any
    emit_task_done: Any

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"runner={self.runner!r}, "
            f"node={self.node!r}, "
            f"agent={self.agent!r}, "
            f"agent_type={self.agent_type!r}"
            ")"
        )


@dataclass(frozen=True, slots=True)
class _AttemptDecision:
    """Result or retry instruction produced by one execution attempt."""

    result: AgentResult | None = None
    retry: bool = False
    terminal: bool = False


def _task_from(ctx: _TaskAttemptLoopContext) -> Any:
    """Return the source task from the retry-loop node."""
    return ctx.node.task


def _record_heartbeat(ctx: _TaskAttemptLoopContext) -> None:
    """Record an optional monitor heartbeat for the active task."""
    if ctx.monitor:
        task = _task_from(ctx)
        item = {"task_id": task.id, "agent_type": ctx.agent_type.value}
        try:
            ctx.monitor.heartbeat(f"{ctx.agent_type.value}:{_task_from(ctx).id}")
        except Exception:
            account_evidence_drop(item, "retry_heartbeat", logger=logger)


def _budget_exhausted_result(ctx: _TaskAttemptLoopContext) -> AgentResult | None:
    """Return a failure result when the agent budget is exhausted."""
    task = _task_from(ctx)
    agent_budget = getattr(ctx.agent, "_budget", None)
    if agent_budget is None or type(agent_budget).__name__ != "BudgetTracker" or not agent_budget.is_exhausted():
        return None
    snap = ctx.agent._budget.snapshot()
    logger.warning(
        "[AgentGraph] Budget exhausted for %s on task %s - tokens=%d/%d, iterations=%d/%d, cost=$%.4f/$%.2f",
        ctx.agent_type.value,
        task.id,
        snap.tokens_used,
        snap.token_budget,
        snap.iterations_used,
        snap.iteration_cap,
        snap.cost_used_usd,
        snap.cost_budget_usd,
    )
    return ctx.emit_task_done(
        AgentResult(
            success=False,
            output=None,
            errors=[
                f"Budget exhausted for {ctx.agent_type.value}: "
                f"tokens={snap.tokens_used}/{snap.token_budget}, "
                f"iterations={snap.iterations_used}/{snap.iteration_cap}"
            ],
        )
    )


def _temporary_execution_context(ctx: _TaskAttemptLoopContext) -> Any:
    """Enter optional execution mode for tool permission checks."""
    try:
        from vetinari.execution_context import get_context_manager
        from vetinari.types import ExecutionMode

        exec_ctx = get_context_manager().temporary_mode(ExecutionMode.EXECUTION, task_id=_task_from(ctx).id)
        exec_ctx.__enter__()
        return exec_ctx
    except Exception:
        logger.warning("Exception handled by  temporary execution context fallback", exc_info=True)
        return None


def _enforce_execution_constraints(ctx: _TaskAttemptLoopContext) -> AgentResult | None:
    """Run optional enforcement constraints before agent execution."""
    try:
        from vetinari.enforcement import enforce_all

        enforce_all(agent_type=ctx.agent_type, current_depth=getattr(ctx.node, "delegation_depth", None))
        return None
    except ImportError:
        logger.debug("Enforcement module not available, skipping pre-execution check")
        return None
    except Exception as exc:
        logger.warning("[AgentGraph] Enforcement blocked task %s: %s", _task_from(ctx).id, exc)
        return ctx.emit_task_done(AgentResult(success=False, output=None, errors=[f"Enforcement blocked: {exc}"]))


def _execute_agent(ctx: _TaskAttemptLoopContext) -> AgentResult:
    """Execute the assigned agent under the optional execution context."""
    exec_ctx = _temporary_execution_context(ctx)
    try:
        return ctx.agent.execute(ctx.agent_task)
    finally:
        if exec_ctx is not None:
            with contextlib.suppress(Exception):
                exec_ctx.__exit__(None, None, None)


def _record_post_attempt_bookkeeping(ctx: _TaskAttemptLoopContext, result: AgentResult) -> None:
    """Record iteration, monitor, and circuit-breaker outcomes."""
    iter_budget = getattr(ctx.agent, "_budget", None)
    if iter_budget is not None and type(iter_budget).__name__ == "BudgetTracker":
        iter_budget.record_iteration()
    if ctx.monitor:
        task = _task_from(ctx)
        item = {"task_id": task.id, "agent_type": ctx.agent_type.value}
        try:
            ctx.monitor.record_step(f"{ctx.agent_type.value}:{_task_from(ctx).id}")
        except Exception:
            account_evidence_drop(item, "retry_step_record", logger=logger)
            raise
    for breaker in (ctx.circuit_breaker, ctx.agent_circuit_breaker):
        if breaker is None:
            continue
        if result.success:
            breaker.record_success()
        else:
            breaker.record_failure()


def _check_stagnation(ctx: _TaskAttemptLoopContext, result: AgentResult) -> AgentResult | None:
    """Fail a task when global or scoped stagnation is detected."""
    detector = getattr(ctx.runner, "_stagnation_detector", None)
    if detector is None:
        return None
    task = _task_from(ctx)
    output_str = str(result.output)[:200] if result.output else ""
    detector.record_output(output_str)
    if not result.success:
        detector.record_error()
    if detector.is_stagnant():
        reasons = detector.stagnation_reasons()
        logger.warning("[AgentGraph] Stagnation detected for task %s: %s", task.id, "; ".join(reasons))
        return ctx.emit_task_done(
            AgentResult(success=False, output=result.output, errors=[f"Stagnation detected: {'; '.join(reasons)}"])
        )
    if detector.detect_scoped(ctx.agent_type.value, output_str, error=not result.success):
        _pause_stagnant_scope(ctx)
    return None


def _pause_stagnant_scope(ctx: _TaskAttemptLoopContext) -> None:
    """Pause the agent scope when scoped stagnation is detected."""
    task = _task_from(ctx)
    scope_str = ctx.agent_type.value
    try:
        from vetinari.workflow.andon import AndonSignal, get_andon_system

        andon = get_andon_system()
        if not andon.is_scope_paused(scope_str):
            andon.pause_scope(
                scope_str,
                AndonSignal(
                    source="stagnation_detector",
                    severity="warning",
                    message=f"Scoped stagnation in {scope_str} for task {task.id}",
                    affected_tasks=[str(task.id)],
                    scope=scope_str,
                ),
            )
    except Exception as exc:
        logger.warning(
            "Andon scope pause skipped for scope %r after stagnation on task %s - andon unavailable: %s",
            scope_str,
            task.id,
            exc,
        )


def _apply_inter_agent_guardrails(ctx: _TaskAttemptLoopContext, result: AgentResult) -> AgentResult:
    """Block unsafe inter-agent output before verification."""
    if not result.success or not result.output:
        return result
    try:
        from vetinari.safety.guardrails import RailContext, get_guardrails

        output_text = str(result.output)[:TRUNCATE_OUTPUT_PREVIEW]
        guard_result = get_guardrails().check_output(output_text, context=RailContext.CODE_EXECUTION)
        if guard_result.allowed:
            return result
        violations = (
            "; ".join(str(violation) for violation in guard_result.violations)
            if guard_result.violations
            else "policy violation"
        )
        logger.warning(
            "[AgentGraph] Inter-agent guardrail BLOCKED output from %s on task %s: %s",
            ctx.agent_type.value,
            _task_from(ctx).id,
            violations,
        )
        return AgentResult(success=False, output=None, errors=[f"Guardrail blocked: {violations}"])
    except ImportError:
        logger.warning("Exception handled by  apply inter agent guardrails fallback", exc_info=True)
        return result


def _record_goal_adherence(ctx: _TaskAttemptLoopContext, result: AgentResult) -> None:
    """Record goal-drift warnings on successful outputs."""
    if not ctx.runner._goal_tracker or not result.success:
        return
    task = _task_from(ctx)
    try:
        output_str = str(result.output)[:500] if result.output else ""
        adherence = ctx.runner._goal_tracker.check_adherence(output_str, task.description or "")
        if adherence.score < 0.4:
            logger.warning(
                "[AgentGraph] Goal drift in %s: score=%.2f - %s",
                task.id,
                adherence.score,
                adherence.deviation_description,
            )
            result.metadata["drift_warning"] = adherence.to_dict()
    except Exception:
        logger.warning("Goal adherence check failed for task %s", task.id, exc_info=True)


def _handle_delegation(ctx: _TaskAttemptLoopContext, result: AgentResult) -> _AttemptDecision:
    """Handle explicit agent delegation requests."""
    if not result.metadata.get("delegation_requested"):
        return _AttemptDecision(result=result)
    task = _task_from(ctx)
    reason = result.metadata.get("delegation_reason", "no reason given")
    logger.info("[AgentGraph] %s delegated task '%s': %s - finding substitute", ctx.agent_type.value, task.id, reason)
    delegate_type = ctx.runner._find_delegate(task, exclude=ctx.agent_type)
    if delegate_type and delegate_type in ctx.runner._agents:
        return _AttemptDecision(result=ctx.runner._agents[delegate_type].execute(ctx.agent_task))
    return _AttemptDecision(
        result=ctx.emit_task_done(
            AgentResult(
                success=False,
                output=None,
                errors=[f"Task delegated by {ctx.agent_type.value} but no substitute found: {reason}"],
            )
        ),
        terminal=True,
    )


def _handle_needs_info(ctx: _TaskAttemptLoopContext, result: AgentResult, attempt: int) -> _AttemptDecision:
    """Handle mid-task info requests through another agent or user input."""
    if not result.metadata.get("needs_info"):
        return _AttemptDecision(result=result)
    delegate_to = result.metadata.get("delegate_to")
    question = result.metadata.get("question", "")
    if delegate_to and delegate_to in {agent_type.value for agent_type in ctx.runner._agents}:
        delegate_type = AgentType(delegate_to)
        logger.info("[AgentGraph] %s needs info from %s: %s", ctx.agent_type.value, delegate_to, question[:100])
        info_result = ctx.runner._agents[delegate_type].execute(
            _info_request_task(ctx, delegate_type, question, attempt)
        )
        if info_result.success:
            ctx.agent_task.context["info_response"] = str(info_result.output)[:TRUNCATE_OUTPUT_PREVIEW]
            ctx.agent_task.description = (
                f"{_task_from(ctx).description}\n\n[INFO RESPONSE] {str(info_result.output)[:TRUNCATE_OUTPUT_PREVIEW]}"
            )
            return _AttemptDecision(result=ctx.agent.execute(ctx.agent_task))
    elif result.metadata.get("needs_user_input"):
        logger.info("[AgentGraph] %s needs user input: %s", ctx.agent_type.value, question[:100])
        return _AttemptDecision(result=ctx.emit_task_done(result), terminal=True)
    return _AttemptDecision(result=result)


def _info_request_task(
    ctx: _TaskAttemptLoopContext,
    delegate_type: AgentType,
    question: str,
    attempt: int,
) -> AgentTask:
    """Build a delegated info-request task."""
    task = _task_from(ctx)
    return AgentTask(
        task_id=f"{task.id}_info_{attempt}",
        agent_type=delegate_type,
        description=question,
        prompt=question,
        context={"original_task": task.id, "requesting_agent": ctx.agent_type.value},
    )


def _handle_verified_success(
    ctx: _TaskAttemptLoopContext, result: AgentResult, verification: Any
) -> AgentResult | None:
    """Apply success-path validation and bookkeeping after verification passes."""
    if not result.success or not verification.passed:
        return None
    task = _task_from(ctx)
    schema_issues = ctx.runner._validate_output_schema(ctx.agent_type, result.output)
    result.metadata["schema_valid"] = len(schema_issues) == 0
    if schema_issues:
        result.metadata["schema_issues"] = schema_issues
        logger.info("[AgentGraph] %s output schema deviations: %s", task.id, "; ".join(schema_issues))
    if ctx.agent_type in ctx.runner._quality_reviewed_agents and AgentType.INSPECTOR in ctx.runner._agents:
        result = ctx.runner._apply_maker_checker(task, result)
    _record_config_self_tuning(ctx)
    return ctx.emit_task_done(result)


def _record_config_self_tuning(ctx: _TaskAttemptLoopContext) -> None:
    """Record task completion with the optional config self-tuner."""
    task = _task_from(ctx)
    try:
        from vetinari.config.self_tuning import get_config_self_tuner

        task_type = getattr(task, "type", None) or getattr(ctx.node, "task_type", None) or "general"
        get_config_self_tuner().record_task_completion(task_type)
    except Exception:
        logger.warning("Config self-tuner unavailable for task %s", task.id, exc_info=True)


def _maybe_prepare_retry(ctx: _TaskAttemptLoopContext, verification: Any, attempt: int) -> bool:
    """Inject verification feedback when another retry is available."""
    if verification.passed or attempt >= ctx.node.max_retries:
        return False
    task = _task_from(ctx)
    issues_text = _issues_text(verification.issues)
    fix_hint = _retry_fix_hint(task, issues_text)
    logger.warning("[AgentGraph] %s verification failed: %s - injecting feedback and retrying", task.id, issues_text)
    ctx.agent_task.description = (
        f"{task.description}\n\n"
        f"[SELF-CORRECTION] Previous attempt failed verification. "
        f"Issues: {issues_text}. Please fix these issues.{fix_hint}"
    )
    ctx.node.retries += 1
    return True


def _issues_text(issues: list[Any]) -> str:
    """Convert verifier issues into retry feedback text."""
    return "; ".join(issue.get("message", str(issue)) if isinstance(issue, dict) else str(issue) for issue in issues)


def _retry_fix_hint(task: Any, issues_text: str) -> str:
    """Return a known-fix hint from retry intelligence when available."""
    try:
        from vetinari.resilience.retry_intelligence import get_retry_analyzer

        retry_strategy = get_retry_analyzer().analyze(
            failure_trace=issues_text,
            error_msg=issues_text,
            task_type=getattr(task, "type", ""),
        )
        if retry_strategy.known and retry_strategy.confidence >= 0.7:
            logger.info(
                "[AgentGraph] Retry intelligence: known fix for %s (confidence=%.2f)",
                task.id,
                retry_strategy.confidence,
            )
            return f"\n[KNOWN FIX] {retry_strategy.fix_action}"
    except Exception:
        logger.warning("Retry intelligence unavailable for task %s", task.id, exc_info=True)
    return ""


def _final_attempt_result(
    ctx: _TaskAttemptLoopContext,
    result: AgentResult,
    verification: Any,
    attempt: int,
) -> AgentResult:
    """Return the final failure or Worker recovery result."""
    if AgentType.WORKER in ctx.runner._agents and attempt >= ctx.node.max_retries:
        return ctx.emit_task_done(ctx.runner._run_error_recovery(_task_from(ctx), result, verification))
    return ctx.emit_task_done(
        AgentResult(
            success=False,
            output=result.output,
            errors=[f"Verification failed after {attempt + 1} attempts: " + _issues_text(verification.issues)],
        )
    )


def _run_single_attempt(ctx: _TaskAttemptLoopContext, attempt: int) -> _AttemptDecision:
    """Run one attempt and return either a terminal result or retry decision."""
    _record_heartbeat(ctx)
    exhausted = _budget_exhausted_result(ctx)
    if exhausted is not None:
        return _AttemptDecision(result=exhausted, terminal=True)
    task = _task_from(ctx)
    logger.info(
        "[AgentGraph] Executing %s with %s (attempt %d/%d)",
        task.id,
        ctx.agent_type.value,
        attempt + 1,
        ctx.node.max_retries + 1,
    )
    blocked = _enforce_execution_constraints(ctx)
    if blocked is not None:
        return _AttemptDecision(result=blocked, terminal=True)
    result = _execute_agent(ctx)
    _record_post_attempt_bookkeeping(ctx, result)
    stagnant = _check_stagnation(ctx, result)
    if stagnant is not None:
        return _AttemptDecision(result=stagnant, terminal=True)
    result = _apply_inter_agent_guardrails(ctx, result)
    _record_goal_adherence(ctx, result)
    delegation = _handle_delegation(ctx, result)
    if delegation.terminal:
        return delegation
    result = delegation.result or result
    needs_info = _handle_needs_info(ctx, result, attempt)
    if needs_info.terminal:
        return needs_info
    result = needs_info.result or result
    verification = ctx.agent.verify(result.output)
    verified = _handle_verified_success(ctx, result, verification)
    if verified is not None:
        return _AttemptDecision(result=verified, terminal=True)
    if _maybe_prepare_retry(ctx, verification, attempt):
        return _AttemptDecision(retry=True)
    return _AttemptDecision(result=_final_attempt_result(ctx, result, verification, attempt), terminal=True)


def _run_task_attempt_loop_steps(
    runner: Any,
    node: Any,
    agent: Any,
    agent_type: AgentType,
    agent_task: AgentTask,
    monitor: Any,
    circuit_breaker: Any,
    agent_circuit_breaker: Any,
    emit_task_done: Any,
) -> AgentResult:
    """Execute retry attempts for one graph task."""
    global AgentResult, AgentTask
    from vetinari.agents.contracts import AgentResult, AgentTask

    ctx = _TaskAttemptLoopContext(
        runner,
        node,
        agent,
        agent_type,
        agent_task,
        monitor,
        circuit_breaker,
        agent_circuit_breaker,
        emit_task_done,
    )
    for attempt in range(node.max_retries + 1):
        try:
            decision = _run_single_attempt(ctx, attempt)
            if decision.retry:
                continue
            if decision.result is not None:
                return decision.result
        except Exception as exc:
            logger.error("[AgentGraph] %s raised exception: %s", node.task.id, exc)
            if attempt < node.max_retries:
                continue
            return emit_task_done(AgentResult(success=False, output=None, errors=[str(exc)]))
    return emit_task_done(AgentResult(success=False, output=None, errors=["Task failed after all retries"]))
