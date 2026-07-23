"""Consolidated Worker Agent (v0.5.0).

========================================
The Worker is the unified execution agent in Vetinari's 3-agent factory
pipeline (Foreman → Worker → Inspector). It combines all execution modes from
the former Researcher, Oracle, Builder, and Operations agents into a single
agent with 4 mode groups.

Mode Groups:
- **Research** (8 modes): code_discovery, domain_research, api_lookup,
  lateral_thinking, ui_design, database, devops, git_workflow
- **Architecture** (5 modes): architecture, risk_assessment,
  ontological_analysis, contrarian_review, suggest
- **Build** (2 modes): build, image_generation
- **Operations** (9 modes): documentation, creative_writing, cost_analysis,
  experiment, error_recovery, synthesis, improvement, monitor, devops_ops

Internally delegates to the existing agent implementations — zero behavior
change. The consolidation eliminates inter-agent handoff latency and context
loss for multi-step tasks (e.g., research → architecture → build) that
previously required 4 separate agent invocations.

Per-mode constraints are preserved:
- Architecture modes: read-only + ADR production
- Build modes: sole writer of production files
- Research modes: read-only + web access
- Operations modes: post-execution synthesis and monitoring
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.agents.contracts import AgentResult, AgentTask, VerificationResult
from vetinari.agents.handlers import HandlerRouter
from vetinari.agents.handlers.cost_analysis_handler import CostAnalysisHandler
from vetinari.agents.handlers.creative_writing_handler import CreativeWritingHandler
from vetinari.agents.handlers.documentation_handler import DocumentationHandler
from vetinari.agents.multi_mode_agent import MultiModeAgent
from vetinari.exceptions import CapabilityNotAvailable
from vetinari.security import AgentAction, AgentPermissions, sanitize_prompt
from vetinari.types import AgentType
from vetinari.workers.sandbox.worker_execution import execute_worker_code_in_configured_sandbox

logger = logging.getLogger(__name__)


# ── Lazy getter for ADRSystem (avoids circular import at module level) ──
_ADRSystem = None


def _get_adr_system_class():
    global _ADRSystem
    if _ADRSystem is None:
        from vetinari.adr import ADRSystem

        _ADRSystem = ADRSystem
    return _ADRSystem


# ── Lazy getter for get_available_mcp_tools ──────────────────────────────────
# Deferred so the heavy subprocess/config loading in worker_bridge does not
# run at import time, but the symbol is cached after first access so
# get_system_prompt() does not re-import on every call (VET130).
_get_available_mcp_tools = None


def _get_mcp_tools_fn():
    """Return the get_available_mcp_tools callable, loading it on first use."""
    global _get_available_mcp_tools
    if _get_available_mcp_tools is None:
        from vetinari.mcp.worker_bridge import get_available_mcp_tools

        _get_available_mcp_tools = get_available_mcp_tools
    return _get_available_mcp_tools


def _authorize_worker_action(action: AgentAction) -> None:
    """Raise when the worker permission policy denies a dispatch action."""
    if not AgentPermissions().is_allowed(AgentType.WORKER, action):
        raise PermissionError(f"agent action denied: {action}")


# ── Mode-to-group mapping for constraint enforcement ─────────────────
MODE_GROUPS: dict[str, str] = {
    # Research group — read-only + web
    "code_discovery": "research",
    "domain_research": "research",
    "api_lookup": "research",
    "lateral_thinking": "research",
    "ui_design": "research",
    "database": "research",
    "devops": "research",
    "git_workflow": "research",
    # Architecture group — read-only + ADRs
    "architecture": "architecture",
    "risk_assessment": "architecture",
    "ontological_analysis": "architecture",
    "contrarian_review": "architecture",
    "suggest": "architecture",
    # Build group — sole writer
    "build": "build",
    "image_generation": "build",
    # Operations group — post-execution
    "documentation": "operations",
    "creative_writing": "operations",
    "cost_analysis": "operations",
    "experiment": "operations",
    "error_recovery": "operations",
    "synthesis": "operations",
    "improvement": "operations",
    "monitor": "operations",
    "devops_ops": "operations",
}


_WORKER_FROZEN_ATTRS: frozenset[str] = frozenset({
    "agent_type",
    "MODES",
    "DEFAULT_MODE",
    "MODE_KEYWORDS",
})
_READ_ONLY_MODE_GROUPS: frozenset[str] = frozenset({"research", "architecture"})
_WORKER_BASE_SYSTEM_PROMPT = (
    "You are Vetinari's Worker — the unified execution engine of the "
    "factory pipeline. You have 24 operational modes across 4 groups: "
    "Research (code discovery, domain analysis, API lookup, lateral "
    "thinking, UI design, database, DevOps, git workflow), "
    "Architecture (decisions, risk assessment, ontological analysis, "
    "contrarian review), Build (code scaffolding, image generation), "
    "and Operations (documentation, creative writing, cost analysis, "
    "experiments, error recovery, synthesis, improvement, monitoring). "
    "You execute with full context — no handoff losses between modes."
)


class WorkerAgent(MultiModeAgent):
    """Unified execution agent combining research, architecture, build, and operations.

    The Worker wraps the 4 former execution agents (Researcher, Oracle, Builder,
    Operations) and delegates each mode to the appropriate internal implementation.
    This preserves all existing behavior while collapsing the agent boundary.

    Args:
        config: Optional configuration dict. Sub-configs for internal agents
            can be nested under ``research_config``, ``oracle_config``,
            ``builder_config``, and ``operations_config`` keys.
    """

    # ── All 24 modes across 4 groups ─────────────────────────────────
    MODES = {
        # Research modes (8) — delegated to ConsolidatedResearcherAgent
        "code_discovery": "_dispatch_research",
        "domain_research": "_dispatch_research",
        "api_lookup": "_dispatch_research",
        "lateral_thinking": "_dispatch_research",
        "ui_design": "_dispatch_research",
        "database": "_dispatch_research",
        "devops": "_dispatch_research",
        "git_workflow": "_dispatch_research",
        # Architecture modes (5) — delegated to ConsolidatedOracleAgent
        "architecture": "_dispatch_oracle",
        "risk_assessment": "_dispatch_oracle",
        "ontological_analysis": "_dispatch_oracle",
        "contrarian_review": "_dispatch_oracle",
        "suggest": "_dispatch_oracle",
        # Build modes (2) — delegated to BuilderAgent
        "build": "_dispatch_builder",
        "image_generation": "_dispatch_builder",
        # Operations modes (9) — delegated to OperationsAgent
        "documentation": "_dispatch_operations",
        "creative_writing": "_dispatch_operations",
        "cost_analysis": "_dispatch_operations",
        "experiment": "_dispatch_operations",
        "error_recovery": "_dispatch_operations",
        "synthesis": "_dispatch_operations",
        "improvement": "_dispatch_operations",
        "monitor": "_dispatch_operations",
        "devops_ops": "_dispatch_operations",
    }
    DEFAULT_MODE = "build"

    MODE_KEYWORDS = {
        # Research
        "code_discovery": [
            "code",
            "file",
            "class",
            "function",
            "pattern",
            "codebase",
            "discover",
            "explore",
            "search code",
        ],
        "domain_research": [
            "research",
            "feasib",
            "competit",
            "market",
            "domain",
            "analys",
            "consult",
            "advise",
        ],
        "api_lookup": ["api", "library", "framework", "package", "documentation", "docs", "dependency"],
        "lateral_thinking": ["lateral", "creative", "alternative", "novel", "brainstorm", "unconventional"],
        "ui_design": [
            "ui",
            "ux",
            "frontend",
            "component",
            "wireframe",
            "layout",
            "design token",
            "accessibility",
            "wcag",
            "responsive",
        ],
        "database": ["database", "schema", "table", "migration", "etl", "sql", "data model", "orm"],
        "devops": [
            "ci/cd",
            "docker",
            "kubernetes",
            "terraform",
            "deploy",
            "container",
            "pipeline",
            "helm",
            "infrastructure",
        ],
        "git_workflow": [
            "git",
            "branch",
            "commit",
            "merge",
            "pull request",
            "pr",
            "release",
            "changelog",
            "tag",
            "rebase",
        ],
        # Architecture
        "architecture": ["architect", "design", "structure", "pattern", "component", "module"],
        "risk_assessment": ["risk", "vulnerab", "threat", "impact", "likelihood", "mitigat"],
        "ontological_analysis": ["ontolog", "concept", "fundament", "deep analy", "ponder", "deliberat"],
        "contrarian_review": ["contrarian", "challenge", "assumption", "blind spot", "devil", "critique"],
        "suggest": ["suggest", "improve", "enhancement", "recommendation"],
        # Build
        "build": [
            "scaffold",
            "boilerplate",
            "generate code",
            "code gen",
            "implement",
            "build",
            "code",
        ],
        "image_generation": [
            "image",
            "logo",
            "icon",
            "mockup",
            "diagram",
            "illustration",
            "banner",
            "svg",
            "picture",
            "visual",
        ],
        # Operations
        "documentation": ["document", "api doc", "user guide", "changelog", "readme"],
        "creative_writing": ["creative", "story", "narrative", "prose", "write"],
        "cost_analysis": ["cost", "pricing", "budget", "roi", "expense", "token cost"],
        "experiment": ["experiment", "a/b test", "hypothesis", "control group", "variant"],
        "error_recovery": ["error recovery", "failure", "retry", "fallback", "resilien"],
        "synthesis": ["synthesiz", "synthesise", "combine", "fuse", "consolidat"],
        "improvement": ["improv", "kaizen", "optimize", "enhance", "performance"],
        "monitor": ["monitor", "health", "metric", "alert", "uptime"],
        "devops_ops": ["deploy", "release", "rollback", "scale", "infra"],
    }

    def __init__(self, config: dict[str, Any] | None = None):
        """Initialize the Worker with internal delegate agents.

        Args:
            config: Configuration dict. Sub-configs can be nested under
                ``research_config``, ``oracle_config``, ``builder_config``,
                ``operations_config`` for per-group customization.
        """
        # Import here to avoid circular imports — internal delegates
        from vetinari.agents.builder_agent import BuilderAgent
        from vetinari.agents.consolidated.operations_agent import OperationsAgent
        from vetinari.agents.consolidated.oracle_agent import ConsolidatedOracleAgent

        # Initialize MultiModeAgent with WORKER type
        super().__init__(AgentType.WORKER, config)

        # Create internal delegates with optional per-group config
        cfg = config or {}
        self._researcher = None
        self._research_config = cfg.get("research_config")
        self._oracle = ConsolidatedOracleAgent(cfg.get("oracle_config"))
        self._builder = BuilderAgent(cfg.get("builder_config"))
        self._operations = OperationsAgent(cfg.get("operations_config"))

        # Map mode groups to delegates for constraint lookup
        self._group_delegates: dict[str, MultiModeAgent | None] = {
            "research": None,
            "architecture": self._oracle,
            "build": self._builder,
            "operations": self._operations,
        }

        # Register operation handlers for direct dispatch (bypasses delegate overhead)
        self._handler_router = HandlerRouter()
        self._handler_router.register(DocumentationHandler())
        self._handler_router.register(CreativeWritingHandler())
        self._handler_router.register(CostAnalysisHandler())

        self._system_prompt_cache: dict[str | None, str] = {}

        # Freeze structural attributes after init — Worker must not modify its own
        # agent identity or mode registry at runtime (ADR-0061 scope enforcement).
        self._config_frozen = True

    def _get_researcher(self) -> MultiModeAgent:
        """Return the internal researcher delegate, creating it on first use."""
        if self._researcher is None:
            from vetinari.agents.consolidated.researcher_agent import ConsolidatedResearcherAgent

            self._researcher = ConsolidatedResearcherAgent(self._research_config)
            self._group_delegates["research"] = self._researcher
        return self._researcher

    def __setattr__(self, name: str, value: object) -> None:
        """Prevent modification of frozen structural attributes after __init__.

        Raises:
            CapabilityNotAvailable: If attempting to modify a frozen attribute
                (agent_type, MODES, DEFAULT_MODE, MODE_KEYWORDS) after init.
        """
        if getattr(self, "_config_frozen", False) and name in _WORKER_FROZEN_ATTRS:
            raise CapabilityNotAvailable(
                f"Worker cannot modify structural attribute {name!r} after initialization — "
                f"frozen attrs: {sorted(_WORKER_FROZEN_ATTRS)}"
            )
        super().__setattr__(name, value)

    # ── Dispatch methods ─────────────────────────────────────────────
    # Each dispatch method routes to the correct internal agent's handler
    # based on the current mode. This preserves all existing behavior.

    def _dispatch_research(self, task: AgentTask) -> AgentResult:
        """Dispatch to the internal Researcher agent for the current mode.

        Args:
            task: The agent task to execute.

        Returns:
            Result from the Researcher agent's mode handler.
        """
        _authorize_worker_action(AgentAction.WEB_SEARCH)
        task.prompt = sanitize_prompt(task.prompt)
        mode = self._current_mode
        researcher = self._get_researcher()
        handler_name = researcher.MODES[mode]
        handler = getattr(researcher, handler_name)
        return handler(task)

    def _dispatch_oracle(self, task: AgentTask) -> AgentResult:
        """Dispatch to the internal Oracle agent for the current mode.

        Injects prior ADR decisions into the task context so the Oracle can
        reference past architectural choices and avoid contradictions (WO-10).

        Args:
            task: The agent task to execute.

        Returns:
            Result from the Oracle agent's mode handler.
        """
        _authorize_worker_action(AgentAction.READ_MEMORY)
        task.prompt = sanitize_prompt(task.prompt)
        # Wire WO-10: inject prior ADR decisions so Oracle has architectural context
        try:
            _prior_adrs = _get_adr_system_class().get_instance().list_adrs(limit=20)
            if _prior_adrs:
                _adr_summaries = [
                    {
                        "adr_id": adr.adr_id,
                        "title": adr.title,
                        "status": adr.status,
                        "category": adr.category,
                        "decision": adr.decision[:200] if adr.decision else "",
                    }
                    for adr in _prior_adrs[:20]  # Cap at 20 most recent
                ]
                task.context["prior_adrs"] = _adr_summaries
                logger.debug(
                    "[Worker] Injected %d prior ADR(s) into Oracle context",
                    len(_adr_summaries),
                )
        except Exception:
            logger.warning("ADR lookup for Oracle context failed (non-fatal)", exc_info=True)

        mode = self._current_mode
        handler_name = self._oracle.MODES[mode]
        handler = getattr(self._oracle, handler_name)
        return handler(task)

    def _dispatch_builder(self, task: AgentTask) -> AgentResult:
        """Dispatch to the internal Builder agent for the current mode.

        Args:
            task: The agent task to execute.

        Returns:
            Result from the Builder agent's mode handler.
        """
        _authorize_worker_action(AgentAction.WRITE_FILE)
        task.prompt = sanitize_prompt(task.prompt)
        sandbox_result = execute_worker_code_in_configured_sandbox(task)
        if sandbox_result is not None:
            return sandbox_result

        mode = self._current_mode
        handler_name = self._builder.MODES[mode]
        handler = getattr(self._builder, handler_name)
        return handler(task)

    def _dispatch_operations(self, task: AgentTask) -> AgentResult:
        """Dispatch to the appropriate handler or the internal Operations agent.

        Checks the handler router first; if a registered handler exists for the
        current mode it is invoked directly with an inference context.  Modes
        not covered by a handler fall through to the delegate OperationsAgent.

        Args:
            task: The agent task to execute.

        Returns:
            Result from the handler or the Operations agent's mode handler.
        """
        _authorize_worker_action(AgentAction.EXECUTE_CODE)
        task.prompt = sanitize_prompt(task.prompt)
        mode = self._current_mode
        handler = self._handler_router.get_handler(mode)
        if handler is not None:
            context = self._operations.infer_context
            return handler.execute(task, context)
        handler_name = self._operations.MODES[mode]
        method = getattr(self._operations, handler_name)
        return method(task)

    # ── System prompt generation ─────────────────────────────────────

    def _get_base_system_prompt(self) -> str:
        return _WORKER_BASE_SYSTEM_PROMPT

    def _get_mode_system_prompt(self, mode: str) -> str:
        """Return a mode-specific prompt by delegating to the internal agent.

        Args:
            mode: The active mode name.

        Returns:
            Mode-specific system prompt from the appropriate delegate.
        """
        group = MODE_GROUPS.get(mode, "build")
        delegate = self._get_researcher() if group == "research" else self._group_delegates.get(group)
        if delegate and hasattr(delegate, "_get_mode_system_prompt"):
            return delegate._get_mode_system_prompt(mode)
        return ""

    def get_system_prompt(self) -> str:
        """Build the full system prompt including base, mode-specific, and MCP tool sections.

        Appends a section listing available external MCP tools when any are
        registered in the bridge, so the underlying LLM knows which namespaced
        tools it can request via ``invoke_mcp_tool()``.

        Returns:
            Combined system prompt for the current execution context.
        """
        mode = self._current_mode
        prompt_cache = getattr(self, "_system_prompt_cache", None)
        if prompt_cache is None:
            prompt_cache = {}
            self._system_prompt_cache = prompt_cache
        cached_prompt = prompt_cache.get(mode)
        if cached_prompt is not None:
            return cached_prompt

        base = self._get_base_system_prompt()
        parts = [base]

        if mode:
            mode_prompt = self._get_mode_system_prompt(mode)
            if mode_prompt:
                parts.append(mode_prompt)

        # Inject external MCP tools section when tools are available.
        # _get_mcp_tools_fn() caches the callable after the first call so
        # this function does not re-import on every prompt build (VET130).
        try:
            mcp_tools = _get_mcp_tools_fn()()
            if mcp_tools:
                tool_lines = "\n".join(f"  - {t['name']}: {t.get('description', '')}" for t in mcp_tools)
                parts.append(f"External MCP tools available (invoke via invoke_mcp_tool):\n{tool_lines}")
        except Exception:
            logger.info("MCP tool injection skipped — bridge unavailable", exc_info=True)

        prompt = "\n\n".join(parts)
        prompt_cache[mode] = prompt
        return prompt

    def verify(self, output: Any) -> VerificationResult:
        """Verify Worker output by delegating to the mode group's verifier.

        Args:
            output: The output to verify (matches BaseAgent.verify contract).

        Returns:
            Verification result from the appropriate delegate.
        """
        mode = self._current_mode or self.DEFAULT_MODE
        group = MODE_GROUPS.get(mode, "build")
        delegate = self._get_researcher() if group == "research" else self._group_delegates.get(group)
        if delegate:
            return delegate.verify(output)
        return VerificationResult(
            passed=False,
            score=0.0,
            issues=[{"message": "No delegate verifier for mode group"}],
        )

    @property
    def mode_group(self) -> str:
        if self._current_mode:
            return MODE_GROUPS.get(self._current_mode, "build")
        return "build"

    def can_write(self) -> bool:
        """Return whether the active Worker mode permits mutating actions."""
        return self.mode_group not in _READ_ONLY_MODE_GROUPS

    def allowed_actions(self) -> list[str]:
        """Return coarse-grained actions permitted in the active Worker mode.

        Returns:
            The high-level action names available to the current Worker mode.
        """
        actions = ["read", "infer"]
        if self._current_mode and self.mode_group == "research":
            actions.append("web_access")
        if self.can_write():
            actions.extend(["write", "delete"])
        return actions


def get_worker_agent(config: dict[str, Any] | None = None) -> WorkerAgent:
    """Factory function to create a WorkerAgent.

    Args:
        config: Optional configuration dict.

    Returns:
        A configured WorkerAgent instance.
    """
    return WorkerAgent(config)
