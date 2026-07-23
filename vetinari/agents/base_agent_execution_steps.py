"""Safe BaseAgent execution helpers.

This module owns validation, guardrails, observability, and cleanup around an
agent's core execution function.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vetinari.agents.contracts import AgentResult, AgentTask
from vetinari.guards import require_subsystem

if TYPE_CHECKING:
    from vetinari.agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)

_UNTRUSTED_OPEN = "<<<UNTRUSTED_USER_CONTENT_BEGIN>>>"
_UNTRUSTED_CLOSE = "<<<UNTRUSTED_USER_CONTENT_END>>>"


@dataclass(slots=True)
class _SafeExecutionState:
    """Mutable cleanup state for one safe agent execution."""

    agent: BaseAgent
    task: AgentTask
    execute_fn: Callable[[AgentTask], AgentResult]
    monitor: object | None = None
    monitor_agent_id: str | None = None
    monitor_owns_registration: bool = False
    genai_span: object | None = None
    lease_holder_id: str | None = None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"agent={self.agent!r}, "
            f"task={self.task!r}, "
            f"execute_fn={self.execute_fn!r}, "
            f"monitor={self.monitor!r}"
            ")"
        )


def _record_task_start(agent: BaseAgent, task: AgentTask) -> None:
    """Record task start in the optional execution-context audit trail."""
    try:
        from vetinari.execution_context import current_context

        current_context().record_operation(
            operation_name=f"agent_task_start:{agent.agent_type.value}",
            params={"task_id": task.task_id},
            result="started",
        )
    except Exception:
        logger.warning("Could not record task start in execution context")


def _register_monitor(agent: BaseAgent, task: AgentTask) -> tuple[object | None, str | None]:
    """Register the task with the optional agent health monitor."""
    try:
        from vetinari.safety.agent_monitor import get_agent_monitor

        monitor = get_agent_monitor()
        monitor_agent_id = f"{agent.agent_type.value}:{task.task_id}"
        registered = bool(monitor.register_agent(monitor_agent_id, timeout_seconds=300, max_steps=100))
        if not registered:
            return None, None
        return monitor, monitor_agent_id
    except Exception:
        logger.warning("AgentMonitor unavailable - heartbeat tracking disabled for %s", agent.agent_type.value)
        return None, None


def _start_genai_span(agent: BaseAgent, task: AgentTask) -> object | None:
    """Start the optional GenAI tracing span for this agent execution."""
    try:
        from vetinari.observability.otel_genai import get_genai_tracer

        span = get_genai_tracer().start_agent_span(
            agent_name=agent.name,
            operation="execute",
            model=getattr(agent, "default_model", ""),
        )
        span.attributes["agent_type"] = agent.agent_type.value
        span.attributes["mode"] = getattr(task, "mode", "default")
        return span
    except Exception:
        logger.warning("GenAI tracer unavailable for agent span")
        return None


def _inject_relevant_memories(agent: BaseAgent, task: AgentTask) -> None:
    """Attach recalled memories to the task context before execution."""
    prior_memories = agent._recall_relevant_memories(task.description or "")
    recall_status = getattr(agent, "_last_memory_recall_status", None)
    context = getattr(task, "context", None) or {}
    if isinstance(recall_status, dict):
        context["memory_recall_status"] = recall_status
    if not prior_memories:
        if context is not getattr(task, "context", None):
            task.context = context
        return
    context["prior_memories"] = prior_memories
    task.context = context
    agent._log("debug", "Injected %d prior memories into task context", len(prior_memories))


def _enforce_agent_model_permission(agent: BaseAgent) -> AgentResult | None:
    """Run per-agent model-inference permission checks."""
    try:
        from vetinari.exceptions import SecurityError
        from vetinari.execution_context import ToolPermission, enforce_agent_permissions

        enforce_agent_permissions(agent.agent_type, ToolPermission.MODEL_INFERENCE)
        return None
    except (PermissionError, SecurityError) as perm_err:
        logger.warning("Agent permission denied: %s", perm_err)
        agent._log("warning", "Agent permission denied: %s", perm_err)
        return AgentResult(success=False, output=None, errors=[str(perm_err)])
    except Exception:
        logger.warning("Agent permission check not available in base_agent")
        return None


def _sanitize_task_description(task: AgentTask) -> None:
    """Wrap or sanitize user-provided task descriptions before prompt use."""
    try:
        from vetinari.safety.prompt_sanitizer import is_content_delimited, sanitize_task_description

        if task.description and not is_content_delimited(task.description):
            task.description = sanitize_task_description(task.description)
    except Exception:
        logger.warning(
            "Prompt sanitizer unavailable for task %s - applying inline delimiter wrap only (FSA-0207)",
            task.task_id,
        )
        if task.description:
            task.description = f"{_UNTRUSTED_OPEN}\n{task.description}\n{_UNTRUSTED_CLOSE}"


def _check_input_guardrails(agent: BaseAgent, task: AgentTask) -> AgentResult | None:
    """Run input guardrails and fail closed on guardrail errors."""
    try:
        with require_subsystem("agent_guardrails", "safety_module"):
            from vetinari.safety.guardrails import RailContext, get_guardrails

            input_text = task.description or ""
            if not input_text:
                return None
            input_check = get_guardrails().check_input(input_text, context=RailContext.USER_FACING)
        if input_check.allowed:
            return None
        violations = "; ".join(violation.description for violation in input_check.violations)
        agent._log("warning", "Input guardrail blocked task: %s", violations)
        return AgentResult(success=False, output=None, errors=[f"Input guardrail violation: {violations}"])
    except Exception as exc:
        logger.warning("Input guardrail check failed - blocking execution: %s", exc)
        return AgentResult(success=False, output=None, errors=[f"Input guardrail system error: {exc}"])


def _set_inference_task_state(agent: BaseAgent, task: AgentTask) -> None:
    """Expose task context to InferenceBehavior for this execution."""
    task_context = getattr(task, "context", None) or {}
    agent._current_task_memories = task_context.get("prior_memories")
    task_type = getattr(task, "task_type", None)
    context_task_type = task_context.get("type") if isinstance(task_context, dict) else None
    agent._current_task_type = (
        task_type
        if isinstance(task_type, str) and task_type
        else context_task_type
        if isinstance(context_task_type, str) and context_task_type
        else "general"
    )


def _heartbeat(state: _SafeExecutionState) -> None:
    """Send a best-effort health-monitor heartbeat."""
    if state.monitor_agent_id is not None and state.monitor is not None:
        with contextlib.suppress(Exception):
            state.monitor.heartbeat(state.monitor_agent_id)


def _acquire_vram_lease(agent: BaseAgent, task: AgentTask) -> str | None:
    """Acquire an optional VRAM lease for the model used by this task."""
    lease_model_id = getattr(agent, "default_model", "") or ""
    if not lease_model_id:
        return None
    try:
        from vetinari.models.vram_manager import get_vram_manager

        lease_holder_id = f"{agent.agent_type.value}:{task.task_id}"
        if get_vram_manager().acquire_lease(lease_model_id, lease_holder_id):
            return lease_holder_id
    except Exception:
        logger.warning("VRAM lease unavailable - proceeding without eviction protection for task %s", task.task_id)
    return None


def _run_self_check(agent: BaseAgent, task: AgentTask, result: AgentResult) -> None:
    """Run optional skill self-check on successful output."""
    if not result.success or not result.output:
        return
    try:
        from vetinari.agents.skill_contract import SkillOutput, self_check

        skill_output = SkillOutput(
            agent_type=agent.agent_type.value,
            task_summary=task.description or "",
            verdict=SkillOutput.__dataclass_fields__["verdict"].default
            if hasattr(SkillOutput, "__dataclass_fields__")
            else None,
            confidence=0.8,
        )
        if isinstance(result.output, dict):
            skill_output.task_summary = result.output.get("task_summary", task.description or "")
            skill_output.confidence = result.output.get("confidence", 0.8)
        checked = self_check(skill_output)
        result.metadata["self_check_passed"] = checked.self_check_passed
        result.metadata["self_check_issues"] = checked.self_check_issues
        if not checked.self_check_passed:
            agent._log(
                "warning",
                "Self-check FAILED with %d issue(s): %s",
                len(checked.self_check_issues),
                "; ".join(checked.self_check_issues[:3]),
            )
            result.metadata["self_check_gate_hint"] = "stricter"
    except Exception as exc:
        logger.warning("Self-check failed (non-fatal): %s", exc)


def _stamp_inference_metadata(agent: BaseAgent, result: AgentResult) -> None:
    """Attach last inference metadata for downstream confidence routing."""
    inference_metadata = getattr(agent, "_last_inference_metadata", None)
    if inference_metadata:
        result.metadata["inference_metadata"] = inference_metadata


def _record_meta_adapter_outcome(agent: BaseAgent, task: AgentTask, result: AgentResult) -> None:
    """Record successful execution outcomes for strategy learning."""
    if not result.success:
        return
    try:
        from vetinari.learning.meta_adapter import StrategyBundle, get_meta_adapter

        mode = getattr(task, "mode", "default")
        task_type = getattr(task, "task_type", "general")
        get_meta_adapter().record_outcome(
            task_description=task.description or "",
            task_type=task_type if isinstance(task_type, str) and task_type else "general",
            strategy_used=StrategyBundle(),
            quality_score=result.metadata.get("quality_score", 0.7),
            success=True,
            mode=mode if isinstance(mode, str) and mode else "default",
        )
    except Exception:
        logger.warning("MetaAdapter outcome recording unavailable")


def _check_output_guardrails(agent: BaseAgent, result: AgentResult) -> AgentResult | None:
    """Run output guardrails and fail closed on guardrail errors."""
    if not result.success:
        return None
    try:
        import json as json_module

        with require_subsystem("agent_guardrails", "safety_module"):
            from vetinari.safety.guardrails import RailContext, get_guardrails

            output_text = (
                result.output if isinstance(result.output, str) else json_module.dumps(result.output, default=str)
            )
            if not output_text:
                return None
            guardrail_result = get_guardrails().check_output(output_text, context=RailContext.USER_FACING)
        if guardrail_result.allowed:
            return None
        violations = "; ".join(violation.description for violation in guardrail_result.violations)
        agent._log("warning", "Output guardrail blocked: %s", violations)
        return AgentResult(success=False, output=None, errors=[f"Output guardrail violation: {violations}"])
    except Exception as exc:
        logger.warning("Output guardrail check failed - blocking output: %s", exc)
        return AgentResult(success=False, output=None, errors=[f"Output guardrail system error: {exc}"])


def _record_operation_outcome(agent: BaseAgent, task: AgentTask, result: AgentResult) -> None:
    """Record task completion in the execution-context audit trail."""
    try:
        from vetinari.execution_context import current_context

        current_context().record_operation(
            operation_name=f"agent_execute:{agent.agent_type.value}",
            params={"task_id": task.task_id, "description": (task.description or "")[:200]},
            result="success" if result.success else "failed",
        )
    except Exception:
        logger.warning("Could not record operation in execution context")


def _close_genai_span(span: object | None, success: bool) -> None:
    """Close an optional GenAI span with the final status."""
    if span is None:
        return
    try:
        from vetinari.observability.otel_genai import get_genai_tracer

        get_genai_tracer().end_agent_span(span, status="ok" if success else "error")
    except Exception:
        logger.warning("Failed to close GenAI span")


def _release_vram_lease(lease_holder_id: str | None) -> None:
    """Release an optional VRAM lease."""
    if lease_holder_id is None:
        return
    with contextlib.suppress(Exception):
        from vetinari.models.vram_manager import get_vram_manager

        get_vram_manager().release_lease(lease_holder_id)


def _cleanup_inference_task_state(agent: BaseAgent) -> None:
    """Clear per-task inference state from the agent instance."""
    agent._current_task_memories = None
    agent._current_task_type = None


def _deregister_monitor(state: _SafeExecutionState) -> None:
    """Deregister the optional health monitor entry."""
    if state.monitor_owns_registration and state.monitor_agent_id is not None and state.monitor is not None:
        with contextlib.suppress(Exception):
            state.monitor.deregister_agent(state.monitor_agent_id)


def _run_execution_body(state: _SafeExecutionState) -> AgentResult:
    """Run guardrails, core execution, learning hooks, and success cleanup."""
    _inject_relevant_memories(state.agent, state.task)
    permission_result = _enforce_agent_model_permission(state.agent)
    if permission_result is not None:
        return permission_result
    _sanitize_task_description(state.task)
    input_result = _check_input_guardrails(state.agent, state.task)
    if input_result is not None:
        return input_result
    _set_inference_task_state(state.agent, state.task)
    _heartbeat(state)
    state.lease_holder_id = _acquire_vram_lease(state.agent, state.task)
    result = state.execute_fn(state.task)
    _run_self_check(state.agent, state.task, result)
    _stamp_inference_metadata(state.agent, result)
    _record_meta_adapter_outcome(state.agent, state.task, result)
    output_result = _check_output_guardrails(state.agent, result)
    if output_result is not None:
        return output_result
    if result.success:
        state.agent.complete_task(state.task, result)
    _record_operation_outcome(state.agent, state.task, result)
    return result


def _execute_safely_steps(
    agent: BaseAgent, task: AgentTask, execute_fn: Callable[[AgentTask], AgentResult]
) -> AgentResult:
    """Run the full safe-execution template for a BaseAgent task."""
    if not agent.validate_task(task):
        return AgentResult(success=False, output=None, errors=[f"Task validation failed for {agent.agent_type}"])
    prepared_task = agent.prepare_task(task)
    _record_task_start(agent, prepared_task)
    monitor, monitor_agent_id = _register_monitor(agent, prepared_task)
    state = _SafeExecutionState(
        agent=agent,
        task=prepared_task,
        execute_fn=execute_fn,
        monitor=monitor,
        monitor_agent_id=monitor_agent_id,
        monitor_owns_registration=monitor_agent_id is not None,
        genai_span=_start_genai_span(agent, prepared_task),
    )
    try:
        result = _run_execution_body(state)
        _close_genai_span(state.genai_span, result.success)
        return result
    except Exception as exc:
        logger.error("[%s] Execute failed: %s", agent.agent_type, exc)
        _close_genai_span(state.genai_span, False)
        return AgentResult(success=False, output=None, errors=[str(exc)])
    finally:
        _release_vram_lease(state.lease_holder_id)
        _cleanup_inference_task_state(agent)
        _deregister_monitor(state)
