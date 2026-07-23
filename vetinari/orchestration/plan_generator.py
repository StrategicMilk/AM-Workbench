"""Plan Generator — Layer 1 planning logic for the Two-Layer Orchestration System.

Generates execution graphs from goals using LLM-powered or keyword-based
task decomposition following the assembly-line pattern.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from vetinari.orchestration.execution_graph import ExecutionGraph
from vetinari.types import AgentType, PlanStatus

logger = logging.getLogger(__name__)
_BACKGROUND_EXECUTOR: ThreadPoolExecutor | None = None
_BACKGROUND_EXECUTOR_LOCK = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _BACKGROUND_EXECUTOR
    if _BACKGROUND_EXECUTOR is None:
        with _BACKGROUND_EXECUTOR_LOCK:
            if _BACKGROUND_EXECUTOR is None:
                _BACKGROUND_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="plan-gen")
    return _BACKGROUND_EXECUTOR


def shutdown_plan_generator_executor_for_test() -> None:
    """Drain and clear the plan-generator background executor.

    Wired into ``tests/_root_conftest_harness.py`` so daemon worker threads
    do not leak across test sessions. Safe to call when the executor was
    never started.
    """
    global _BACKGROUND_EXECUTOR
    with _BACKGROUND_EXECUTOR_LOCK:
        executor = _BACKGROUND_EXECUTOR
        _BACKGROUND_EXECUTOR = None
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=False)


def _log_bg_error(future: Future[Any]) -> None:
    try:
        future.result()
    except Exception:
        logger.warning("Plan-generator background task failed", exc_info=True)


def _log_decomposition_async(
    choice: str,
    reasoning: str,
    context: dict[str, Any],
) -> None:
    """Fire-and-forget audit log for plan decomposition decisions.

    Runs in a daemon thread to avoid blocking the plan generation hot path.
    Failures are logged at WARNING level.

    Args:
        choice: Short description of the decomposition method chosen.
        reasoning: Explanation of why this method was used.
        context: Additional context dict for the audit record.
    """

    def _log() -> None:
        try:
            from vetinari.audit import get_audit_logger

            get_audit_logger().log_decision(
                decision_type="plan_decomposition",
                choice=choice,
                reasoning=reasoning,
                context=context,
            )
        except Exception:
            logger.warning("Audit logging failed during decomposition", exc_info=True)

    future = _get_executor().submit(_log)
    future.add_done_callback(_log_bg_error)


class PlanGenerator:
    """Generates execution plans from goals.

    Features:
    - Multi-candidate plan generation
    - Plan scoring and selection
    - Constraint handling
    """

    def __init__(
        self,
        model_router=None,
        *,
        graph_factory: Callable[..., Any] | None = None,
    ):
        self.model_router = model_router
        self._graph_factory = graph_factory or ExecutionGraph

    def generate_plan(
        self,
        goal: str,
        constraints: dict[str, Any] | None = None,
        max_depth: int = 10,
    ) -> ExecutionGraph:
        """Generate an execution graph from a goal.

        Args:
            goal: Goal to achieve.
            constraints: Optional planning constraints.
            max_depth: Maximum task decomposition depth.

        Returns:
            ExecutionGraph with decomposed tasks.

        Raises:
            AttributeError: If the graph lacks required topology metadata.
        """
        constraints = constraints or {}
        plan_id = f"plan-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"

        graph = self._graph_factory(plan_id=plan_id, goal=goal)

        tasks = self._decompose_goal(goal, max_depth, constraints)

        for task_spec in tasks:
            graph.add_task(
                task_id=task_spec["id"],
                description=task_spec["description"],
                task_type=task_spec.get("type", "general"),
                depends_on=task_spec.get("depends_on", []),
                input_data=task_spec.get("input", {}),
            )

        if self._has_circular_dependency(graph):
            logger.error("Plan %s has circular dependencies — blocking plan", plan_id)
            graph.status = PlanStatus.FAILED
            return graph

        if not hasattr(graph, "topology_metadata"):
            raise AttributeError("ExecutionGraph.topology_metadata is required for generated plan topology metadata")

        task_dicts = [{"id": t.id, "dependencies": list(t.depends_on)} for t in graph.nodes.values()]
        graph.topology_metadata["task_dependencies"] = task_dicts

        try:
            from vetinari.routing.dag_analyzer import analyze_dag, suggest_topology

            dag_shape = analyze_dag(task_dicts)
            topology = suggest_topology(dag_shape)
            graph.topology_metadata["suggested_topology"] = topology
            graph.topology_metadata["dag_shape"] = dag_shape.to_dict()
            logger.debug("[PlanGenerator] Plan %s: topology=%s", plan_id, topology)
        except Exception as exc:
            logger.warning("[PlanGenerator] DAG analysis skipped for plan %s: %s", plan_id, exc)

        graph.status = PlanStatus.DRAFT
        return graph

    def _decompose_goal(self, goal: str, max_depth: int, constraints: dict[str, Any] | None = None) -> list[dict]:
        """Decompose a goal into tasks, using Foreman when available."""
        try:
            from vetinari.agents import get_foreman_agent
            from vetinari.agents.contracts import AgentTask

            planner = get_foreman_agent()
            planner.set_interaction_mode("auto")
            try:
                from vetinari.orchestration.variant_system import get_variant_manager as _get_vm

                _variant_max_ctx = _get_vm().get_config().max_context_tokens
                planner._max_context_tokens = _variant_max_ctx
            except Exception:
                logger.warning("Could not load VariantManager — planner will use its own default context token limit")
            _ctx: dict[str, Any] = {"max_depth": max_depth}
            if constraints:
                # Forward project metadata (category, tech_stack, etc.) to the Foreman
                for _ck in (
                    "category",
                    "tech_stack",
                    "priority",
                    "platforms",
                    "required_features",
                    "things_to_avoid",
                    "expected_outputs",
                ):
                    if _ck in constraints:
                        _ctx[_ck] = constraints[_ck]
            task = AgentTask(
                task_id="decomp-0",
                agent_type=AgentType.FOREMAN,
                description=goal,
                prompt=goal,
                context=_ctx,
            )
            result = planner.execute(task)
            if result.success and isinstance(result.output, dict) and result.output.get("tasks"):
                decomposed = [
                    {
                        "id": t.get("id", f"t{i + 1}"),
                        "description": t.get("description", "Task"),
                        "type": (
                            t.get("assigned_agent", "general").lower()
                            if isinstance(t.get("assigned_agent"), str)
                            else "general"
                        ),
                        "depends_on": t.get("dependencies", []),
                        "input": {"goal": goal, "inputs": t.get("inputs", [])},
                    }
                    for i, t in enumerate(result.output["tasks"])
                ]
                _log_decomposition_async(
                    choice=f"llm_decomposition ({len(decomposed)} tasks)",
                    reasoning=f"ForemanAgent decomposed goal into {len(decomposed)} tasks",
                    context={
                        "task_count": len(decomposed),
                        "method": "foreman_agent",
                        "goal_preview": goal[:120],
                    },
                )
                return self._assess_risk(goal, decomposed)
        except Exception as e:
            logger.warning("ForemanAgent decomposition failed: %s, using keyword fallback", e)

        fallback_tasks = self._keyword_decomposition(goal, constraints=constraints)

        _log_decomposition_async(
            choice=f"keyword_fallback ({len(fallback_tasks)} tasks)",
            reasoning="ForemanAgent unavailable, used keyword-based heuristic decomposition",
            context={
                "task_count": len(fallback_tasks),
                "method": "keyword_fallback",
                "goal_preview": goal[:120],
            },
        )

        return fallback_tasks

    @staticmethod
    def _assess_risk(goal: str, tasks: list[dict]) -> list[dict]:
        """Tag tasks with risk flags for destructive or irreversible operations."""
        _DESTRUCTIVE_KEYWORDS = {
            "high": ["delete", "drop", "destroy", "overwrite", "force push", "rm -rf", "truncate"],
            "medium": ["migrate", "deploy", "push", "upgrade", "rename", "move", "replace"],
        }
        goal_lower = goal.lower()

        for task in tasks:
            desc_lower = task["description"].lower()
            combined = f"{goal_lower} {desc_lower}"
            for level, keywords in _DESTRUCTIVE_KEYWORDS.items():
                matched = [kw for kw in keywords if kw in combined]
                if matched:
                    task.setdefault("input", {})["risk_level"] = level
                    task["input"]["risk_reason"] = f"Destructive operation detected: {', '.join(matched)}"
                    logger.info(
                        "Task %s flagged as %s risk: %s",
                        task["id"],
                        level,
                        ", ".join(matched),
                    )
                    break
        return tasks

    @staticmethod
    def _add_foundation_task(
        tasks: list[dict],
        next_id: Callable[[str], str],
        goal: str,
        tech_stack: str,
        is_code: bool,
    ) -> str | None:
        """Add an optional tech-stack foundation task."""
        if not tech_stack or not is_code:
            return None
        foundation_id = next_id("foundation-")
        tasks.append(
            {
                "id": foundation_id,
                "description": f"Set up project architecture and framework scaffolding for: {tech_stack}",
                "type": "scaffolding",
                "depends_on": [],
                "input": {"goal": goal, "tech_stack": tech_stack},
            },
        )
        logger.info("Layer 0 scaffolding task injected for tech_stack=%s", tech_stack)
        return foundation_id

    @staticmethod
    def _add_chain_task(
        tasks: list[dict],
        next_id: Callable[[str], str],
        description: str,
        task_type: str,
        depends_on: list[str],
        input_data: dict[str, Any],
    ) -> str:
        """Append one generated task and return its ID."""
        task_id = next_id("t")
        tasks.append(
            {
                "id": task_id,
                "description": description,
                "type": task_type,
                "depends_on": depends_on,
                "input": input_data,
            },
        )
        return task_id

    def _add_code_tasks(
        self,
        tasks: list[dict],
        next_id: Callable[[str], str],
        goal: str,
        goal_summary: str,
        previous: str,
    ) -> None:
        """Add implementation, testing, and verification tasks for code goals."""
        for description, task_type in (
            (f"Set up project structure and scaffolding for: {goal_summary}", "implementation"),
            (f"Implement core functionality: {goal_summary}", "implementation"),
            (f"Write and run tests for: {goal_summary}", "testing"),
            (f"Verify output quality and completeness for: {goal_summary}", "verification"),
        ):
            previous = self._add_chain_task(tasks, next_id, description, task_type, [previous], {"goal": goal})

    def _add_research_tasks(
        self,
        tasks: list[dict],
        next_id: Callable[[str], str],
        goal: str,
        goal_summary: str,
        previous: str,
    ) -> None:
        """Add research and synthesis tasks for investigation-style goals."""
        research_id = self._add_chain_task(
            tasks,
            next_id,
            f"Gather information and sources about: {goal_summary}",
            "research",
            [previous],
            {"goal": goal},
        )
        self._add_chain_task(
            tasks,
            next_id,
            f"Analyze and synthesize findings for: {goal_summary}",
            "analysis",
            [research_id],
            {"goal": goal},
        )

    def _add_review_and_docs(
        self,
        tasks: list[dict],
        next_id: Callable[[str], str],
        goal: str,
        goal_summary: str,
        include_docs: bool,
    ) -> None:
        """Add trailing review and optional documentation tasks."""
        review_id = self._add_chain_task(
            tasks,
            next_id,
            f"Review output quality and consistency for: {goal_summary}",
            "verification",
            [tasks[-1]["id"]],
            {"goal": goal},
        )
        if include_docs:
            self._add_chain_task(
                tasks,
                next_id,
                f"Create documentation and final summary for: {goal_summary}",
                "documentation",
                [review_id],
                {"goal": goal},
            )

    def _keyword_decomposition(self, goal: str, constraints: dict[str, Any] | None = None) -> list[dict]:
        """Fallback keyword-based goal decomposition."""
        constraints = constraints or {}
        tasks: list[dict] = []
        counter = [1]

        def next_id(p: str = "t") -> str:
            """Generate a unique sequential task ID with the given prefix.

            Returns:
                String of the form ``"{p}{counter}"`` where the counter
                increments globally across all calls within this decomposition.
            """
            tid = f"{p}{counter[0]}"
            counter[0] += 1
            return tid

        goal_lower = goal.lower()
        is_code = any(
            k in goal_lower
            for k in [
                "code",
                "implement",
                "build",
                "create",
                "program",
                "app",
                "web",
                "software",
            ]
        )
        is_research = any(k in goal_lower for k in ["research", "analyze", "investigate", "study", "review"])
        is_docs = any(k in goal_lower for k in ["document", "readme", "explain", "write", "report"])

        tech_stack = constraints.get("tech_stack", "")
        foundation_id = self._add_foundation_task(tasks, next_id, goal, tech_stack, is_code)

        # Stage 1: Analysis — include goal in description for worker context
        _goal_summary = goal[:120].rstrip()
        t1 = self._add_chain_task(
            tasks,
            next_id,
            f"Analyze requirements and create specification for: {_goal_summary}",
            "analysis",
            [foundation_id] if foundation_id else [],
            {"goal": goal},
        )

        if is_code:
            self._add_code_tasks(tasks, next_id, goal, _goal_summary, t1)
        elif is_research:
            self._add_research_tasks(tasks, next_id, goal, _goal_summary, t1)
        else:
            self._add_chain_task(
                tasks,
                next_id,
                f"Execute primary task: {_goal_summary}",
                "implementation",
                [t1],
                {},
            )

        self._add_review_and_docs(tasks, next_id, goal, _goal_summary, is_docs or is_code)
        return self._assess_risk(goal, tasks)

    def resolve_worker_mode(self, task_description: str) -> str | None:
        """Resolve the best Worker mode for a task using capability routing.

        Returns:
            Best matching Worker mode, or None when no capability matches.
        """
        try:
            from vetinari.skills.skill_registry import get_skill, get_skills_by_capability

            _keyword_to_capability = {
                "review": "code_review",
                "audit": "security_audit",
                "security": "security_audit",
                "test": "test_writing",
                "document": "documentation_generation",
                "refactor": "refactoring",
                "bug": "bug_diagnosis",
                "fix": "bug_diagnosis",
                "implement": "feature_implementation",
                "build": "feature_implementation",
                "research": "code_discovery",
                "explore": "code_discovery",
                "analyze": "code_discovery",
                "architecture": "architecture_review",
                "design": "architecture_review",
                "risk": "risk_assessment",
                "cost": "cost_analysis",
                "improve": "continuous_improvement",
                "monitor": "monitoring",
                "recover": "error_recovery",
                "experiment": "experiment_runner",
                "deploy": "infrastructure_research",
                "migrate": "infrastructure_research",
            }

            desc_lower = task_description.lower()
            for keyword, capability in _keyword_to_capability.items():
                if keyword in desc_lower:
                    matching_skills = get_skills_by_capability(capability)
                    if matching_skills:
                        worker_skill = get_skill("worker")
                        if worker_skill:
                            _cap_to_mode = {
                                "code_review": "code_review",
                                "security_audit": "security_audit",
                                "test_writing": "build",
                                "documentation_generation": "documentation",
                                "refactoring": "build",
                                "bug_diagnosis": "build",
                                "feature_implementation": "build",
                                "code_discovery": "code_discovery",
                                "architecture_review": "architecture",
                                "risk_assessment": "risk_assessment",
                                "cost_analysis": "cost_analysis",
                                "continuous_improvement": "improvement",
                                "monitoring": "monitor",
                                "error_recovery": "error_recovery",
                                "experiment_runner": "experiment",
                                "infrastructure_research": "devops",
                            }
                            mode = _cap_to_mode.get(capability)
                            if mode and mode in worker_skill.modes:
                                return mode
        except (ImportError, AttributeError, KeyError):
            logger.warning("Capability-based routing unavailable, using default")
        return None

    @staticmethod
    def _has_circular_dependency(graph: ExecutionGraph) -> bool:
        """Check for circular dependencies in the graph."""
        visited: set = set()
        rec_stack: set = set()

        def visit(node_id: str) -> bool:
            """Recursively detect whether ``node_id`` is part of a cycle.

            Returns:
                True if a cycle is detected reachable from this node,
                False if the node and all its transitive dependencies are acyclic.
            """
            if node_id in rec_stack:
                return True
            if node_id in visited:
                return False
            visited.add(node_id)
            rec_stack.add(node_id)
            node = graph.nodes.get(node_id)
            if node:
                for dep in node.depends_on:
                    if visit(dep):
                        return True
            rec_stack.remove(node_id)
            return False

        return any(visit(node_id) for node_id in graph.nodes)
