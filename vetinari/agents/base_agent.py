"""Vetinari Base Agent.

This module defines the base agent class that all Vetinari agents inherit from.
All agents must implement the execute and verify methods.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

from vetinari.agents._context import AgentContext
from vetinari.agents.base_agent_dependencies import (
    _get_agent_constraints,
    _get_complete_task_fn,
    _get_execute_safely_fn,
    _get_execution_context_mod,
    _get_log_event,
    _get_prompt_evolver,
)
from vetinari.agents.base_agent_prompts import (
    BASE_PROMPT_FRAMEWORK,
    COMPACT_PROMPT_FRAMEWORK,
)
from vetinari.agents.base_agent_prompts import (
    build_system_prompt as _build_system_prompt_fn,
)
from vetinari.agents.collaboration import (
    CONTEXT_NEEDED,
    DELEGATE_TO,
    NEEDS_INFO,
    NEEDS_USER_INPUT,
    QUESTION,
    CollaborationBehavior,
)
from vetinari.agents.contracts import AgentResult, AgentTask, VerificationResult, get_agent_spec
from vetinari.agents.inference import InferenceBehavior
from vetinari.agents.observability import _ObservabilitySpan
from vetinari.agents.tools_mixin import AgentToolAccess
from vetinari.exceptions import OutputBudgetExceededError
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# Re-export symbols so existing imports from base_agent keep working.
__all__ = [
    "CONTEXT_NEEDED",
    "DELEGATE_TO",
    "NEEDS_INFO",
    "NEEDS_USER_INPUT",
    "QUESTION",
    "AgentContext",
    "BaseAgent",
    "_ObservabilitySpan",
    "check_output_budget",
]


# ---------------------------------------------------------------------------
# Module-level lazy getters — used by hot-path functions to avoid per-call
# import overhead. Each getter imports once and caches the result.
# Who writes: each getter on first call.  Who reads: hot-path methods below.
# Lock: not needed — GIL protects simple None-check + assignment on CPython,
# and double-assignment of the same object is safe.
# ---------------------------------------------------------------------------

_cached_shared_memory_cls = None


def _get_unified_memory_store_fn():
    """Return the get_unified_memory_store callable, importing once on first call."""
    global _cached_shared_memory_cls
    if _cached_shared_memory_cls is None:
        from vetinari.memory.unified import get_unified_memory_store

        _cached_shared_memory_cls = get_unified_memory_store
    return _cached_shared_memory_cls


# Regex: match a number (int or float) immediately followed by "b" (e.g. "7b", "13b", "0.5b")
_MODEL_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b", re.IGNORECASE)

# Per-agent token budgets (C5 spec). Controls context window allocation per agent type.
_TOKEN_BUDGETS: dict[str, int] = {
    AgentType.FOREMAN.value: 16384,
    AgentType.WORKER.value: 32768,
    AgentType.INSPECTOR.value: 16384,
}


def check_output_budget(user_id: str, tokens_requested: int, budget: int) -> None:
    """Enforce an output-token budget cap per user or agent session.

    Raises OutputBudgetExceededError when tokens_requested exceeds budget,
    implementing the LLM10 (Model Theft / Excessive Output) mitigation from
    OWASP LLM Top 10 2025. Fails closed — an unknown or zero budget is not
    treated as unlimited.

    Args:
        user_id: Identifier of the requesting user or agent (used in the error message).
        tokens_requested: Number of output tokens the caller intends to generate.
        budget: Maximum tokens permitted for this user/session.

    Raises:
        OutputBudgetExceededError: If tokens_requested > budget or budget is not positive.
    """
    if budget <= 0 or tokens_requested > budget:
        raise OutputBudgetExceededError(
            f"output budget exceeded for {user_id!r}: requested {tokens_requested} tokens "
            f"but budget is {budget}; reduce requested tokens or raise the session budget"
        )


@lru_cache(maxsize=256)
def _parse_model_size_b(model_id: str) -> float:
    """Extract the parameter count in billions from a model name or path.

    Searches for patterns like ``7b``, ``13b``, ``0.5b`` (case-insensitive).
    Returns the largest match so that ``qwen2.5-coder-7b-instruct`` → 7.0.
    Returns 0.0 when no numeric size is found (treated as unknown → full tier).

    Args:
        model_id: Model name, path, or identifier string to parse.

    Returns:
        Parameter count in billions, or 0.0 if not determinable.
    """
    if not model_id:
        return 0.0
    matches = _MODEL_SIZE_RE.findall(model_id.lower())
    if not matches:
        return 0.0
    # Take the largest numeric match — avoids false positives from version numbers
    # that are smaller than the actual param count (e.g. "v2" in "llama-v2-7b")
    return max(float(m) for m in matches)


class BaseAgent(InferenceBehavior, AgentToolAccess, CollaborationBehavior, ABC):
    """Base class for all Vetinari agents.

    All agents must inherit from this class and implement:
    - execute(): Process a task and return results
    - verify(): Verify output meets quality standards
    - get_system_prompt(): Return the agent's system prompt
    """

    def __init__(self, agent_type: AgentType, config: dict[str, Any] | None = None):
        """Set up a new agent instance, loading its spec and preparing shared-service slots.

        Args:
            agent_type: The type of agent this instance represents.
            config: Optional per-instance configuration overrides; merged with defaults.
        """
        self.agent_type = agent_type
        self._config = config or {}
        self._spec = get_agent_spec(agent_type)
        self.is_initialized = False
        self._context: dict[str, Any] = {}
        # Shared services (populated by initialize())
        self._adapter_manager = None
        self._web_search = None
        self._tool_registry = None
        self._last_memory_recall_status: dict[str, Any] = {"status": "not_attempted"}
        # Actual model_id from the most recent inference response (set by _call_inference)
        self._last_inference_model_id: str = ""
        # Budget enforcement (ADR-0075) — constructed from spec with safe defaults
        from vetinari.agents.budget_tracker import BudgetTracker

        self._budget = BudgetTracker.from_agent_spec(self._spec) if self._spec else BudgetTracker()

    @property
    def name(self) -> str:
        return self._spec.name if self._spec else self.agent_type.value

    @property
    def description(self) -> str:
        return self._spec.description if self._spec else ""

    @property
    def default_model(self) -> str:
        return self._spec.default_model if self._spec else ""

    @property
    def thinking_variant(self) -> str:
        return self._spec.thinking_variant if self._spec else "medium"

    def initialize(self, context: AgentContext | dict[str, Any]) -> None:
        """Initialize the agent with context.

        Args:
            context: Context information including:
                - adapter_manager: AdapterManager instance for LLM inference
                - web_search: WebSearchTool instance for online research
                - tool_registry: ToolRegistry for registered tools
                - Any agent-specific configuration
        """
        if not isinstance(context, AgentContext):
            context = AgentContext(context)

        self._context = context
        # Extract key shared services from context
        self._adapter_manager = context.get("adapter_manager")
        self._web_search = context.get("web_search")
        self._tool_registry = context.get("tool_registry")
        self.is_initialized = True
        self._log("info", "Agent %s initialized", self.name)

    # ------------------------------------------------------------------
    # Base prompt framework (modern best practices)
    # ------------------------------------------------------------------

    @classmethod
    def _get_base_prompt_framework(cls) -> str:
        """Return the universal prompt framework applied to all agents.

        Returns:
            Full prompt framework string covering reasoning, confidence,
            verification, error handling, and quality standards.
        """
        return BASE_PROMPT_FRAMEWORK

    @classmethod
    def _get_prompt_tier(cls, model_params_b: float = 0.0, model_id: str | None = None) -> str:
        """Select prompt framework tier based on model parameter count or name.

        Smaller models (<=7B) get a compact prompt (~60 tokens). Larger
        models (>7B) get the full framework (~250 tokens).

        Args:
            model_params_b: Model size in billions. 0 means unknown.
            model_id: Optional model name used to infer size when
                *model_params_b* is 0.

        Returns:
            The appropriate prompt framework string.
        """
        if model_params_b == 0.0 and model_id:
            model_params_b = _parse_model_size_b(model_id)
        if 0 < model_params_b <= 7.0:
            return COMPACT_PROMPT_FRAMEWORK
        return BASE_PROMPT_FRAMEWORK

    def _log(self, level: str, message: str, *args: object, **kwargs: object) -> None:
        """Emit structured log with agent context.

        Supports %-style formatting: ``self._log("info", "Got %d items", count)``.
        Extra *kwargs* are included in the structured log payload.
        """
        log_data = {
            "agent_type": self.agent_type.value,
            "agent_name": self.name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **kwargs,
        }
        formatted = message % args if args else message
        getattr(logger, level)("%s | %s", formatted, log_data)

    # ------------------------------------------------------------------
    # Phase 2.0b: Template method helpers for concrete agents
    # ------------------------------------------------------------------

    def _recall_relevant_memories(self, task_description: str) -> list[dict[str, Any]]:
        """Query shared memory for context relevant to the current task.

        Searches long-term memory via ``SharedMemory`` (backed by
        ``UnifiedMemoryStore``) for entries relevant to *task_description*.
        Uses semantic search when the embedding endpoint is available,
        otherwise falls back to FTS5 keyword search.

        Args:
            task_description: Description of the task about to be executed.

        Returns:
            List of memory entry dicts, empty list if unavailable or on error.
        """
        try:
            store = _get_unified_memory_store_fn()()
            entries = store.search(
                task_description,
                agent=self.agent_type.value,
                limit=5,
            )
            recalled = [e.to_dict() for e in entries] if entries else []
            self._last_memory_recall_status = {
                "status": "ok",
                "count": len(recalled),
                "query_present": bool(task_description),
            }
            return recalled
        except Exception as exc:
            logger.warning("Memory recall failed (non-fatal): %s", exc)
            self._last_memory_recall_status = {
                "status": "failed",
                "count": 0,
                "query_present": bool(task_description),
                "error_type": type(exc).__name__,
            }
        return []

    def _execute_safely(self, task: AgentTask, execute_fn: Callable) -> AgentResult:
        """Template wrapper for safe agent execution with full cross-cutting concerns.

        Delegates to ``base_agent_execution.execute_safely`` which handles:
        validation, preparation, guardrails, observability, self-check,
        MetaAdapter recording, output guardrails, and task completion.

        Args:
            task: The task to execute.
            execute_fn: Callable accepting a prepared AgentTask and returning
                an AgentResult with the agent's core execution logic.

        Returns:
            AgentResult with success/failure status, output, and metadata.
        """
        return _get_execute_safely_fn()(self, task, execute_fn)

    @abstractmethod
    def execute(self, task: AgentTask) -> AgentResult:
        """Execute the given task and return results.

        Args:
            task: The task to execute

        Returns:
            AgentResult containing success status, output, and metadata
        """

    @abstractmethod
    def verify(self, output: Any) -> VerificationResult:
        """Verify the output meets quality standards.

        Args:
            output: The output to verify

        Returns:
            VerificationResult with pass/fail status and issues
        """

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Return the system prompt for this agent.

        Returns:
            The system prompt that defines the agent's role and behavior
        """

    def get_capabilities(self) -> list[str]:
        """Return the capabilities of this agent.

        Returns:
            List of capability identifiers
        """
        return []

    def get_metadata(self) -> dict[str, Any]:
        """Return metadata about this agent.

        Returns:
            Dictionary containing agent metadata
        """
        return {
            "agent_type": self.agent_type.value,
            "name": self.name,
            "description": self.description,
            "default_model": self.default_model,
            "thinking_variant": self.thinking_variant,
            "capabilities": self.get_capabilities(),
            "initialized": self.is_initialized,
        }

    def validate_task(self, task: AgentTask) -> bool:
        """Validate that the task is appropriate for this agent.

        Args:
            task: The task to validate

        Returns:
            True if the task is valid for this agent
        """
        if task.agent_type != self.agent_type:
            self._log("warning", "Task agent type %s does not match %s", task.agent_type, self.agent_type)
            return False
        return True

    def prepare_task(self, task: AgentTask) -> AgentTask:
        """Prepare a task for execution.

        This method can be overridden to add preprocessing.

        Args:
            task: The task to prepare

        Returns:
            The prepared task

        Raises:
            PermissionError: If MODEL_INFERENCE permission is denied by the context manager.
        """
        if not self.is_initialized:
            self._log("warning", "Agent not initialized, initializing with default context")
            self.initialize({})

        self._enforce_model_inference_permission()
        task.started_at = datetime.now(timezone.utc).isoformat()
        self._initialize_token_budget()
        self._apply_resource_constraints(task)
        self._emit_task_started(task)
        self._register_prompt_baseline()
        return task

    def _enforce_model_inference_permission(self) -> None:
        try:
            _ec = _get_execution_context_mod()
            _ec.get_context_manager().enforce_permission(_ec.ToolPermission.MODEL_INFERENCE, "agent_execute")
        except (ImportError, AttributeError):
            # Permission system unavailable — flag degraded safety so callers
            # can inspect state; execution is not silently promoted to permitted.
            logger.warning(
                "Permission system unavailable for agent %s — MODEL_INFERENCE check skipped, safety degraded",
                self.agent_type.value,
            )
            self._degraded_safety = True
        except PermissionError:
            raise  # Permission denied — propagate to caller

    def _initialize_token_budget(self) -> None:
        _budget = _TOKEN_BUDGETS.get(self.agent_type.value, 16384)
        if not hasattr(self, "_token_budget_remaining") or self._token_budget_remaining is None:
            self._token_budget_total = _budget
            self._token_budget_remaining = _budget
        # LLM10 (Model Theft / Output Budget) mitigation: enforce the budget as a hard cap.
        check_output_budget(
            user_id=getattr(self, "user_id", "agent"),
            tokens_requested=_budget,
            budget=_budget,
        )

    def _apply_resource_constraints(self, task: AgentTask) -> None:
        constraints = _get_agent_constraints(self.agent_type.value)
        if constraints and constraints.resources:
            rc = constraints.resources
            # Apply max_tokens cap to the task metadata so _infer() can respect it
            if not hasattr(task, "_constraint_max_tokens"):
                task._constraint_max_tokens = rc.max_tokens
            # Apply max_retries cap (accessible by AgentGraph)
            if not hasattr(task, "_constraint_max_retries"):
                task._constraint_max_retries = rc.max_retries
            # Store timeout for monitoring
            if not hasattr(task, "_constraint_timeout"):
                task._constraint_timeout = rc.timeout_seconds
            self._log(
                "debug",
                "Constraints applied: max_tokens=%s, timeout=%ss, max_retries=%s",
                rc.max_tokens,
                rc.timeout_seconds,
                rc.max_retries,
            )
        else:
            # Registry unavailable — apply conservative safety defaults to prevent
            # runaway token usage or infinite retry loops without explicit caps.
            logger.warning(
                "Constraint registry unavailable for agent %s — applying conservative defaults",
                self.agent_type.value,
            )
            if not hasattr(task, "_constraint_max_tokens"):
                task._constraint_max_tokens = 4096
            if not hasattr(task, "_constraint_max_retries"):
                task._constraint_max_retries = 2
            if not hasattr(task, "_constraint_timeout"):
                task._constraint_timeout = 120

    def _emit_task_started(self, task: AgentTask) -> None:
        try:
            _get_log_event()(
                "info",
                f"agent.{self.agent_type.value}",
                "task_started",
                task_id=task.task_id,
                agent=self.agent_type.value,
            )
        except Exception:
            logger.warning("Failed to emit structured trace span for task_started", exc_info=True)

    def _register_prompt_baseline(self) -> None:
        try:
            evolver = _get_prompt_evolver()()
            evolver.register_baseline(self.agent_type.value, self.get_system_prompt())
        except Exception:
            logger.warning("Failed to register prompt baseline for %s", self.agent_type.value, exc_info=True)

    # ------------------------------------------------------------------
    # Phase 7.9I: Dependency results incorporation
    # ------------------------------------------------------------------

    def _incorporate_prior_results(self, task: AgentTask) -> dict[str, Any]:
        """Extract and return dependency results from the task context.

        AgentGraph injects ``dependency_results`` into ``task.context`` before
        calling ``execute()``.  Subclasses can override this method to
        customise how prior results influence their execution.

        Returns:
            Dictionary mapping dependency task IDs to their result summaries.
            Empty dict if no dependency results are available.
        """
        ctx = getattr(task, "context", None) or {}
        dep_results = ctx.get("dependency_results", {})
        if dep_results:
            self._log(
                "debug",
                "Incorporating %d dependency results: %s",
                len(dep_results),
                ", ".join(dep_results.keys()),
            )
        return dep_results

    def complete_task(self, task: AgentTask, result: AgentResult) -> AgentTask:
        """Mark a task complete and run all post-execution subsystems.

        Delegates to ``base_agent_completion.complete_task`` which runs:
        quality scoring, feedback loop, quality gate enforcement, prompt
        evolver recording, training data collection, and episodic memory.

        Args:
            task: The completed task.
            result: The AgentResult from execution.

        Returns:
            The mutated task with completion timestamps and quality metadata.
        """
        return _get_complete_task_fn()(self, task, result)

    # ------------------------------------------------------------------
    # Tiered prompt assembly (US-063)
    # ------------------------------------------------------------------

    def _build_system_prompt(self, mode: str = "") -> str:
        """Build a tiered system prompt with selective context injection.

        Delegates to ``base_agent_prompts.build_system_prompt`` which assembles
        four tiers: core principles, agent identity, mode-relevant
        practices/standards/rules, and RAG knowledge context.

        Args:
            mode: The current agent mode for context-relevant injection.

        Returns:
            Complete assembled system prompt string.
        """
        return _build_system_prompt_fn(self, mode)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}(type={self.agent_type.value}, name={self.name})>"
