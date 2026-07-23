"""Vetinari AgentGraph — Orchestration Engine.

Coordinates the 3-agent factory pipeline (Foreman, Worker, Inspector)
through a Plan DAG, managing:
- Task decomposition and assignment
- Dependency resolution
- True async parallel execution of independent tasks
- Retry, self-correction, and failure handling
- Result aggregation and synthesis
- Inter-agent delegation via the shared Blackboard

This is step 3 of the pipeline: Intake → Planning → **Execution** → Quality Gate → Assembly.

Split layout:
    graph_planner.py     — GraphPlanningEngine: plan creation, topological sort, pipeline builder
    graph_executor.py    — GraphExecutionEngine: execute_plan, execute_subgraph, execute_plan_async
    graph_task_runner.py — GraphTaskRunner: per-task execution with WIP/safety/retry
    graph_recovery.py    — GraphRecoveryEngine: maker-checker, error recovery, delegation, inject_task
    graph_validator.py   — GraphValidationEngine: output schema validation
    replan_engine.py     — ReplanEngine: mid-execution DAG replanning
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from vetinari.orchestration.graph_executor import GraphExecutionEngine
from vetinari.orchestration.graph_planner import GraphPlanningEngine
from vetinari.orchestration.graph_recovery import GraphRecoveryEngine
from vetinari.orchestration.graph_task_runner import GraphTaskRunner
from vetinari.orchestration.graph_types import (
    ConditionalEdge,
    CycleDetector,
    ExecutionDAG,
    ExecutionStrategy,
    HumanCheckpoint,
    ReplanResult,
    TaskNode,
)
from vetinari.orchestration.graph_validator import GraphValidationEngine
from vetinari.orchestration.plan_diff import PlanDiff
from vetinari.orchestration.replan_engine import ReplanEngine
from vetinari.structured_logging import log_event
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cached lazy getters for vetinari sub-modules used by AgentGraph accessors
# ---------------------------------------------------------------------------

# vetinari.workflow.WIPTracker — pull-based flow control (optional)
_cached_wip_tracker_class: type | None = None
_wip_tracker_class_loaded = False


def _get_wip_tracker_class() -> type | None:
    """Return the WIPTracker class, loading it once and caching the result.

    Returns:
        The WIPTracker class if the module is available, otherwise None.
    """
    global _cached_wip_tracker_class, _wip_tracker_class_loaded
    if not _wip_tracker_class_loaded:
        try:
            from vetinari.workflow import WIPTracker

            _cached_wip_tracker_class = WIPTracker
        except Exception:
            _cached_wip_tracker_class = None
        _wip_tracker_class_loaded = True
    return _cached_wip_tracker_class


# vetinari.skills.skill_registry — capability/skill lookup
_cached_skills_fns: dict[str, Any] | None = None
_skills_fns_loaded = False


def _get_skills_fns() -> dict[str, Any] | None:
    """Return skill registry functions as a dict, loading once and caching.

    Returns:
        Dict with keys 'get_skills_by_capability', 'get_skill_for_agent_type',
        and 'get_all_skills' if the module is available, otherwise None.
    """
    global _cached_skills_fns, _skills_fns_loaded
    if _skills_fns_loaded and _cached_skills_fns is not None:
        return _cached_skills_fns
    if not _skills_fns_loaded:
        try:
            from vetinari.skills import skill_registry as registry_module

            if not getattr(registry_module, "__file__", None):
                raise ImportError("skill registry resolved to a non-file-backed module")

            _cached_skills_fns = {
                "get_skills_by_capability": registry_module.get_skills_by_capability,
                "get_skill_for_agent_type": registry_module.get_skill_for_agent_type,
                "get_all_skills": registry_module.get_all_skills,
            }
        except Exception:
            logger.warning("Could not load skill registry functions", exc_info=True)
            _cached_skills_fns = None
            _skills_fns_loaded = False
            return None
        _skills_fns_loaded = True
    return _cached_skills_fns


# Backward-compatible alias: tests and external callers may import ExecutionPlan.
# The canonical name is ExecutionDAG; this alias preserves existing import contracts.
ExecutionPlan = ExecutionDAG

# Re-export types that tests import from this module for backward compatibility.
__all__ = [
    "AgentGraph",
    "ConditionalEdge",
    "CycleDetector",
    "ExecutionDAG",
    "ExecutionPlan",
    "ExecutionStrategy",
    "HumanCheckpoint",
    "ReplanResult",
    "TaskNode",
    "get_agent_graph",
]


class AgentGraph(
    GraphExecutionEngine,
    GraphPlanningEngine,
    GraphRecoveryEngine,
    GraphTaskRunner,
    GraphValidationEngine,
    ReplanEngine,
):
    """Orchestration engine for Vetinari's 3-agent factory pipeline.

    Registers Foreman (orchestration), Worker (execution), and Inspector
    (quality gate) — 3 agents with 34 total modes.

    Key features
    ------------
    - True parallel execution via ``ThreadPoolExecutor`` for independent DAG layers
    - ``execute_plan_async`` uses ``asyncio.gather`` over thread pool
    - Agent-model affinity routing (VL model for vision tasks)
    - Self-correction loop: failed verification triggers one guided retry
    - Delegates unresolvable failures to Worker(error_recovery)
    - Constraint enforcement (delegation rules, resource limits)
    - Per-instance ``_graph_lock`` guards runtime plan edits and cancellation walks
    """

    def __init__(
        self,
        strategy: ExecutionStrategy = ExecutionStrategy.ADAPTIVE,
        max_workers: int = 5,
        quality_reviewed_agents: set | None = None,
        wip_tracker: Any | None = None,
    ):
        self._strategy = strategy
        self._max_workers = max_workers
        self._agents: dict[AgentType, Any] = {}
        self._execution_plans: dict[str, ExecutionDAG] = {}
        # Re-entrant because runtime-diff drains may re-enter cancellation
        # propagation while holding the graph-state lock.
        self._graph_lock: threading.RLock = threading.RLock()
        # Written by GraphExecutionEngine._enqueue_runtime_diff and drained only
        # at layer boundaries by GraphExecutionEngine._drain_runtime_diff_queue.
        self._runtime_diff_queues: dict[str, list[PlanDiff]] = {}
        self._initialized = False
        self._goal_tracker: Any | None = None
        self._milestone_manager = None
        # Optional callback for milestone approvals set by the web layer or CLI.
        # When set, each milestone checkpoint invokes this callback to obtain
        # a MilestoneApproval rather than auto-approving.
        self._milestone_approval_callback: Any | None = None
        self._quality_reviewed_agents: set = (
            quality_reviewed_agents if quality_reviewed_agents is not None else {AgentType.WORKER}
        )
        # WIP tracking for pull-based flow
        self._wip_tracker = wip_tracker
        if self._wip_tracker is None:
            try:
                _WIPTracker = _get_wip_tracker_class()
                if _WIPTracker is not None:
                    self._wip_tracker = _WIPTracker()
                    logger.debug("[AgentGraph] WIPTracker initialized with default config")
                else:
                    logger.warning("[AgentGraph] WIPTracker unavailable, WIP limits disabled")
            except Exception:  # Broad: optional feature; any failure must not block task execution
                logger.warning("[AgentGraph] WIPTracker unavailable, WIP limits disabled")

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Load factory-pipeline agents and mark the graph as ready."""
        if self._initialized:
            return

        from vetinari.agents import (
            get_foreman_agent,
            get_inspector_agent,
            get_worker_agent,
        )

        # --- 3 active factory-pipeline agents ---
        _agent_map: list[tuple] = [
            (AgentType.FOREMAN, get_foreman_agent),
            (AgentType.WORKER, get_worker_agent),
            (AgentType.INSPECTOR, get_inspector_agent),
        ]

        for agent_type, getter in _agent_map:
            if getter is None:
                logger.warning("[AgentGraph] Getter for %s is None — skipping", agent_type)
                continue
            try:
                agent = getter()
                if agent is not None:
                    agent.initialize({})
                    self._agents[agent_type] = agent
                    logger.debug("[AgentGraph] Registered %s", agent_type.value)
            except Exception as exc:
                logger.warning("[AgentGraph] Could not initialize %s: %s", agent_type.value, exc)

        # Pre-warm specialized worker variants so their singletons are ready
        # before the first task arrives.  These agents are NOT registered in
        # self._agents (which is keyed by AgentType) because both share
        # AgentType.WORKER with the primary worker; they are accessed directly
        # through their own getter functions when needed.
        try:
            from vetinari.agents.builder_agent import get_builder_agent

            get_builder_agent()
            logger.debug("[AgentGraph] BuilderAgent pre-warmed")
        except Exception as exc:
            logger.warning("[AgentGraph] BuilderAgent pre-warm failed — builder tasks will cold-start: %s", exc)

        try:
            from vetinari.agents.consolidated.researcher_agent import get_consolidated_researcher_agent

            get_consolidated_researcher_agent()
            logger.debug("[AgentGraph] ConsolidatedResearcherAgent pre-warmed")
        except Exception as exc:
            logger.warning(
                "[AgentGraph] ConsolidatedResearcherAgent pre-warm failed — research tasks will cold-start: %s",
                exc,
            )

        logger.info(
            "[AgentGraph] Initialized %d agents (strategy=%s)",
            len(self._agents),
            self._strategy.value,
        )
        log_event(
            "info",
            __name__,
            "AgentGraph initialized",
            agent_count=len(self._agents),
            strategy=self._strategy.value,
        )
        self._initialized = True

    def _validate_output_schema(self, agent_type: AgentType, output: Any) -> list[str]:
        """Validate agent output while treating scalar payloads as type failures only."""
        if not isinstance(output, dict):
            spec = self.get_skill_spec(agent_type)
            if spec is None:
                return [f"Missing SkillSpec for agent type: {agent_type.value}"]
            if not spec.output_schema:
                return [f"Missing output schema for agent type: {agent_type.value}"]
            return [f"Output for {agent_type.value} must be an object, got {type(output).__name__}"]

        return super()._validate_output_schema(agent_type, output)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_execution_plan(self, plan_id: str) -> ExecutionDAG | None:
        """Look up a previously stored execution plan by its identifier.

        Args:
            plan_id: Unique identifier of the execution plan to retrieve.

        Returns:
            The matching ExecutionDAG, or None if no plan exists for that id.
        """
        return self._execution_plans.get(plan_id)

    def get_agent(self, agent_type: AgentType) -> Any | None:
        """Retrieve a registered agent instance by its AgentType.

        Args:
            agent_type: The AgentType enum value identifying the agent.

        Returns:
            The agent instance, or None if no agent of that type is registered.
        """
        return self._agents.get(agent_type)

    def get_registered_agents(self) -> list[AgentType]:
        """Return the AgentType keys for all currently registered agents.

        Returns:
            List of AgentType values that have been registered in the graph.
        """
        return list(self._agents.keys())

    def get_agent_by_capability(self, capability: str) -> Any | None:
        """Find the best registered agent that declares a given capability.

        Consults the programmatic SkillSpec registry to map capabilities
        to agent types, then returns the live agent instance if registered.

        Args:
            capability: Capability tag to search for (e.g. "coding", "vision").

        Returns:
            The matching agent instance, or None if no match is found.
        """
        try:
            _skills = _get_skills_fns()
            if _skills is None:
                raise RuntimeError("Skill registry unavailable")
            matching_specs = _skills["get_skills_by_capability"](capability)
            if not matching_specs:
                return None

            # WORKER (multi-mode) has the richest capability set; INSPECTOR and FOREMAN follow
            _AGENT_TYPE_PRIORITY = {
                AgentType.WORKER.value: 10,
                AgentType.INSPECTOR.value: 9,
                AgentType.FOREMAN.value: 8,
            }
            matching_specs.sort(
                key=lambda s: _AGENT_TYPE_PRIORITY.get(s.agent_type, 0),
                reverse=True,
            )

            for spec in matching_specs:
                agent_type_str = spec.agent_type
                try:
                    at = AgentType(agent_type_str)
                except ValueError:
                    logger.warning("Agent spec references unknown agent type %r — skipping spec", agent_type_str)
                    continue
                agent = self._agents.get(at)
                if agent is not None:
                    return agent
        except Exception as e:
            logger.warning("[AgentGraph] Capability lookup for '%s' failed: %s", capability, e)
        return None

    def get_skill_spec(self, agent_type: AgentType) -> Any | None:
        """Return the SkillSpec for a given agent type, if one exists.

        Args:
            agent_type: The AgentType to look up.

        Returns:
            The SkillSpec dataclass, or None if the agent type has no registered spec.
        """
        try:
            _skills2 = _get_skills_fns()
            if _skills2 is None:
                raise RuntimeError("Skill registry unavailable")
            return _skills2["get_skill_for_agent_type"](agent_type.value)
        except Exception:  # Broad: optional feature; any failure must not block task execution
            logger.warning("Skill spec lookup failed for agent type %s", agent_type, exc_info=True)
            return None

    def get_agents_for_task_type(self, task_type: str) -> list[AgentType]:
        """Return agent types whose SkillSpec modes include the given task type.

        Args:
            task_type: Task type string to search for in skill spec modes.

        Returns:
            List of AgentType values whose specs declare this task type.
        """
        results = []
        try:
            _skills3 = _get_skills_fns()
            if _skills3 is None:
                raise RuntimeError("Skill registry unavailable")
            for spec in _skills3["get_all_skills"]().values():
                if task_type in spec.modes or task_type in spec.capabilities:
                    agent_type_str = spec.agent_type
                    try:
                        at = AgentType(agent_type_str)
                        if at in self._agents and at not in results:
                            results.append(at)
                    except ValueError:
                        logger.warning("Unrecognized agent type %s in skill spec", spec.agent_type, exc_info=True)
        except Exception:
            logger.warning("Failed to resolve agents by skill for task type %s", task_type, exc_info=True)
        return results

    def set_milestone_approval_callback(self, callback: Any) -> None:
        """Register a callback to handle milestone approval during plan execution.

        The callback is invoked by ``MilestoneManager.check_and_wait`` whenever
        a task marked as a milestone checkpoint completes.  It receives a
        ``MilestoneReached`` event and must return a ``MilestoneApproval``.

        When no callback is set (the default), milestones auto-approve once the
        quality threshold defined in ``MilestonePolicy`` is exceeded.

        Args:
            callback: Callable that accepts a ``MilestoneReached`` and returns
                a ``MilestoneApproval``.  Set to ``None`` to revert to
                auto-approval.
        """
        self._milestone_approval_callback = callback
        # If a milestone manager is already live (plan is executing), propagate
        # the callback to it immediately so the running plan picks it up.
        if self._milestone_manager is not None:
            try:
                self._milestone_manager.set_approval_callback(callback)
            except Exception as exc:
                logger.warning(
                    "Could not propagate approval callback to live MilestoneManager — "
                    "callback will be applied on the next plan execution: %s",
                    exc,
                )

    def __repr__(self) -> str:
        return f"<AgentGraph(strategy={self._strategy.value}, agents={len(self._agents)}/27)>"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_agent_graph: AgentGraph | None = None
# Singleton-construction lock only; distinct from AgentGraph._graph_lock.
_graph_lock = threading.Lock()


def get_agent_graph(
    strategy: ExecutionStrategy = ExecutionStrategy.ADAPTIVE,
) -> AgentGraph:
    """Return the singleton AgentGraph, initializing it if needed.

    Args:
        strategy: Execution strategy to use when creating a new instance.

    Returns:
        The shared AgentGraph singleton.
    """
    global _agent_graph
    if _agent_graph is None:
        with _graph_lock:
            if _agent_graph is None:
                _agent_graph = AgentGraph(strategy=strategy)
                _agent_graph.initialize()
    return _agent_graph
