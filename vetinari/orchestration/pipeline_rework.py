"""RCA-driven rework routing for the pipeline quality layer."""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import Enum
from typing import Any, Protocol, cast

from vetinari.constants import MAX_REWORK_CYCLES
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


class _ReworkExecutionEngineLike(Protocol):
    """Durable execution engine surface used by this mixin."""

    _task_handlers: dict[str, Callable[..., Any]]

    def _execute_task(self, graph: Any, node: Any) -> dict[str, Any]:
        """Execute a single graph node."""

    def _save_checkpoint(self, plan_id: str, graph: Any) -> None:
        """Persist a graph checkpoint."""


class _PipelineReworkOwner(Protocol):
    """Host contract required by PipelineReworkController."""

    execution_engine: _ReworkExecutionEngineLike

    def _route_model_for_task(self, task: Any) -> str:
        """Select a model for a task node."""


class ReworkDecision(Enum):
    """Decision on how to handle a quality rejection."""

    RETRY_SAME_AGENT = "retry_same_agent"
    RETRY_DIFFERENT_MODEL = "retry_different_model"
    REPLAN = "replan"
    REPLAN_WIDER_SCOPE = "replan_wider_scope"
    RESEARCH_THEN_RETRY = "research_then_retry"
    ESCALATE_TO_USER = "escalate_to_user"


_ROOT_CAUSE_ROUTING: dict[str, ReworkDecision] = {
    "bad_spec": ReworkDecision.REPLAN,
    "wrong_model": ReworkDecision.RETRY_DIFFERENT_MODEL,
    "hallucination": ReworkDecision.RETRY_SAME_AGENT,
    "context": ReworkDecision.RESEARCH_THEN_RETRY,
    "integration": ReworkDecision.REPLAN_WIDER_SCOPE,
    "complexity": ReworkDecision.REPLAN,
    "prompt": ReworkDecision.RETRY_SAME_AGENT,
}


class PipelineReworkController:
    """RCA-driven rework routing for the pipeline."""

    @staticmethod
    def _select_rework_by_quality_score(task_id: str, quality_score: float) -> ReworkDecision:
        """Route rework based on quality score percentiles."""
        if quality_score >= 0.85:
            decision = ReworkDecision.ESCALATE_TO_USER
        elif quality_score >= 0.70:
            decision = ReworkDecision.RETRY_SAME_AGENT
        elif quality_score >= 0.50:
            decision = ReworkDecision.RETRY_DIFFERENT_MODEL
        elif quality_score >= 0.30:
            decision = ReworkDecision.RESEARCH_THEN_RETRY
        else:
            decision = ReworkDecision.ESCALATE_TO_USER

        logger.info("[QualityRework] Task %s quality_score=%.3f -> %s", task_id, quality_score, decision.value)
        return decision

    @staticmethod
    def _audit_rework_decision(
        task_id: str,
        category: str,
        decision: ReworkDecision,
        rework_count: int,
    ) -> None:
        """Log a retry/rework routing decision to the optional audit trail."""
        try:
            from vetinari.audit import get_audit_logger

            get_audit_logger().log_decision(
                decision_type="retry_action",
                choice=decision.value,
                reasoning=f"Root cause category '{category}' for task {task_id} (rework #{rework_count})",
                alternatives=[d.value for d in ReworkDecision if d != decision],
                context={"task_id": task_id, "root_cause_category": category, "rework_count": rework_count},
            )
        except Exception:
            logger.warning("Audit logging failed during rework routing", exc_info=True)

    def _handle_quality_rejection(
        self,
        task_id: str,
        result: dict[str, Any],
        rework_count: int,
    ) -> ReworkDecision:
        """Route corrective action based on root cause analysis and quality score."""
        if rework_count >= MAX_REWORK_CYCLES:
            logger.warning(
                "[RCA] Max rework cycles reached for task %s (rework_count=%d) - escalating", task_id, rework_count
            )
            return ReworkDecision.ESCALATE_TO_USER

        if isinstance(result, dict):
            quality_score = result.get("quality_score") or result.get("score")
            if isinstance(quality_score, (int, float)) and 0.0 <= quality_score <= 1.0:
                return self._select_rework_by_quality_score(task_id, float(quality_score))

        root_cause = result.get("root_cause") if isinstance(result, dict) else None
        if not root_cause:
            logger.info("[RCA] No root_cause in result for task %s - defaulting to retry_same_agent", task_id)
            return ReworkDecision.RETRY_SAME_AGENT
        if not isinstance(root_cause, dict):
            logger.warning(
                "[RCA] Malformed root_cause for task %s (%s) - escalating for explicit recovery",
                task_id,
                type(root_cause).__name__,
            )
            return ReworkDecision.ESCALATE_TO_USER

        category = root_cause.get("category", "")
        decision = _ROOT_CAUSE_ROUTING.get(category, ReworkDecision.RETRY_SAME_AGENT)
        logger.info(
            "[RCA] Task %s quality rejection routed: category=%s, decision=%s", task_id, category, decision.value
        )
        self._audit_rework_decision(task_id, category, decision, rework_count)
        return decision

    @staticmethod
    def _build_rejection_feedback(task_result: Any) -> str:
        """Build retry feedback from a rejected task result."""
        rejection_feedback = ""
        if isinstance(task_result, dict):
            rejection_feedback = task_result.get("summary", "") or task_result.get("reason", "")
            root_cause = task_result.get("root_cause", {})
            if isinstance(root_cause, dict) and root_cause.get("corrective_action"):
                rejection_feedback += f" Corrective action: {root_cause['corrective_action']}"
        try:
            from vetinari.llm_helpers import generate_retry_brief

            retry_brief = generate_retry_brief(
                error_description=rejection_feedback or "Task output rejected",
                inspector_feedback=rejection_feedback,
            )
        except Exception:
            logger.warning("LLM retry brief unavailable - using raw rejection feedback without briefing")
            retry_brief = ""
        return f"{rejection_feedback}\n\nRETRY BRIEF:\n{retry_brief}" if retry_brief else rejection_feedback

    def _reset_for_same_agent_retry(self, node: Any, task_id: str, task_result: Any, decision: ReworkDecision) -> None:
        """Prepare a task node for same-agent or research-then-retry re-execution."""
        node.status = StatusEnum.PENDING
        combined_feedback = self._build_rejection_feedback(task_result)
        if combined_feedback:
            node.input_data["rework_feedback"] = combined_feedback
            node.description = (
                f"{node.description}\n\nPREVIOUS ATTEMPT FAILED - apply this feedback:\n{combined_feedback}"
            )
        node.error = ""
        if decision == ReworkDecision.RESEARCH_THEN_RETRY:
            node.input_data["rework_hint"] = "research_context_before_retry"
        logger.info(
            "[Rework] Task %s reset for retry (decision=%s, feedback=%s)",
            task_id,
            decision.value,
            bool(combined_feedback),
        )

    def _reset_for_model_retry(self, owner: _PipelineReworkOwner, node: Any, task_id: str) -> None:
        """Prepare a task node for retry with a different model."""
        node.status = StatusEnum.PENDING
        node.error = ""
        current_model = node.input_data.get("assigned_model", "default")
        node.input_data["excluded_models"] = [current_model]
        new_model = owner._route_model_for_task(node)
        if new_model == current_model:
            router = getattr(owner, "model_router", None)
            models = list(getattr(router, "models", {}) or {})
            new_model = next((model for model in models if model != current_model), current_model)
        if new_model == current_model:
            node.status = StatusEnum.BLOCKED
            node.error = f"Different-model retry requested but no alternate model is available for {current_model}"
            node.input_data["rework_blocked_reason"] = node.error
            logger.warning("[Rework] Task %s blocked: %s", task_id, node.error)
            return
        node.input_data["assigned_model"] = new_model
        node.input_data["rework_feedback"] = f"Previous model ({current_model}) produced rejected output"
        logger.info("[Rework] Task %s reassigned from %s to %s", task_id, current_model, new_model)
        self._audit_model_swap(task_id, current_model, new_model)

    @staticmethod
    def _audit_model_swap(task_id: str, current_model: str, new_model: str) -> None:
        """Log model-swap decisions to the optional audit trail."""
        try:
            from vetinari.audit import get_audit_logger

            get_audit_logger().log_decision(
                decision_type="model_swap",
                choice=str(new_model),
                reasoning=f"Switched from {current_model} after quality rejection on task {task_id}",
                alternatives=[current_model],
                context={"task_id": task_id, "previous_model": current_model},
            )
        except Exception:
            logger.warning("Audit logging failed during model swap", exc_info=True)

    @staticmethod
    def _reset_for_replan(node: Any, task_id: str, decision: ReworkDecision) -> None:
        """Prepare a task node for replanning-style re-execution."""
        logger.info("[Rework] Replanning for task %s (decision=%s)", task_id, decision.value)
        node.status = StatusEnum.PENDING
        node.error = ""
        if decision == ReworkDecision.REPLAN_WIDER_SCOPE:
            node.input_data["rework_hint"] = "widen_scope"

    def _prepare_rework_node(
        self,
        decision: ReworkDecision,
        owner: _PipelineReworkOwner,
        node: Any,
        task_id: str,
        task_result: Any,
    ) -> None:
        """Apply the requested rework decision to a graph node."""
        if decision in (ReworkDecision.RETRY_SAME_AGENT, ReworkDecision.RESEARCH_THEN_RETRY):
            self._reset_for_same_agent_retry(node, task_id, task_result, decision)
        elif decision == ReworkDecision.RETRY_DIFFERENT_MODEL:
            self._reset_for_model_retry(owner, node, task_id)
        elif decision in (ReworkDecision.REPLAN, ReworkDecision.REPLAN_WIDER_SCOPE):
            self._reset_for_replan(node, task_id, decision)

    def _execute_rework_decision(
        self,
        decision: ReworkDecision,
        task_id: str,
        task_result: Any,
        graph: Any,
        task_handler: Any | None = None,
    ) -> dict[str, Any] | None:
        """Execute a concrete recovery action for a failed task."""
        node = graph.nodes.get(task_id)
        if node is None:
            logger.warning("[Rework] Task %s not found in graph - skipping", task_id)
            return None
        if decision == ReworkDecision.ESCALATE_TO_USER:
            logger.info("[Rework] Task %s escalated to user - no automatic action", task_id)
            return {"action": "escalate", "task_id": task_id, "outcome": "awaiting_user"}

        owner = cast(_PipelineReworkOwner, self)
        self._prepare_rework_node(decision, owner, node, task_id, task_result)
        if node.status == StatusEnum.BLOCKED:
            return {"action": decision.value, "task_id": task_id, "outcome": "blocked", "reason": node.error}
        handler = task_handler or owner.execution_engine._task_handlers.get(node.task_type)
        handler = handler or owner.execution_engine._task_handlers.get("default")
        if handler is None:
            logger.warning("[Rework] No handler available for task %s - cannot re-execute", task_id)
            return {"action": decision.value, "task_id": task_id, "outcome": "no_handler"}

        result = owner.execution_engine._execute_task(graph, node)
        owner.execution_engine._save_checkpoint(graph.plan_id, graph)
        outcome = result.get("status", "unknown")
        logger.info("[Rework] Task %s rework complete: action=%s, outcome=%s", task_id, decision.value, outcome)
        return {"action": decision.value, "task_id": task_id, "outcome": outcome}
