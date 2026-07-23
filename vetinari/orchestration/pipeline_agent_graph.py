"""AgentGraph execution backend for dependency-aware pipeline dispatch."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol, cast

from vetinari.orchestration.execution_graph import ExecutionGraph
from vetinari.orchestration.pipeline_rework import ReworkDecision
from vetinari.privacy import PRIVACY_ENVELOPE_KEY
from vetinari.privacy.envelope import PrivacyClass, privacy_receipt
from vetinari.security.redaction import redact_value
from vetinari.types import AgentType, StatusEnum

logger = logging.getLogger(__name__)
AGENT_GRAPH_RESULT_PRIVACY_SOURCE = "orchestration.pipeline_agent_graph.result"
AGENT_GRAPH_RESULT_PRIVACY_RETENTION_DAYS = 30


class _AgentGraphVariantConfigLike(Protocol):
    """Variant configuration fields used by this mixin."""

    max_planning_depth: int


class _AgentGraphVariantManagerLike(Protocol):
    """Variant manager operations used by this mixin."""

    def get_config(self) -> _AgentGraphVariantConfigLike:
        """Return the active variant configuration."""


class _PlanGeneratorLike(Protocol):
    """Plan generator operations used by this mixin."""

    def generate_plan(
        self, goal: str, constraints: dict[str, Any] | None = None, max_depth: int = 10
    ) -> ExecutionGraph:
        """Generate an execution graph for a goal.

        Args:
            goal: Goal value consumed by generate_plan().
            constraints: Constraints value consumed by generate_plan().
            max_depth: Max depth value consumed by generate_plan().
        """


class _AgentGraphExecutionEngineLike(Protocol):
    """Execution engine operations used by this mixin."""

    def execute_plan(self, graph: ExecutionGraph, task_handler: Callable[..., Any] | None = None) -> dict[str, Any]:
        """Execute an execution graph.

        Args:
            graph: Graph value consumed by execute_plan().
            task_handler: Task handler value consumed by execute_plan().
        """


class _PipelineAgentGraphOwner(Protocol):
    """Host contract required by PipelineAgentGraphRunner."""

    plan_generator: _PlanGeneratorLike
    _variant_manager: _AgentGraphVariantManagerLike
    execution_engine: _AgentGraphExecutionEngineLike

    def generate_and_execute(
        self,
        goal: str,
        constraints: dict[str, Any] | None = None,
        task_handler: Callable[..., Any] | None = None,
        context: dict[str, Any] | None = None,
        project_id: str | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Run the full pipeline for one goal.

        Args:
            goal: Goal value consumed by generate_and_execute().
            constraints: Constraints value consumed by generate_and_execute().
            task_handler: Task handler value consumed by generate_and_execute().
            context: Context value consumed by generate_and_execute().
            project_id: Project identifier that scopes the operation.
            model_id: Model identifier used for routing or lookup.
        """

    def _handle_quality_rejection(self, task_id: str, result: dict[str, Any], rework_count: int) -> ReworkDecision:
        """Choose a rework decision for a rejected task."""

    def _execute_rework_decision(
        self,
        decision: ReworkDecision,
        task_id: str,
        task_result: Any,
        graph: ExecutionGraph,
        task_handler: Any | None = None,
    ) -> dict[str, Any] | None:
        """Execute a rework decision."""

    def _make_default_handler(self) -> Callable[..., Any]:
        """Create the default durable-execution task handler."""


class PipelineAgentGraphRunner:
    """AgentGraph execution backend for the pipeline."""

    @staticmethod
    def _classify_agent_graph_intake(goal: str, context: dict[str, Any]) -> tuple[Any | None, Any | None]:
        """Populate AgentGraph context with intake classification."""
        try:
            from vetinari.orchestration.intake import get_request_intake

            tier, features = get_request_intake().classify_with_features(goal, context)
            context["intake_tier"] = tier.value
            context["intake_confidence"] = features.confidence
            context["intake_pattern_key"] = features.pattern_key
            logger.info("[TwoLayer] AgentGraph intake: tier=%s, confidence=%.2f", tier.value, features.confidence)
            return tier, features
        except Exception:
            logger.warning("Intake classification unavailable for AgentGraph path")
            return None, None

    @staticmethod
    def _build_agent_graph_request_spec(goal: str, context: dict[str, Any]) -> None:
        """Build RequestSpec metadata for AgentGraph execution."""
        try:
            from vetinari.orchestration.intake import Tier as SpecTier
            from vetinari.orchestration.request_spec import get_spec_builder

            spec = get_spec_builder().build(
                goal=goal,
                tier=SpecTier(context.get("intake_tier", "standard")),
                category=context.get("category", "code"),
            )
            context["request_spec"] = spec.to_dict()
            logger.info("[TwoLayer] AgentGraph spec: complexity=%d", spec.estimated_complexity)
        except Exception:
            logger.warning("RequestSpec unavailable for AgentGraph path")

    @staticmethod
    def _route_agent_graph_complexity(goal: str, context: dict[str, Any]) -> None:
        """Apply complexity router metadata to AgentGraph context."""
        try:
            from vetinari.routing import route_by_complexity

            routing = route_by_complexity(goal)
            context["routing_decision"] = routing.to_dict()
            context["complexity"] = routing.complexity.value
            context["skip_stages"] = routing.skip_stages
            context["add_stages"] = routing.add_stages
            logger.info(
                "[TwoLayer] ComplexityRouter: %s - skip=%s, add=%s",
                routing.complexity.value,
                routing.skip_stages,
                routing.add_stages,
            )
        except Exception:
            logger.warning("ComplexityRouter unavailable for AgentGraph path")

    def _prepare_agent_graph_context(self, goal: str, context: dict[str, Any]) -> tuple[Any | None, Any | None]:
        """Run optional pre-planning context enrichers."""
        tier, features = self._classify_agent_graph_intake(goal, context)
        self._build_agent_graph_request_spec(goal, context)
        self._route_agent_graph_complexity(goal, context)
        return tier, features

    @staticmethod
    def _generate_agent_graph_execution_graph(
        owner: _PipelineAgentGraphOwner,
        goal: str,
        constraints: dict[str, Any] | None,
    ) -> ExecutionGraph:
        """Generate the ExecutionGraph used to build a contracts.Plan."""
        return owner.plan_generator.generate_plan(
            goal,
            constraints,
            max_depth=owner._variant_manager.get_config().max_planning_depth,
        )

    @staticmethod
    def _pause_for_follow_up_if_needed(
        graph: ExecutionGraph, goal: str, context: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Return a pause payload when PlanGenerator asks for clarification."""
        if not (hasattr(graph, "follow_up_question") and graph.follow_up_question):
            return None
        try:
            from vetinari.orchestration.intake import PipelinePaused

            paused = PipelinePaused(
                questions=[graph.follow_up_question],
                pipeline_state={"goal": goal, "plan_id": graph.plan_id},
                tier=context.get("intake_tier", "standard"),
                goal=goal,
            )
            logger.info("[TwoLayer] Plan has follow_up_question - pausing pipeline")
            return cast(dict[str, Any], paused.to_dict())
        except Exception:
            logger.warning("follow_up_question pause failed, proceeding")
            return None

    def _contracts_task_from_node(self, node: Any, *, include_manifest: bool = True):
        """Convert one ExecutionGraph node to an AgentGraph contract task."""
        from vetinari.agents.contracts import Task as ContractsTask

        agent_type_str = node.input_data.get("assigned_agent", AgentType.WORKER.value).upper()
        try:
            agent_type = AgentType[agent_type_str]
        except KeyError:
            agent_type = AgentType.WORKER
        task = ContractsTask(
            id=node.id,
            description=node.description,
            assigned_agent=agent_type,
            dependencies=list(node.depends_on),
            inputs=list(node.input_data.keys()) if node.input_data else [],
            outputs=[],
        )
        if include_manifest:
            self._attach_task_manifest(task, node, agent_type_str)
        return task

    @staticmethod
    def _attach_task_manifest(task: Any, node: Any, agent_type_str: str) -> None:
        """Attach optional task manifest metadata."""
        try:
            from vetinari.orchestration.task_manifest import get_manifest_builder

            manifest = get_manifest_builder().build(
                task_description=node.description,
                agent_type=agent_type_str,
                mode=node.input_data.get("mode", "build"),
            )
            task.metadata["manifest"] = manifest.to_dict()
        except Exception as exc:
            logger.warning("Failed to build manifest for task %s: %s", node.id, exc)

    def _convert_execution_graph_to_plan(self, graph: ExecutionGraph, goal: str, include_manifest: bool = True):
        """Convert ExecutionGraph nodes into an AgentGraph contracts.Plan."""
        from vetinari.agents.contracts import Plan as ExecutionPlan

        plan = ExecutionPlan.create_new(goal)
        for node in graph.nodes.values():
            task = self._contracts_task_from_node(node, include_manifest=include_manifest)
            plan.tasks.append(task)
        return plan

    @staticmethod
    def _apply_complexity_stage_filter(plan: Any, context: dict[str, Any]) -> None:
        """Remove tasks matching stages the complexity router says to skip."""
        skip = set(context.get("skip_stages", []))
        if not skip:
            return
        before = len(plan.tasks)
        plan.tasks = [
            task
            for task in plan.tasks
            if not any(
                stage in (task.metadata.get("manifest", {}).get("mode", "") or task.description.lower())
                for stage in skip
            )
        ]
        if before != len(plan.tasks):
            logger.info("[TwoLayer] ComplexityRouter skipped %d stages: %s", before - len(plan.tasks), skip)

    @staticmethod
    def _handle_agent_graph_rework(
        owner: _PipelineAgentGraphOwner,
        graph: ExecutionGraph,
        results: dict[str, Any],
    ) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
        """Route failed AgentGraph tasks through RCA-driven rework."""
        rework_decisions: dict[str, str] = {}
        rework_outcomes: dict[str, dict[str, Any]] = {}
        for task_id, result in results.items():
            if result.success or task_id.startswith("_"):
                continue
            root_cause = result.metadata.get("root_cause") if result.metadata else None
            if not root_cause:
                continue
            rework_count = result.metadata.get("rework_count", 0) if result.metadata else 0
            decision = owner._handle_quality_rejection(task_id, result.metadata, rework_count)
            rework_decisions[task_id] = decision.value
            logger.info("[TwoLayer] AgentGraph task %s failed - RCA routing: %s", task_id, decision.value)
            rework_result = owner._execute_rework_decision(decision, task_id, result.metadata, graph)
            if rework_result:
                rework_outcomes[task_id] = rework_result
                if rework_result.get("outcome") == StatusEnum.COMPLETED.value and hasattr(results[task_id], "_replace"):
                    results[task_id] = results[task_id]._replace(success=True)
        return rework_decisions, rework_outcomes

    @staticmethod
    def _build_agent_graph_result(
        graph: ExecutionGraph,
        goal: str,
        results: dict[str, Any],
        rework_decisions: dict[str, str],
        rework_outcomes: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Build the public AgentGraph result payload."""
        payload: dict[str, Any] = {
            "plan_id": graph.plan_id,
            "goal": goal,
            "backend": "agent_graph",
            "completed": sum(1 for result in results.values() if result.success),
            "failed": sum(1 for result in results.values() if not result.success),
            "outputs": {task_id: redact_value(result.output) for task_id, result in results.items()},
            "errors": {task_id: redact_value(result.errors) for task_id, result in results.items() if result.errors},
            PRIVACY_ENVELOPE_KEY: privacy_receipt(
                privacy_class=PrivacyClass.OPERATIONAL.value,
                retention_days=AGENT_GRAPH_RESULT_PRIVACY_RETENTION_DAYS,
                source=AGENT_GRAPH_RESULT_PRIVACY_SOURCE,
                redaction_applied=True,
            ),
        }
        if rework_decisions:
            payload["rework_decisions"] = rework_decisions
        if rework_outcomes:
            payload["rework_outcomes"] = redact_value(rework_outcomes)
        return payload

    @staticmethod
    def _record_agent_graph_outcome(result: dict[str, Any], tier: Any | None, features: Any | None) -> None:
        """Record Thompson Sampling tier outcome for AgentGraph execution."""
        if tier is None or features is None:
            return
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            completed = result[StatusEnum.COMPLETED.value]
            failed = result[StatusEnum.FAILED.value]
            quality = completed / max(completed + failed, 1)
            get_thompson_selector().update_tier(
                pattern_key=features.pattern_key,
                tier_used=tier.value,
                quality_score=quality,
                rework_count=0,
            )
            logger.info("[TwoLayer] AgentGraph Thompson outcome: tier=%s, quality=%.2f", tier.value, quality)
        except Exception:
            logger.warning("Thompson tier outcome recording failed in AgentGraph path", exc_info=True)

    def execute_with_agent_graph(
        self,
        goal: str,
        constraints: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a goal using AgentGraph as the execution backend.

        Args:
            goal: Goal value consumed by execute_with_agent_graph().
            constraints: Constraints value consumed by execute_with_agent_graph().
            context: Context value consumed by execute_with_agent_graph().

        Returns:
            Value produced for the caller.
        """
        owner = cast(_PipelineAgentGraphOwner, self)
        try:
            from vetinari.orchestration.agent_graph import get_agent_graph

            context = context or {}
            tier, features = self._prepare_agent_graph_context(goal, context)
            graph = self._generate_agent_graph_execution_graph(owner, goal, constraints)
            pause_result = self._pause_for_follow_up_if_needed(graph, goal, context)
            if pause_result is not None:
                return pause_result
            plan = self._convert_execution_graph_to_plan(graph, goal)
            self._apply_complexity_stage_filter(plan, context)
            results = get_agent_graph().execute_plan(plan)
            rework_decisions, rework_outcomes = self._handle_agent_graph_rework(owner, graph, results)
            agent_graph_result = self._build_agent_graph_result(graph, goal, results, rework_decisions, rework_outcomes)
            self._record_agent_graph_outcome(agent_graph_result, tier, features)
            return agent_graph_result
        except Exception as exc:
            logger.warning("[TwoLayer] AgentGraph execution failed, falling back: %s", exc)
            return owner.generate_and_execute(goal, constraints, context=context)

    @staticmethod
    def _should_use_agent_graph(graph: Any, task_handler: Any) -> bool:
        """Return whether Stage 5 should use AgentGraph rather than durable handlers."""
        return len(graph.nodes) >= 2 and task_handler is None

    @staticmethod
    def _agent_graph_results_to_durable_shape(graph: Any, ag_results: dict[str, Any]) -> dict[str, Any]:
        """Convert AgentGraph results to DurableExecutionEngine result shape."""
        completed = sum(1 for result in ag_results.values() if result.success)
        failed = sum(1 for result in ag_results.values() if not result.success)
        return {
            "plan_id": graph.plan_id,
            "total_tasks": len(graph.nodes),
            StatusEnum.COMPLETED.value: completed,
            StatusEnum.FAILED.value: failed,
            "task_results": {
                task_id: {
                    "output": redact_value(result.output),
                    "status": StatusEnum.COMPLETED.value if result.success else StatusEnum.FAILED.value,
                    "errors": redact_value(result.errors),
                    "metadata": redact_value(result.metadata),
                }
                for task_id, result in ag_results.items()
            },
            "backend": "agent_graph",
            PRIVACY_ENVELOPE_KEY: privacy_receipt(
                privacy_class=PrivacyClass.OPERATIONAL.value,
                retention_days=AGENT_GRAPH_RESULT_PRIVACY_RETENTION_DAYS,
                source=AGENT_GRAPH_RESULT_PRIVACY_SOURCE,
                redaction_applied=True,
            ),
        }

    def _execute_via_agent_graph_or_fallback(
        self,
        graph: Any,
        task_handler: Any,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Route Stage 5 execution through AgentGraph or DurableExecutionEngine."""
        owner = cast(_PipelineAgentGraphOwner, self)
        if self._should_use_agent_graph(graph, task_handler):
            try:
                from vetinari.orchestration.agent_graph import get_agent_graph

                plan = self._convert_execution_graph_to_plan(graph, context.get("goal", ""), include_manifest=False)
                return self._agent_graph_results_to_durable_shape(graph, get_agent_graph().execute_plan(plan))
            except Exception as exc:
                logger.warning("[Pipeline] AgentGraph unavailable for parallel execution, falling back: %s", exc)

        effective_handler = task_handler or owner._make_default_handler()
        return owner.execution_engine.execute_plan(graph, effective_handler)
