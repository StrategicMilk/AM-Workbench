"""Consolidated Oracle Agent (Phase 3).

====================================
Replaces: ORACLE + PONDER

Modes:
- architecture: Architecture decisions and design guidance
- risk_assessment: Risk analysis with likelihood/impact scoring
- ontological_analysis: Deep conceptual analysis
- contrarian_review: Challenge assumptions, find blind spots
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.agents.consolidated.oracle_prompts import ORACLE_MODE_PROMPTS
from vetinari.agents.contracts import AgentResult, AgentTask, VerificationResult
from vetinari.agents.multi_mode_agent import MultiModeAgent
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


class ConsolidatedOracleAgent(MultiModeAgent):
    """Unified oracle agent for architecture decisions, risk assessment, and deep deliberation."""

    MODES = {
        "architecture": "_execute_architecture",
        "risk_assessment": "_execute_risk_assessment",
        "ontological_analysis": "_execute_ontological",
        "contrarian_review": "_execute_contrarian",
        "suggest": "_execute_suggest",
    }
    DEFAULT_MODE = "architecture"
    MODE_KEYWORDS = {
        "architecture": ["architect", "design", "structure", "pattern", "component", "module"],
        "risk_assessment": ["risk", "vulnerab", "threat", "impact", "likelihood", "mitigat"],
        "ontological_analysis": ["ontolog", "concept", "fundament", "deep analy", "ponder", "deliberat", "reflect"],
        "contrarian_review": ["contrarian", "challenge", "assumption", "blind spot", "devil", "critique"],
        "suggest": ["suggest", "improve", "enhancement", "recommendation", "creative"],
    }

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(AgentType.WORKER, config)

    def _get_base_system_prompt(self) -> str:
        return (
            "You are Vetinari's Oracle — the system's strategic advisor. "
            "You provide architecture guidance, risk assessment, deep analysis, "
            "and contrarian perspectives to improve decision quality."
        )

    def _get_mode_system_prompt(self, mode: str) -> str:
        return ORACLE_MODE_PROMPTS.get(mode, "")

    def verify(self, output: Any) -> VerificationResult:
        """Verify.

        Returns:
            The VerificationResult result.
        """
        if output is None:
            return VerificationResult(passed=False, issues=[{"message": "No output"}], score=0.0)
        if isinstance(output, dict):
            analysis = output.get("analysis")
            analysis_has_depth = isinstance(analysis, dict) and any(
                value for key, value in analysis.items() if key != "summary"
            )
            has_substance = bool(
                analysis_has_depth or output.get("recommendations") or output.get("risks") or output.get("insights"),
            )
            return VerificationResult(passed=has_substance, score=0.8 if has_substance else 0.3)
        return VerificationResult(
            passed=False,
            issues=[{"message": "No structured verification output"}],
            score=0.0,
        )

    def _execute_architecture(self, task: AgentTask) -> AgentResult:
        subject = task.context.get("subject", task.description)
        prompt = (
            f"Provide architectural analysis and guidance for:\n{subject}\n\n"
            "Respond as JSON:\n"
            '{"analysis": {"summary": "...", "key_decisions": [...]}, '
            '"recommendations": [{"recommendation": "...", "rationale": "...", "priority": "high|medium|low"}], '
            '"design_patterns": [...], "tradeoffs": [{"option": "...", "pros": [...], "cons": [...]}]}'
        )
        result = self._infer_json(prompt, fallback={"analysis": {"summary": subject}, "recommendations": []})
        return AgentResult(success=True, output=result, metadata={"mode": "architecture"})

    def _execute_risk_assessment(self, task: AgentTask) -> AgentResult:
        subject = task.context.get("subject", task.description)
        prompt = (
            f"Assess risks for:\n{subject}\n\n"
            "Respond as JSON:\n"
            '{"risks": [{"risk": "...", "likelihood": 3, "impact": 4, '
            '"composite_score": 12, "mitigation": "...", "category": "technical|operational|security"}], '
            '"overall_risk_level": "high|medium|low", '
            '"recommendations": [...]}'
        )
        result = self._infer_json(prompt, fallback={"risks": [], "overall_risk_level": "unknown"})
        return AgentResult(success=True, output=result, metadata={"mode": "risk_assessment"})

    def _execute_ontological(self, task: AgentTask) -> AgentResult:
        subject = task.context.get("subject", task.description)
        prompt = (
            f"Conduct deep ontological analysis of:\n{subject}\n\n"
            "Examine first principles, foundational assumptions, and conceptual foundations.\n\n"
            "Respond as JSON:\n"
            '{"insights": [{"insight": "...", "depth": "surface|structural|foundational", '
            '"confidence": 0.8}], "assumptions_challenged": [...], '
            '"first_principles": [...], "synthesis": "..."}'
        )
        result = self._infer_json(prompt, fallback={"insights": [], "synthesis": ""})
        return AgentResult(success=True, output=result, metadata={"mode": "ontological_analysis"})

    def _execute_contrarian(self, task: AgentTask) -> AgentResult:
        subject = task.context.get("subject", task.description)
        prompt = (
            f"Provide a contrarian review of:\n{subject}\n\n"
            "Challenge assumptions, identify blind spots, propose alternatives.\n\n"
            "Respond as JSON:\n"
            '{"challenges": [{"assumption": "...", "challenge": "...", "severity": "high|medium|low"}], '
            '"blind_spots": [...], "alternatives": [{"approach": "...", "rationale": "...", "feasibility": 0.7}], '
            '"recommendations": [...]}'
        )
        result = self._infer_json(prompt, fallback={"challenges": [], "blind_spots": []})
        return AgentResult(success=True, output=result, metadata={"mode": "contrarian_review"})

    # ── Suggestion rules (absorbed from ArchitectAgent) ────────────────────────
    # Each rule: (exclude_keywords, require_keywords, suggestion_dict)
    _SUGGESTION_RULES: list[tuple[tuple[str, ...], tuple[str, ...], dict[str, str]]] = [
        (
            ("test",),
            (),
            {
                "title": "Add comprehensive test coverage",
                "rationale": "No testing mentioned — tests catch regressions early",
                "expected_impact": "high",
                "effort": "medium",
                "category": "testing",
            },
        ),
        (
            ("security", "auth"),
            (),
            {
                "title": "Security review pass",
                "rationale": "No explicit security requirements — proactive auditing prevents vulnerabilities",
                "expected_impact": "high",
                "effort": "low",
                "category": "security",
            },
        ),
        (
            ("doc", "readme"),
            (),
            {
                "title": "Add API documentation",
                "rationale": "Documentation helps future maintainers and API consumers",
                "expected_impact": "medium",
                "effort": "low",
                "category": "documentation",
            },
        ),
        (
            (),
            ("api", "web", "server", "endpoint"),
            {
                "title": "Add rate limiting and input validation",
                "rationale": "Public-facing APIs need protection against abuse",
                "expected_impact": "high",
                "effort": "medium",
                "category": "security",
            },
        ),
        (
            (),
            ("database", "sql", "data"),
            {
                "title": "Add database migration strategy",
                "rationale": "Schema changes need a forward migration path",
                "expected_impact": "medium",
                "effort": "medium",
                "category": "architecture",
            },
        ),
    ]

    _DEFAULT_SUGGESTION: dict[str, str] = {
        "title": "Add error handling and logging",
        "rationale": "Structured logging and error recovery improve reliability",
        "expected_impact": "medium",
        "effort": "low",
        "category": "architecture",
    }

    def _fallback_suggestions(self, goal: str) -> list[dict[str, str]]:
        """Generate rule-based improvement suggestions without LLM inference.

        Uses keyword matching against the goal to surface relevant suggestions
        for testing, security, documentation, and architecture.

        Args:
            goal: The project goal text to match suggestions against.

        Returns:
            Up to 5 relevant suggestion dicts, or a single default suggestion.
        """
        goal_lower = goal.lower()
        suggestions = []
        for exclude_kws, require_kws, suggestion in self._SUGGESTION_RULES:
            if exclude_kws and any(kw in goal_lower for kw in exclude_kws):
                continue
            if require_kws and not any(kw in goal_lower for kw in require_kws):
                continue
            suggestions.append(suggestion)

        return suggestions[:5] if suggestions else [self._DEFAULT_SUGGESTION]

    def _execute_suggest(self, task: AgentTask) -> AgentResult:
        """Generate creative improvement suggestions based on project context.

        Called at strategic insertion points (pre-decomposition, mid-execution,
        post-execution) to propose enhancements the user might want.

        Args:
            task: The agent task containing goal and context.

        Returns:
            AgentResult with a list of filtered suggestions.
        """
        goal = task.prompt or task.description
        context = task.context or {}
        completed_outputs = context.get("completed_outputs", [])

        prompt = (
            "Based on this project context, suggest 3-5 improvements:\n\n"
            f"Goal: {goal}\n\n"
            f"{('Completed so far: ' + ', '.join(str(o)[:100] for o in completed_outputs[:5])) if completed_outputs else ''}.\n\n"
            "For each suggestion provide:\n"
            "- title: Short name\n"
            "- rationale: Why this improvement matters\n"
            "- expected_impact: What benefit it brings (low/medium/high)\n"
            "- effort: Implementation effort (low/medium/high)\n"
            '- category: One of "performance", "security", "ux", "architecture", "testing", "documentation"\n\n'
            "Return as JSON array. Only suggest improvements with confidence > 0.7.\n"
            "Avoid generic advice — be specific to this project."
        )

        suggestions = self._infer_json(prompt)
        if not suggestions or not isinstance(suggestions, list):
            suggestions = self._fallback_suggestions(goal)

        filtered = [s for s in suggestions if isinstance(s, dict) and s.get("expected_impact") != "low"][:5]

        return AgentResult(
            success=True,
            output={"suggestions": filtered, "insertion_point": context.get("insertion_point", "post_execution")},
            metadata={"mode": "suggest", "suggestion_count": len(filtered)},
        )

    def get_capabilities(self) -> list[str]:
        """Return capability strings describing this agent's supported modes and features.

        Returns:
            List of capability identifiers such as architecture decisions,
            risk assessment, and first-principles thinking.
        """
        return [
            "architecture_decision",
            "design_pattern_recommendation",
            "risk_assessment",
            "risk_mitigation",
            "ontological_analysis",
            "first_principles_thinking",
            "contrarian_review",
            "assumption_challenging",
            "suggest_improvements",
        ]
