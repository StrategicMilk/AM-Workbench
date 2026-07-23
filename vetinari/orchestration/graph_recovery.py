"""Error recovery, delegation, and dynamic graph modification for AgentGraph."""

from __future__ import annotations

import logging
from typing import Any

from vetinari.agents.contracts import AgentResult, AgentTask, Task
from vetinari.constants import TRUNCATE_OUTPUT_PREVIEW, TRUNCATE_OUTPUT_SUMMARY
from vetinari.orchestration.graph_types import TaskNode
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


class GraphRecoveryEngine:
    """Error handling and recovery methods for AgentGraph."""

    _MAKER_CHECKER_MAX_ITERATIONS = 3

    @staticmethod
    def _get_maker_checker_condenser() -> Any | None:
        """Return optional context condenser for maker-checker handoffs."""
        try:
            from vetinari.context import get_context_condenser

            return get_context_condenser()
        except Exception:
            logger.warning("Exception handled by  get maker checker condenser fallback", exc_info=True)
            return None

    @staticmethod
    def _condense_maker_output(condenser: Any | None, current_result: AgentResult) -> str:
        """Condense Worker output for Inspector review."""
        if condenser is not None:
            return condenser.condense_for_handoff(
                AgentType.WORKER.value,
                AgentType.INSPECTOR.value,
                current_result.output,
                current_result.metadata,
            )
        return str(current_result.output)[:TRUNCATE_OUTPUT_PREVIEW]

    @staticmethod
    def _build_review_task(
        task: Task,
        current_result: AgentResult,
        condensed_output: str,
        iteration: int,
    ) -> AgentTask:
        """Build the Inspector review task for one maker-checker iteration."""
        review_prompt = (
            f"Review the following output from WORKER task '{task.id}':\n\n"
            f"{condensed_output}\n\nOriginal task: {task.description}"
        )
        metadata = current_result.metadata or {}
        return AgentTask(
            task_id=f"{task.id}_review_{iteration}",
            agent_type=AgentType.INSPECTOR,
            description=review_prompt,
            prompt=review_prompt,
            context={
                "review_type": "code_review",
                "original_task_id": task.id,
                "iteration": iteration,
                "self_check_passed": metadata.get("self_check_passed"),
                "self_check_issues": metadata.get("self_check_issues", []),
                "schema_valid": metadata.get("schema_valid"),
            },
        )

    @staticmethod
    def _approve_maker_checker_result(
        task: Task,
        current_result: AgentResult,
        review_verification: Any,
        iteration: int,
    ) -> AgentResult:
        """Mark a maker-checker result as approved and return it."""
        logger.info("[AgentGraph] Maker-checker: INSPECTOR approved %s on iteration %s", task.id, iteration + 1)
        if current_result.metadata is None:
            current_result.metadata = {}
        current_result.metadata["maker_checker"] = {
            "approved": True,
            "iterations": iteration + 1,
            "review_score": getattr(review_verification, "score", None),
        }
        return current_result

    @staticmethod
    def _review_issues_text(review_verification: Any) -> str:
        """Format Inspector verification issues for Worker rework."""
        return "; ".join(
            issue.get("message", str(issue)) if isinstance(issue, dict) else str(issue)
            for issue in review_verification.issues
        )

    @staticmethod
    def _build_fix_task(
        task: Task,
        current_result: AgentResult,
        review_result: AgentResult,
        issues_text: str,
        condenser: Any | None,
        iteration: int,
    ) -> AgentTask:
        """Build a Worker rework task from Inspector feedback."""
        if condenser is not None:
            rework_context = condenser.condense_for_handoff(
                AgentType.INSPECTOR.value,
                AgentType.WORKER.value,
                review_result.output,
                review_result.metadata,
            )
        else:
            rework_context = issues_text
        fix_prompt = (
            f"{task.description}\n\n"
            "[MAKER-CHECKER FEEDBACK] Previous output was rejected by INSPECTOR review.\n"
            f"{rework_context}\nPlease fix these issues."
        )
        return AgentTask(
            task_id=f"{task.id}_fix_{iteration}",
            agent_type=AgentType.WORKER,
            description=fix_prompt,
            prompt=fix_prompt,
            context={
                "original_output": str(current_result.output)[:TRUNCATE_OUTPUT_SUMMARY],
                "review_issues": issues_text,
                "iteration": iteration + 1,
            },
        )

    def _mark_maker_checker_exhausted(self, current_result: AgentResult) -> AgentResult:
        """Mark the final maker-checker result as unapproved after retries."""
        if current_result.metadata is None:
            current_result.metadata = {}
        current_result.metadata["maker_checker"] = {
            "approved": False,
            "iterations": self._MAKER_CHECKER_MAX_ITERATIONS,
        }
        return current_result

    def _apply_maker_checker(self, task: Task, result: AgentResult) -> AgentResult:
        """Run maker-checker loop: INSPECTOR reviews WORKER output."""
        quality_agent = self._agents.get(AgentType.INSPECTOR)
        builder_agent = self._agents.get(AgentType.WORKER)
        if quality_agent is None or builder_agent is None:
            return result

        current_result = result
        condenser = self._get_maker_checker_condenser()
        for iteration in range(self._MAKER_CHECKER_MAX_ITERATIONS):
            condensed_output = self._condense_maker_output(condenser, current_result)
            review_task = self._build_review_task(task, current_result, condensed_output, iteration)
            try:
                review_result = quality_agent.execute(review_task)
                review_verification = quality_agent.verify(review_result.output)
                if review_result.success and review_verification.passed:
                    return self._approve_maker_checker_result(task, current_result, review_verification, iteration)

                issues_text = self._review_issues_text(review_verification)
                logger.warning(
                    "[AgentGraph] Maker-checker: INSPECTOR rejected %s (iteration %d): %s",
                    task.id,
                    iteration + 1,
                    issues_text,
                )
                if iteration < self._MAKER_CHECKER_MAX_ITERATIONS - 1:
                    fix_task = self._build_fix_task(
                        task, current_result, review_result, issues_text, condenser, iteration
                    )
                    current_result = builder_agent.execute(fix_task)
                    if not current_result.success:
                        break
            except Exception as exc:
                logger.warning("[AgentGraph] Maker-checker iteration failed: %s", exc)
                break
        return self._mark_maker_checker_exhausted(current_result)

    @staticmethod
    def _verification_issues_text(verification: Any) -> str:
        """Format verification issues from a failed task."""
        return "; ".join(
            issue.get("message", str(issue)) if isinstance(issue, dict) else str(issue) for issue in verification.issues
        )

    def _try_redecompose_failed_task(self, task: Task, issues_text: str) -> AgentResult | None:
        """Ask FOREMAN to re-decompose a failed task when available."""
        if AgentType.FOREMAN not in self._agents:
            return None
        try:
            planner = self._agents[AgentType.FOREMAN]
            replan_task = AgentTask.from_task(
                task,
                f"Re-decompose failed task '{task.id}' into smaller subtasks. Original error: {issues_text}",
            )
            replan_task.context["mode"] = "extract"
            replan_result = planner.execute(replan_task)
            if replan_result.success:
                logger.info("[AgentGraph] Planner re-decomposed failed task %s", task.id)
                return replan_result
        except Exception as exc:
            logger.warning("[AgentGraph] Planner re-decomposition failed: %s", exc)
        return None

    def _run_error_recovery(self, task: Task, failed_result: AgentResult, verification: Any) -> AgentResult:
        """Delegate a failed task to the Worker for error analysis and recovery."""
        try:
            recovery_agent = self._agents[AgentType.WORKER]
            issues_text = self._verification_issues_text(verification)
            recovery_task = AgentTask.from_task(
                task, f"Analyse and recover from failure in task '{task.id}': {issues_text}"
            )
            recovery_task.context["original_output"] = str(failed_result.output)[:TRUNCATE_OUTPUT_SUMMARY]
            recovery_task.context["verification_issues"] = issues_text
            recovery_result = recovery_agent.execute(recovery_task)
            if not recovery_result.success and issues_text:
                logger.warning("[AgentGraph] Error recovery failed for task %s: %s", task.id, issues_text[:200])
                if issues_text not in " ".join(recovery_result.errors):
                    recovery_result.errors.append(f"[verification_issues] {issues_text}")
            replan_result = (
                self._try_redecompose_failed_task(task, issues_text) if not recovery_result.success else None
            )
            return replan_result or recovery_result
        except Exception as exc:
            logger.warning("[AgentGraph] Error recovery delegation failed: %s", exc)
            return AgentResult(success=False, output=failed_result.output, errors=[f"Recovery failed: {exc}"])

    def _find_delegate(self, task: Task, exclude: AgentType | None = None) -> AgentType | None:
        """Find the best available agent to handle a delegated task."""
        candidates = []
        task_lower = task.description.lower()
        for agent_type, agent in self._agents.items():
            if agent_type == exclude:
                continue
            try:
                agent_task = AgentTask.from_task(task, task.description)
                if agent.can_handle(agent_task):
                    caps = [cap.lower() for cap in agent.get_capabilities()]
                    candidates.append((sum(1 for cap in caps if cap in task_lower), agent_type))
            except Exception:
                logger.warning(
                    "Agent %s raised during capability check for task %s", agent_type, task.id, exc_info=True
                )
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        best_score, best_type = candidates[0]
        if best_score == 0 and AgentType.FOREMAN in self._agents:
            return AgentType.FOREMAN
        return best_type

    def inject_task(self, plan_id: str, new_task: Task, after_task_id: str) -> bool:
        """Inject a new task into an in-flight execution plan.

        Args:
            plan_id: Plan id value consumed by inject_task().
            new_task: New task value consumed by inject_task().
            after_task_id: After task id value consumed by inject_task().

        Returns:
            Value produced for the caller.
        """
        plan = self._execution_plans.get(plan_id)
        if plan is None:
            logger.warning("[AgentGraph] inject_task: plan %s not found", plan_id)
            return False
        if after_task_id not in plan.nodes:
            logger.warning("[AgentGraph] inject_task: after_task %s not in plan", after_task_id)
            return False
        if new_task.id in plan.nodes:
            logger.warning("[AgentGraph] inject_task: task %s already exists", new_task.id)
            return False

        new_node = TaskNode(task=new_task, dependencies={after_task_id})
        for node in plan.nodes.values():
            if after_task_id in node.dependencies:
                node.dependencies.discard(after_task_id)
                node.dependencies.add(new_task.id)

        after_node = plan.nodes[after_task_id]
        old_dependents = after_node.dependents.copy()
        after_node.dependents = {new_task.id}
        new_node.dependents = old_dependents
        plan.nodes[new_task.id] = new_node
        plan.execution_order = self._topological_sort(plan.nodes)
        logger.info("[AgentGraph] Injected task %s after %s in plan %s", new_task.id, after_task_id, plan_id)
        return True
