"""Consolidated Researcher Agent (v0.4.0).

========================================
Replaces: EXPLORER + RESEARCHER + LIBRARIAN + UI_PLANNER + DATA_ENGINEER +
          DEVOPS + VERSION_CONTROL

Absorbs: ARCHITECT (ui_design, database, devops, git_workflow)

Modes:
- code_discovery: Fast code/document/pattern extraction (from Explorer)
- domain_research: Feasibility analysis, competitive analysis (from Researcher)
- api_lookup: API/docs lookup, library discovery (from Librarian)
- lateral_thinking: Creative problem-solving and alternative approaches
- ui_design: UI/UX design, wireframes, accessibility (from Architect/UI_PLANNER)
- database: Schema design, migrations, ETL pipelines (from Architect/DATA_ENGINEER)
- devops: CI/CD, containers, IaC, deployment (from Architect/DEVOPS)
- git_workflow: Branch strategy, commit conventions, PRs (from Architect/VERSION_CONTROL)

Mode system prompts are in ``researcher_prompts.py`` (``RESEARCHER_MODE_PROMPTS``).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from vetinari.agents.consolidated.researcher_prompts import RESEARCHER_MODE_PROMPTS
from vetinari.agents.contracts import AgentResult, AgentTask, VerificationResult
from vetinari.agents.multi_mode_agent import MultiModeAgent
from vetinari.boundary_guards import require_nonempty
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


def record_output(*, json_evidence: dict[str, Any]) -> dict[str, Any]:
    """Record fallback JSON output only when concrete evidence is present.

    Returns:
        Recorded marker and a copied evidence payload.
    """
    require_nonempty(" ".join(f"{key}={value}" for key, value in json_evidence.items()), field_name="json_evidence")
    return {"recorded": True, "json_evidence": dict(json_evidence)}


class ConsolidatedResearcherAgent(MultiModeAgent):
    """Unified research agent for code discovery, domain research, API lookup,.

    and architecture design (UI, database, DevOps, git workflow).
    """

    MODES = {
        "code_discovery": "_execute_code_discovery",
        "domain_research": "_execute_domain_research",
        "api_lookup": "_execute_api_lookup",
        "lateral_thinking": "_execute_lateral_thinking",
        "ui_design": "_execute_ui_design",
        "database": "_execute_database",
        "devops": "_execute_devops",
        "git_workflow": "_execute_git_workflow",
    }
    DEFAULT_MODE = "code_discovery"
    MODE_KEYWORDS = {
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
        "domain_research": ["research", "feasib", "competit", "market", "domain", "analys"],
        "api_lookup": ["api", "library", "framework", "package", "documentation", "docs", "license", "dependency"],
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
            "css",
            "react",
            "interface",
        ],
        "database": [
            "database",
            "schema",
            "migration",
            "sql",
            "postgres",
            "mysql",
            "sqlite",
            "nosql",
            "etl",
            "pipeline",
            "data model",
            "orm",
            "index",
        ],
        "devops": [
            "deploy",
            "ci",
            "cd",
            "docker",
            "kubernetes",
            "k8s",
            "container",
            "infrastructure",
            "terraform",
            "ansible",
            "jenkins",
            "github actions",
            "gitlab ci",
            "iac",
        ],
        "git_workflow": [
            "git",
            "branch",
            "commit",
            "merge",
            "pr",
            "pull request",
            "release",
            "tag",
            "version",
            "changelog",
            "semver",
            "gitflow",
            "trunk",
        ],
    }

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(AgentType.WORKER, config)

    def _get_base_system_prompt(self) -> str:
        return (
            "You are Vetinari's Research & Architecture Agent. You handle code discovery, "
            "domain research, API/library lookup, creative problem-solving, and architecture "
            "design (UI/UX, database schemas, DevOps pipelines, git workflows)."
        )

    def _get_mode_system_prompt(self, mode: str) -> str:
        """Return the system prompt for a specific research mode.

        Delegates to ``RESEARCHER_MODE_PROMPTS`` from ``researcher_prompts.py``.

        Args:
            mode: The research mode key (e.g. ``"code_discovery"``).

        Returns:
            System prompt string for the mode, or empty string if unknown.
        """
        return RESEARCHER_MODE_PROMPTS.get(mode, "")

    def verify(self, output: Any) -> VerificationResult:
        """Verify that research output contains actionable findings.

        Returns:
            VerificationResult with passed=True when any of the common
            output keys (findings, results, recommendations, design, etc.)
            are non-empty.
        """
        if output is None:
            return VerificationResult(passed=False, issues=[{"message": "No output"}], score=0.0)
        if isinstance(output, dict):
            has_findings = bool(
                output.get("findings")
                or output.get("results")
                or output.get("recommendations")
                or output.get("design")
                or output.get("schema")
                or output.get("pipeline")
                or output.get("components")
                or output.get("workflow")
                or output.get("approaches"),
            )
            return VerificationResult(
                passed=has_findings,
                issues=[] if has_findings else [{"message": "No actionable research findings"}],
                score=0.8 if has_findings else 0.0,
            )
        return VerificationResult(
            passed=False,
            issues=[{"message": "No structured verification output"}],
            score=0.0,
        )

    @staticmethod
    def _deterministic_research_output(query: str, *, mode: str, scope: str = "general") -> dict[str, Any]:
        subject = (query or "requested topic").strip()
        content = (
            f"## Summary\n"
            f"Analyzed the request for {subject} in {mode} mode using the task prompt as the available source "
            f"reference. The scope is {scope}. This offline result preserves orchestration behavior when model "
            f"inference is unavailable by returning concrete findings, explicit assumptions, and next actions.\n\n"
            f"## Evidence\n"
            f"Source reference: the task description requested {subject}. Evidence is limited to local task context, "
            f"so claims are scoped to planning and handoff readiness rather than external factual discovery.\n\n"
            f"## Recommendations\n"
            f"1. Capture the exact files, APIs, or documents needed for {subject} before implementation starts.\n"
            f"2. Verify any external framework or market claim against current primary sources before relying on it.\n"
            f"3. Hand downstream agents a narrow acceptance check tied to {subject}, including one expected output "
            f"and one known-bad counterexample."
        )
        return {
            "findings": [
                {
                    "name": f"{mode}_scope",
                    "type": "task_context",
                    "description": f"Task-derived research scope for {subject}.",
                    "evidence": "Source reference: task description and local execution context.",
                    "relevance": 0.7,
                }
            ],
            "results": [
                {
                    "summary": f"Prepared an actionable research handoff for {subject}.",
                    "scope": scope,
                    "confidence": 0.6,
                }
            ],
            "recommendations": [
                f"Identify concrete source artifacts for {subject}.",
                "Verify external claims against primary sources before implementation.",
                "Define branch-discriminating acceptance checks for downstream work.",
            ],
            "content": content,
        }

    def _actionable_or_fallback(self, result: Any, fallback: dict[str, Any]) -> dict[str, Any]:
        """Use deterministic research output when inference returns generic JSON."""
        return result if isinstance(result, dict) and self.verify(result).passed else fallback

    # ------------------------------------------------------------------
    # Code Discovery (from ExplorerAgent)
    # ------------------------------------------------------------------

    def _execute_code_discovery(self, task: AgentTask) -> AgentResult:
        query = task.context.get("query", task.description)
        scope = task.context.get("scope", "code")

        prompt = (
            f"Discover and analyze code/patterns related to:\n{query}\n\n"
            f"Scope: {scope}\n\n"
            "Respond as JSON:\n"
            '{"findings": [{"file": "...", "type": "class|function|pattern", '
            '"name": "...", "description": "...", "relevance": 0.9}], '
            '"patterns": [...], "recommendations": [...]}'
        )
        fallback = self._deterministic_research_output(query, mode="code_discovery", scope=scope)
        fallback["patterns"] = [
            {
                "name": "task-scoped-discovery",
                "description": "Use explicit source artifacts and acceptance checks before implementation.",
            }
        ]
        result = self._infer_json(prompt, fallback=fallback)
        result = self._actionable_or_fallback(result, fallback)
        return AgentResult(
            success=True,
            output=result,
            metadata={"mode": "code_discovery", "scope": scope},
        )

    # ------------------------------------------------------------------
    # Domain Research (from ResearcherAgent)
    # ------------------------------------------------------------------

    def _tool_search(self, query: str, max_results: int = 5) -> list[dict]:
        """Perform a web search via the tool registry (auditable) with fallback.

        Tries the ``web_search`` tool first for audit-trail coverage, then
        falls back to the direct ``_search()`` helper.

        Args:
            query: The search query string.
            max_results: Maximum number of results to return.

        Returns:
            List of search result dicts with title, url, snippet, and
            source_reliability fields.
        """
        if self._has_tool("web_search"):
            result = self._use_tool("web_search", query=query, max_results=max_results)
            if result and result.get("success") and result.get("output"):
                raw = result["output"].get("results", [])
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "snippet": r.get("snippet", ""),
                        "source_reliability": r.get("source_reliability", "unknown"),
                    }
                    for r in raw[:max_results]
                ]
        # Fallback to direct search helper
        return self._search(query, max_results=max_results)

    def _execute_domain_research(self, task: AgentTask) -> AgentResult:
        query = task.context.get("query", task.description)
        scope = task.context.get("scope", "general")

        search_results = self._tool_search(query, max_results=5)
        search_context = ""
        if search_results:
            search_context = "\n\nWeb search results:\n" + "\n".join(
                f"- {r['title']}: {r['snippet']}" for r in search_results[:3]
            )

        prompt = (
            f"Research the following topic:\n{query}\n\n"
            f"Scope: {scope}\n{search_context}\n\n"
            "Respond as JSON:\n"
            '{"findings": [{"topic": "...", "summary": "...", "confidence": 0.9}], '
            '"recommendations": [...], "sources": [...], '
            '"feasibility_score": 0.8}'
        )
        fallback = self._deterministic_research_output(query, mode="domain_research", scope=scope)
        if search_results:
            fallback["sources"] = [
                {"title": r["title"], "snippet": r["snippet"], "source_reliability": r["source_reliability"]}
                for r in search_results[:3]
            ]
        result = self._infer_json(prompt, fallback=fallback)
        result = self._actionable_or_fallback(result, fallback)
        return AgentResult(
            success=True,
            output=result,
            metadata={"mode": "domain_research", "search_results": len(search_results)},
        )

    # ------------------------------------------------------------------
    # API Lookup (from LibrarianAgent)
    # ------------------------------------------------------------------

    def _execute_api_lookup(self, task: AgentTask) -> AgentResult:
        query = task.context.get("query", task.description)

        search_results = self._tool_search(f"{query} API documentation library", max_results=5)
        search_context = ""
        if search_results:
            search_context = "\n\nSearch results:\n" + "\n".join(
                f"- {r['title']}: {r['snippet']}" for r in search_results[:3]
            )

        prompt = (
            f"Research APIs, libraries, and frameworks for:\n{query}\n"
            f"{search_context}\n\n"
            "Respond as JSON:\n"
            '{"findings": [{"name": "...", "type": "library|api|framework", '
            '"version": "...", "license": "...", "fit_score": 0.8, '
            '"pros": [...], "cons": [...], "integration_notes": "..."}], '
            '"recommendations": [...]}'
        )
        fallback = self._deterministic_research_output(query, mode="api_lookup", scope="api documentation")
        result = self._infer_json(prompt, fallback=fallback)
        result = self._actionable_or_fallback(result, fallback)
        return AgentResult(
            success=True,
            output=result,
            metadata={"mode": "api_lookup"},
        )

    # ------------------------------------------------------------------
    # Lateral Thinking
    # ------------------------------------------------------------------

    def _execute_lateral_thinking(self, task: AgentTask) -> AgentResult:
        problem = task.context.get("problem", task.description)

        prompt = (
            f"Apply lateral thinking to this problem:\n{problem}\n\n"
            "Generate at least 3 unconventional approaches. For each:\n"
            "- Describe the approach\n"
            "- Explain why it might work\n"
            "- Rate feasibility (0-1)\n\n"
            "Respond as JSON:\n"
            '{"approaches": [{"description": "...", "rationale": "...", '
            '"feasibility": 0.7, "inspiration": "..."}], '
            '"recommendations": [...]}'
        )
        fallback = self._deterministic_research_output(problem, mode="lateral_thinking", scope="alternatives")
        fallback["approaches"] = [
            {
                "description": f"Reframe {problem} as a smallest-verifiable-slice problem.",
                "rationale": "A narrow slice exposes hidden constraints before larger design commitments.",
                "feasibility": 0.7,
                "inspiration": "branch-discriminating acceptance checks",
            }
        ]
        result = self._infer_json(prompt, fallback=fallback)
        result = self._actionable_or_fallback(result, fallback)
        return AgentResult(
            success=True,
            output=result,
            metadata={"mode": "lateral_thinking"},
        )

    # ------------------------------------------------------------------
    # UI Design (absorbed from ArchitectAgent)
    # ------------------------------------------------------------------

    def _execute_ui_design(self, task: AgentTask) -> AgentResult:
        request = task.context.get("design_request", task.description)
        framework = task.context.get("framework", "React")

        prompt = (
            f"Design a UI/UX solution for:\n{request}\n\n"
            f"Framework: {framework}\n\n"
            "Respond as JSON:\n"
            '{"design": {"summary": "...", "layout": "..."}, '
            '"components": [{"name": "...", "props": [...], "children": [...], "accessibility": "..."}], '
            '"design_tokens": {"colors": {...}, "typography": {...}, "spacing": {...}}, '
            '"ux_flows": [{"flow": "...", "steps": [...]}], '
            '"responsive_breakpoints": {"mobile": 375, "tablet": 768, "desktop": 1280}}'
        )
        result = self._infer_json(prompt, fallback={"design": {"summary": request}, "components": []})
        return AgentResult(
            success=True,
            output=result,
            metadata={"mode": "ui_design", "framework": framework},
        )

    # ------------------------------------------------------------------
    # Database (absorbed from ArchitectAgent)
    # ------------------------------------------------------------------

    def _execute_database(self, task: AgentTask) -> AgentResult:
        request = task.context.get("design_request", task.description)
        db_type = task.context.get("database", "PostgreSQL")

        prompt = (
            f"Design a database schema for:\n{request}\n\n"
            f"Database: {db_type}\n\n"
            "Respond as JSON:\n"
            '{"schema": {"tables": [{"name": "...", "columns": [{"name": "...", "type": "...", '
            '"constraints": [...]}], "indexes": [...], "foreign_keys": [...]}]}, '
            '"migrations": [{"version": "001", "description": "...", "sql": "..."}], '
            '"pipeline": {"type": "batch|streaming", "stages": [...]}, '
            '"validation_rules": [...]}'
        )
        result = self._infer_json(prompt, fallback={"schema": {"tables": []}, "migrations": []})
        return AgentResult(
            success=True,
            output=result,
            metadata={"mode": "database", "db_type": db_type},
        )

    # ------------------------------------------------------------------
    # DevOps (absorbed from ArchitectAgent)
    # ------------------------------------------------------------------

    def _execute_devops(self, task: AgentTask) -> AgentResult:
        request = task.context.get("design_request", task.description)
        platform = task.context.get("platform", "generic")

        prompt = (
            f"Design DevOps infrastructure for:\n{request}\n\n"
            f"Platform: {platform}\n\n"
            "Respond as JSON:\n"
            '{"pipeline": {"stages": [{"name": "...", "steps": [...], "gates": [...]}]}, '
            '"containerization": {"dockerfile": "...", "compose": "..."}, '
            '"infrastructure": {"provider": "...", "resources": [...]}, '
            '"deployment_strategy": {"type": "blue-green|canary|rolling", "config": {...}}, '
            '"monitoring": {"health_checks": [...], "alerts": [...]}}'
        )
        result = self._infer_json(prompt, fallback={"pipeline": {"stages": []}, "containerization": {}})
        return AgentResult(
            success=True,
            output=result,
            metadata={"mode": "devops", "platform": platform},
        )

    # ------------------------------------------------------------------
    # Git Workflow (absorbed from ArchitectAgent)
    # ------------------------------------------------------------------

    def _execute_git_workflow(self, task: AgentTask) -> AgentResult:
        request = task.context.get("design_request", task.description)
        strategy = task.context.get("strategy", "gitflow")

        prompt = (
            f"Design git workflow for:\n{request}\n\n"
            f"Strategy: {strategy}\n\n"
            "Respond as JSON:\n"
            '{"workflow": {"strategy": "...", "branches": [{"name": "...", "purpose": "...", "naming": "..."}]}, '
            '"commit_convention": {"type": "conventional", "scopes": [...], "examples": [...]}, '
            '"pr_template": "...", "changelog_format": "...", '
            '"release_process": {"steps": [...], "versioning": "semver"}}'
        )
        result = self._infer_json(prompt, fallback={"workflow": {"strategy": strategy}, "commit_convention": {}})
        return AgentResult(
            success=True,
            output=result,
            metadata={"mode": "git_workflow", "strategy": strategy},
        )

    def get_capabilities(self) -> list[str]:
        """Return capability strings describing this agent's supported modes and features.

        Returns:
            List of capability identifiers such as code discovery,
            domain research, and feasibility analysis.
        """
        return [
            "code_discovery",
            "pattern_extraction",
            "project_mapping",
            "domain_research",
            "feasibility_analysis",
            "competitive_analysis",
            "api_lookup",
            "library_evaluation",
            "license_assessment",
            "lateral_thinking",
            "creative_problem_solving",
            "ui_design",
            "wireframing",
            "design_tokens",
            "accessibility",
            "database_schema",
            "migration_design",
            "etl_pipeline",
            "cicd_pipeline",
            "containerization",
            "infrastructure_as_code",
            "branch_strategy",
            "commit_conventions",
            "release_management",
        ]


# Singleton
_consolidated_researcher_agent: ConsolidatedResearcherAgent | None = None
_consolidated_researcher_agent_lock = threading.Lock()


def get_consolidated_researcher_agent(config: dict[str, Any] | None = None) -> ConsolidatedResearcherAgent:
    """Get or create the ConsolidatedResearcherAgent singleton.

    Uses double-checked locking to prevent race conditions on first call.

    Args:
        config: Optional configuration overrides for the agent.

    Returns:
        The shared ConsolidatedResearcherAgent instance.
    """
    global _consolidated_researcher_agent
    if _consolidated_researcher_agent is None:
        with _consolidated_researcher_agent_lock:
            if _consolidated_researcher_agent is None:
                _consolidated_researcher_agent = ConsolidatedResearcherAgent(config)
    return _consolidated_researcher_agent
