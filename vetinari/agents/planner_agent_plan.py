"""Plan-generation mixin for the ForemanAgent compatibility class."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import AgentResult, AgentTask, ExecutionPlan, Plan, Task


def _planner_module() -> Any:
    """Return the public planner_agent module for compatibility-patched symbols."""
    from vetinari.agents import planner_agent

    return planner_agent


class ForemanPlanMixin:
    """Plan-mode behavior for ForemanAgent."""

    if TYPE_CHECKING:
        _max_tasks: Any
        _non_goal_store: Any
        _plan_cache: Any
        _plan_reviewer: Any

    def _execute_plan(self, task: AgentTask) -> AgentResult:
        """Generate a plan from the goal, with cache lookup and storage.

        Args:
            task: The agent task carrying the goal prompt and context.

        Returns:
            An AgentResult whose output is the plan dictionary and whose
            metadata includes cache and dispatch-gate details.
        """
        planner_agent = _planner_module()
        goal = task.prompt or task.description
        context = task.context

        cached = self._plan_cache.find_similar(goal)
        if cached is not None:
            return self._cached_plan_result(cached, goal, planner_agent)

        plan = self._generate_plan(goal, context)

        if self._plan_reviewer is not None:
            review_result = self._review_dispatch_gate(plan, goal, context, planner_agent)
            if review_result is not None:
                return review_result

        self._plan_cache.store(goal, plan.to_dict())
        planner_agent.logger.debug(
            "Stored new approved plan in cache (goal_hash=%s)", self._plan_cache._goal_hash(goal)
        )

        return AgentResult(
            success=True,
            output=plan.to_dict(),
            metadata={
                "plan_id": plan.plan_id,
                "task_count": len(plan.tasks),
                "goal": goal,
                "from_cache": False,
            },
        )

    @staticmethod
    def _cached_plan_result(cached: Any, goal: str, planner_agent: Any) -> AgentResult:
        planner_agent.logger.info(
            "Plan cache hit for goal (hash=%s, hit_count=%d) - skipping LLM decomposition",
            cached.goal_hash,
            cached.hit_count,
        )
        return AgentResult(
            success=True,
            output=cached.plan_data,
            metadata={
                "plan_id": cached.plan_data.get("plan_id"),
                "task_count": len(cached.plan_data.get("tasks", [])),
                "goal": goal,
                "from_cache": True,
                "cache_hit_count": cached.hit_count,
            },
        )

    @staticmethod
    def _execution_plan_for_review(plan: Plan, goal: str) -> ExecutionPlan:
        return ExecutionPlan(
            plan_id=plan.plan_id,
            goal=goal,
            tasks=[
                Task(
                    id=str(getattr(t, "id", getattr(t, "subtask_id", f"t{i}"))),
                    description=str(getattr(t, "description", "")),
                    outputs=list(getattr(t, "outputs", [])),
                    metadata=dict(getattr(t, "metadata", {})),
                )
                for i, t in enumerate(plan.tasks)
            ],
            notes=str(getattr(plan, "notes", "")),
        )

    def _review_dispatch_gate(
        self,
        plan: Plan,
        goal: str,
        context: dict[str, Any],
        planner_agent: Any,
    ) -> AgentResult | None:
        project_id = str(context.get("project_id", plan.plan_id))
        exec_plan = self._execution_plan_for_review(plan, goal)
        non_goal_outcome = planner_agent.check_non_goals(exec_plan, project_id, store=self._non_goal_store)
        if non_goal_outcome is not None:
            return self._non_goal_refusal_result(non_goal_outcome, plan, goal, project_id, planner_agent)

        plan_text = str(exec_plan.to_dict() if hasattr(exec_plan, "to_dict") else exec_plan)
        review_outcome = self._plan_reviewer.review(plan_text)
        gate = planner_agent.evaluate_dispatch_gate(exec_plan, review_outcome)
        if gate.dispatched:
            return None
        planner_agent.logger.warning("Dispatch gate blocked plan for project %s: %s", project_id, gate.reason)
        return AgentResult(
            success=False,
            output={},
            errors=[gate.reason],
            metadata={
                "plan_id": plan.plan_id,
                "goal": goal,
                "dispatch_refused": True,
                "refusal_reason": gate.reason,
            },
        )

    @staticmethod
    def _non_goal_refusal_result(
        non_goal_outcome: Any,
        plan: Plan,
        goal: str,
        project_id: str,
        planner_agent: Any,
    ) -> AgentResult:
        from vetinari.planning.review_outcome import PlanDecision as _PD

        decision_val = non_goal_outcome.decision
        is_refuse = decision_val is _PD.REFUSE or decision_val == _PD.REFUSE
        is_refuse = is_refuse or getattr(decision_val, "value", decision_val) == "REFUSE"
        gate_reason = f"non-goal matched: {', '.join(non_goal_outcome.citations)}"
        planner_agent.logger.warning("Plan blocked by non-goal check for project %s: %s", project_id, gate_reason)
        return AgentResult(
            success=False,
            output={},
            errors=[gate_reason],
            metadata={
                "plan_id": plan.plan_id,
                "goal": goal,
                "dispatch_refused": True,
                "refusal_reason": gate_reason,
                "hard_refuse": is_refuse,
            },
        )

    @staticmethod
    def _decompose_goal_keyword(goal: str, context: dict[str, Any] | None = None) -> list:
        """Decompose a goal into tasks using keyword heuristics.

        Args:
            goal: The user goal string to decompose.
            context: Ignored; accepted for API symmetry with LLM decomposition.

        Returns:
            List of Task objects built from keyword pattern matching.
        """
        del context
        return _planner_module().decompose_goal_keyword(goal)

    def _decompose_goal_llm(self, goal: str, context: dict[str, Any], max_tasks: int | None = None) -> list:
        """Decompose a goal into tasks via the planner_decompose helper.

        Args:
            goal: The user goal string to decompose.
            context: Optional context dictionary.
            max_tasks: Maximum tasks to request. Defaults to ``self._max_tasks``.

        Returns:
            List of Task objects with dependencies and depth pre-computed.
        """
        return _planner_module().decompose_goal_llm(
            self,
            goal,
            context,
            max_tasks if max_tasks is not None else self._max_tasks,
        )

    def _generate_plan(self, goal: str, context: dict[str, Any]) -> Plan:
        """Generate a plan from the goal using LLM-powered decomposition.

        Args:
            goal: The user goal string.
            context: Context dictionary passed through to decomposition helpers.

        Returns:
            Plan with tasks and DAG structure populated.
        """
        planner_agent = _planner_module()
        plan = Plan.create_new(goal)

        if planner_agent.is_vague_goal(goal):
            plan.needs_context = True
            plan.follow_up_question = "Could you provide more details about what you want to build?"
            return plan

        tasks = planner_agent.decompose_goal_llm(self, goal, context, self._max_tasks)
        if not tasks:
            tasks = planner_agent.decompose_goal_keyword(goal)

        plan.tasks = tasks
        if len(tasks) > self._max_tasks:
            plan.warnings.append(f"Generated {len(tasks)} tasks - consider breaking into smaller goals")

        return plan
