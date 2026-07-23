"""Vetinari Foreman Agent (v0.5.0).

The Foreman is the central planning and user interaction agent in the
3-agent factory pipeline (Foreman -> Worker -> Inspector). It generates
dynamic plans from goals, coordinates Worker task assignment, and handles
user clarification and context management.

Modes: plan, clarify, consolidate, summarise, prune, extract

Mode prompts live in planner_prompts.py; decomposition helpers in planner_decompose.py.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any

from vetinari.agents.consolidated.foreman import (
    evaluate_dispatch_gate as evaluate_dispatch_gate,
)
from vetinari.agents.contracts import (
    VerificationResult,
)
from vetinari.agents.multi_mode_agent import MultiModeAgent
from vetinari.agents.planner_agent_context import ForemanContextMixin
from vetinari.agents.planner_agent_interaction import ForemanInteractionMixin
from vetinari.agents.planner_agent_plan import ForemanPlanMixin
from vetinari.agents.planner_decompose import (
    decompose_goal_keyword as decompose_goal_keyword,
)
from vetinari.agents.planner_decompose import (
    decompose_goal_llm as decompose_goal_llm,
)
from vetinari.agents.planner_decompose import (
    is_vague_goal as is_vague_goal,
)
from vetinari.agents.planner_prompts import FOREMAN_MODE_PROMPTS
from vetinari.boundary_guards import require_nonempty
from vetinari.exceptions import JurisdictionViolation
from vetinari.plan_cache import PlanCache, get_plan_cache
from vetinari.planning.non_goals import NonGoalStore
from vetinari.planning.non_goals import check_non_goals as check_non_goals
from vetinari.planning.plan_reviewer import PlanReviewer
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


def verify_plan_closure(*, closure_evidence: str) -> bool:
    """Validate that plan closure has concrete evidence before it is accepted.

    Returns:
        ``True`` when closure evidence is present.
    """
    require_nonempty(closure_evidence, field_name="closure_evidence")
    return True


class ForemanAgent(MultiModeAgent, ForemanPlanMixin, ForemanInteractionMixin, ForemanContextMixin):
    """Foreman agent - planning, user interaction, and context management.

    The Foreman orchestrates the factory pipeline by decomposing goals into
    task DAGs, assigning work to the Worker, and managing user interaction.
    """

    MODES = {
        "plan": "_execute_plan",
        "clarify": "_execute_clarify",
        "consolidate": "_execute_consolidate",
        "summarise": "_execute_summarise",
        "prune": "_execute_prune",
        "extract": "_execute_extract",
    }
    DEFAULT_MODE = "plan"
    MODE_KEYWORDS = {
        "plan": ["plan", "decompose", "schedule", "specify", "goal", "task", "breakdown"],
        "clarify": ["ambiguous", "clarif", "question", "unclear", "vague", "user input"],
        "consolidate": ["consolidat", "memory", "merge", "context"],
        "summarise": ["summari", "summariz", "digest", "recap"],
        "prune": ["prune", "trim", "reduce", "budget", "token limit"],
        "extract": ["extract", "knowledge", "entities", "structured"],
    }
    _MAX_ENTRIES_FOR_CONSOLIDATION = 50
    # Foreman is a pure coordinator - inference is restricted to planning modes.
    # Task execution inference must go through Worker. See ADR-0093.
    _PLANNING_MODES: frozenset[str] = frozenset(MODES.keys())

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        plan_reviewer: PlanReviewer | None = None,
        non_goal_store: NonGoalStore | None = None,
    ):
        super().__init__(AgentType.FOREMAN, config)
        self._max_depth = self._config.get("max_depth", 14)
        self._min_tasks = self._config.get("min_tasks", 5)
        self._max_tasks = self._config.get("max_tasks", 15)
        # Orchestrator state (absorbed)
        self._interaction_mode = (config or {}).get("mode", "interactive")
        self._callback: Callable | None = None
        self._pending_questions: list[dict[str, Any]] = []
        self._gathered_context: dict[str, Any] = {}
        self._max_context_tokens = int(
            (config or {}).get("max_context_tokens", os.environ.get("VETINARI_MAX_CONTEXT_TOKENS", "4096")),
        )
        self._plan_cache: PlanCache = get_plan_cache()
        # Plan-review dispatch gate (ADR-0106).
        # When plan_reviewer is None the gate is bypassed; existing callers that
        # construct ForemanAgent without a reviewer are unaffected.
        self._plan_reviewer: PlanReviewer | None = plan_reviewer
        self._non_goal_store: NonGoalStore = non_goal_store or NonGoalStore()

    def _infer(self, prompt: str, **kwargs: Any) -> str:
        """Guard that restricts Foreman inference to planning/coordination only.

        The Foreman decomposes goals and delegates execution to Workers.
        Direct task-execution inference MUST go through the Worker pipeline.
        All six Foreman modes (plan, clarify, consolidate, summarise, prune,
        extract) are planning modes - this guard catches calls made outside
        a recognized mode context or from future non-planning modes.

        Args:
            prompt: The user/task prompt forwarded to the LLM.
            **kwargs: Remaining inference parameters forwarded to super().

        Returns:
            LLM response text.

        Raises:
            JurisdictionViolation: If called outside a recognized planning mode.
        """
        if self._current_mode not in self._PLANNING_MODES:
            raise JurisdictionViolation(
                f"Foreman inference blocked - mode {self._current_mode!r} is not a "
                f"planning mode. Task execution must be delegated to Worker. "
                f"Allowed modes: {sorted(self._PLANNING_MODES)}"
            )
        return super()._infer(prompt, **kwargs)

    def _get_base_system_prompt(self) -> str:
        return (
            "You are Vetinari's Foreman - the factory pipeline orchestrator. "
            "You handle goal decomposition, task scheduling, Worker assignment, "
            "user interaction (ambiguity detection, clarifying questions), and "
            "context management (memory consolidation, session summarisation, "
            "knowledge extraction)."
        )

    def _get_mode_system_prompt(self, mode: str) -> str:
        """Return the LLM system prompt for the given Foreman mode.

        Prompts are stored in planner_prompts.py to keep this file under
        the 550-line limit.

        Args:
            mode: One of plan, clarify, consolidate, summarise, prune, extract.

        Returns:
            System prompt string, or empty string for unknown modes.
        """
        return FOREMAN_MODE_PROMPTS.get(mode, "")

    def verify(self, output: Any) -> VerificationResult:
        """Verify output - mode-aware.

        Returns:
            The VerificationResult result.
        """
        if not isinstance(output, dict):
            return VerificationResult(passed=False, issues=[{"message": "Output must be a dict"}], score=0.0)

        mode = self._current_mode or self.DEFAULT_MODE
        if mode == "plan":
            issues = []
            score = 1.0
            required_fields = ["plan_id", "goal", "tasks"]
            for f in required_fields:
                if f not in output:
                    issues.append({"type": "missing_field", "message": f"Missing: {f}"})
                    score -= 0.2
            tasks = output.get("tasks", [])
            if len(tasks) < self._min_tasks:
                issues.append({"type": "insufficient_tasks", "message": f"Too few tasks: {len(tasks)}"})
                score -= 0.1
            if not any(t.get("dependencies") for t in tasks):
                issues.append({"type": "no_dependencies", "message": "No task dependencies"})
                score -= 0.1
            closure_evidence = output.get("closure_evidence")
            if closure_evidence is not None:
                try:
                    verify_plan_closure(closure_evidence=str(closure_evidence))
                except ValueError as exc:
                    issues.append({"type": "missing_closure_evidence", "message": str(exc)})
                    score -= 0.2
            return VerificationResult(passed=score >= 0.7, issues=issues, score=max(0, score))

        mode_evidence_fields = {
            "clarify": ("pending_questions", "needs_user_input"),
            "consolidate": ("consolidated_summary", "key_knowledge", "entries_processed"),
            "summarise": ("session_summary", "goals_achieved", "next_steps", "entries_processed"),
            "prune": ("entries_to_retain", "stale_entries", "pruned_count"),
            "extract": ("key_knowledge", "entities_discovered"),
        }
        evidence_fields = mode_evidence_fields.get(mode)
        if evidence_fields is None:
            return VerificationResult(
                passed=False,
                issues=[{"type": "unknown_mode", "message": f"Unknown Foreman verification mode: {mode}"}],
                score=0.0,
            )
        has_mode_evidence = any(field in output and output.get(field) is not None for field in evidence_fields)
        return VerificationResult(
            passed=has_mode_evidence,
            issues=[] if has_mode_evidence else [{"type": "missing_mode_evidence", "message": f"No {mode} evidence"}],
            score=0.8 if has_mode_evidence else 0.0,
        )

    def get_capabilities(self) -> list[str]:
        """Return capability strings describing this agent's supported modes and features.

        Returns:
            List of capability identifiers such as plan generation,
            task decomposition, and risk assessment.
        """
        return [
            "plan_generation",
            "task_decomposition",
            "dependency_mapping",
            "resource_estimation",
            "risk_assessment",
            "ambiguity_detection",
            "clarification_generation",
            "context_gathering",
            "memory_consolidation",
            "session_summarisation",
            "context_pruning",
            "knowledge_extraction",
        ]

    # ------------------------------------------------------------------
    # Mode verification requirements
    # ------------------------------------------------------------------

    def validate_agent_output(
        self,
        agent_type: str,
        mode: str,
        output: dict[str, Any] | None,
    ) -> tuple[bool, list[str]]:
        """Validate agent output against mode-specific verification requirements.

        Checks the output against ``MODE_VERIFICATION_REQUIREMENTS`` defined in
        ``vetinari.agents.practices``. Returns pass/fail and a list of unmet
        requirements for rework routing.

        Args:
            agent_type: The agent type value (e.g. "WORKER").
            mode: The mode name (e.g. "build").
            output: The agent's output dict to validate.

        Returns:
            Tuple of (passed: bool, unmet_requirements: list[str]).
        """
        try:
            from vetinari.agents.practices import get_verification_requirements
        except ImportError:
            logger.warning(
                "Could not import vetinari.agents.practices - cannot validate %s:%s output, treating as failed",
                agent_type,
                mode,
            )
            return False, ["agent practices module unavailable"]

        requirements = get_verification_requirements(agent_type, mode)
        if not requirements:
            return True, []

        if output is None:
            return False, requirements

        metadata = output if isinstance(output, dict) else {}
        verification = metadata.get("verification", {})

        unmet: list[str] = [req for req in requirements if not verification.get(req, False)]
        passed = len(unmet) == 0
        if not passed:
            logger.warning(
                "Agent %s:%s failed verification - unmet requirements: %s",
                agent_type,
                mode,
                unmet,
            )
        return passed, unmet


# Singleton instance
_foreman_agent: ForemanAgent | None = None
_foreman_agent_lock = threading.Lock()


def get_foreman_agent(config: dict[str, Any] | None = None) -> ForemanAgent:
    """Get the singleton Foreman agent instance.

    Args:
        config: Optional configuration dict.

    Returns:
        A configured ForemanAgent instance.
    """
    global _foreman_agent
    if _foreman_agent is None:
        with _foreman_agent_lock:
            if _foreman_agent is None:
                _foreman_agent = ForemanAgent(config)
    return _foreman_agent
