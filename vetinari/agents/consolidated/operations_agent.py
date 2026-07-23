"""Consolidated Operations Agent (v0.4.0).

Unifies documentation, synthesis, cost analysis, experiments, recovery,
improvement, monitoring, and DevOps modes behind one Worker agent.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from vetinari.agents.consolidated.operations_prompts import OPERATIONS_MODE_PROMPTS
from vetinari.agents.contracts import AgentResult, AgentTask, VerificationResult
from vetinari.agents.multi_mode_agent import MultiModeAgent
from vetinari.boundary_guards import require_nonempty
from vetinari.constants import TRUNCATE_CONTENT_ANALYSIS, TRUNCATE_OUTPUT_PREVIEW
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error pattern registry (preserved from ErrorRecoveryAgent)
# ---------------------------------------------------------------------------

_ERROR_PATTERNS: dict[str, dict[str, Any]] = {
    "connection_refused": {
        "patterns": [r"ConnectionRefusedError", r"Connection refused", r"ECONNREFUSED"],
        "category": "network",
        "severity": "high",
        "quick_fix": "Check if the target service is running and accessible",
    },
    "timeout": {
        "patterns": [r"TimeoutError", r"timed? ?out", r"deadline exceeded"],
        "category": "network",
        "severity": "medium",
        "quick_fix": "Increase timeout or check network connectivity",
    },
    "rate_limit": {
        "patterns": [r"429", r"rate.?limit", r"too many requests", r"quota exceeded"],
        "category": "api",
        "severity": "medium",
        "quick_fix": "Implement exponential backoff or request queuing",
    },
    "out_of_memory": {
        "patterns": [r"MemoryError", r"OOM", r"out of memory", r"ENOMEM"],
        "category": "resource",
        "severity": "critical",
        "quick_fix": "Reduce batch size, use streaming, or increase memory allocation",
    },
    "import_error": {
        "patterns": [r"ImportError", r"ModuleNotFoundError", r"No module named"],
        "category": "dependency",
        "severity": "high",
        "quick_fix": "Install missing package or check virtual environment",
    },
    "permission_denied": {
        "patterns": [r"PermissionError", r"Permission denied", r"EACCES"],
        "category": "filesystem",
        "severity": "high",
        "quick_fix": "Check file/directory permissions and ownership",
    },
    "file_not_found": {
        "patterns": [r"FileNotFoundError", r"No such file", r"ENOENT"],
        "category": "filesystem",
        "severity": "medium",
        "quick_fix": "Verify file path and ensure parent directories exist",
    },
    "json_decode": {
        "patterns": [r"JSONDecodeError", r"json\.decoder", r"Expecting value"],
        "category": "parsing",
        "severity": "medium",
        "quick_fix": "Validate JSON input, check for empty responses or HTML errors",
    },
    "key_error": {
        "patterns": [r"KeyError", r"key not found"],
        "category": "data",
        "severity": "medium",
        "quick_fix": "Use .get() with default values or validate dict keys before access",
    },
    "type_error": {
        "patterns": [r"TypeError", r"not callable", r"expected .* got"],
        "category": "logic",
        "severity": "medium",
        "quick_fix": "Check argument types and function signatures",
    },
}

# Model pricing (preserved from CostPlannerAgent)
_MODEL_PRICING: dict[str, dict[str, float | str]] = {
    "qwen2.5-coder-7b": {"input_per_1k": 0.0001, "output_per_1k": 0.0002, "tier": "small"},
    "qwen2.5-72b": {"input_per_1k": 0.001, "output_per_1k": 0.002, "tier": "large"},
    "qwen3-30b-a3b": {"input_per_1k": 0.0005, "output_per_1k": 0.001, "tier": "medium"},
    "qwen2.5-vl-32b": {"input_per_1k": 0.0005, "output_per_1k": 0.001, "tier": "medium"},
    "claude-opus-4-8": {"input_per_1k": 0.005, "output_per_1k": 0.025, "tier": "premium"},
    "claude-opus-4-7": {"input_per_1k": 0.005, "output_per_1k": 0.025, "tier": "premium"},
    "claude-sonnet-4-6": {"input_per_1k": 0.003, "output_per_1k": 0.015, "tier": "premium"},
    "claude-haiku-4-5-20251001": {"input_per_1k": 0.001, "output_per_1k": 0.005, "tier": "premium"},
    "gpt-4o": {"input_per_1k": 0.005, "output_per_1k": 0.015, "tier": "premium"},
    "gemini-3.5-flash": {"input_per_1k": 0.0015, "output_per_1k": 0.009, "tier": "large"},
    "gemini-3.1-flash-lite": {"input_per_1k": 0.00025, "output_per_1k": 0.0015, "tier": "small"},
}


def _pricing_float(pricing: dict[str, float | str], key: str) -> float:
    return float(pricing[key])


def _pricing_tier(pricing: dict[str, float | str]) -> str:
    return str(pricing["tier"])


def report_health(*, telemetry: dict[str, Any] | None) -> dict[str, Any]:
    """Return a health report only when telemetry is non-empty.

    Returns:
        Status payload containing the copied telemetry values.
    """
    payload = "" if not telemetry else " ".join(f"{key}={value}" for key, value in telemetry.items())
    require_nonempty(payload, field_name="telemetry")
    return {"status": "reported", "telemetry": dict(telemetry)}


class OperationsAgent(MultiModeAgent):
    """Unified operations agent for docs, analysis, recovery, and monitoring."""

    MODES = {
        "documentation": "_execute_documentation",
        "creative_writing": "_execute_creative_writing",
        "cost_analysis": "_execute_cost_analysis",
        "experiment": "_execute_experiment",
        "error_recovery": "_execute_error_recovery",
        "synthesis": "_execute_synthesis",
        "improvement": "_execute_improvement",
        "monitor": "_execute_monitor",
        "devops_ops": "_execute_devops_ops",
    }
    DEFAULT_MODE = "documentation"
    MODE_KEYWORDS = {
        "documentation": ["document", "api doc", "user guide", "changelog", "readme", "reference"],
        "creative_writing": ["creative", "story", "narrative", "prose", "write"],
        "cost_analysis": ["cost", "pricing", "budget", "roi", "expense", "token cost", "model selection"],
        "experiment": ["experiment", "a/b test", "hypothesis", "metric", "telemetry", "variant"],
        "error_recovery": ["error", "failure", "crash", "exception", "retry", "recover", "circuit break"],
        "synthesis": ["synthesiz", "synthesise", "merge", "fusion", "consolidat", "combin"],
        "improvement": ["improv", "optimiz", "performance", "bottleneck", "tune", "enhance"],
        "monitor": ["monitor", "status", "health", "performance check", "system check"],
        "devops_ops": ["deploy", "ci/cd", "pipeline", "docker", "infrastructure", "devops"],
    }

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(AgentType.WORKER, config)

    @property
    def infer_context(self) -> dict[str, Any]:
        """Return an inference-context dict for handler delegation."""
        return {"infer_json": self._infer_json}

    def _get_base_system_prompt(self) -> str:
        return (
            "You are Vetinari's Operations Agent — the system's operational backbone\n"
            "responsible for documentation, cost optimisation, experimentation, error recovery,\n"
            "multi-source synthesis, continuous improvement, system monitoring, and DevOps.\n\n"
            "You operate across 9 specialised modes, each with domain-specific expertise.\n"
            "Always produce structured JSON output. Prioritise actionable recommendations\n"
            "over theoretical analysis. Include concrete numbers, code snippets, and\n"
            "evidence-based reasoning in all outputs."
        )

    def _get_mode_system_prompt(self, mode: str) -> str:
        """Return the LLM system prompt for the given Operations mode.

        Prompts are stored in operations_prompts.py to keep this file under
        the 550-line limit.

        Args:
            mode: One of ``documentation``, ``creative_writing``, ``cost_analysis``,
                ``experiment``, ``error_recovery``, ``synthesis``, ``improvement``,
                ``monitor``, ``devops_ops``.

        Returns:
            System prompt string, or empty string for unknown modes.
        """
        return OPERATIONS_MODE_PROMPTS.get(mode, "")

    def verify(self, output: Any) -> VerificationResult:
        """Verify operations output contains actionable substance.

        Returns:
            VerificationResult requiring positive evidence of substance.
        """
        if output is None:
            return VerificationResult(passed=False, issues=[{"message": "No output"}], score=0.0)
        if not isinstance(output, dict):
            return VerificationResult(
                passed=False,
                issues=[{"message": "No structured verification output"}],
                score=0.0,
            )
        has_substance = bool(
            output.get("content")
            or output.get("documentation")
            or output.get("narrative")
            or output.get("deployment")
            or output.get("metrics")
            or output.get("processes")
            or output.get("status")
            or output.get("report"),
        )
        return VerificationResult(passed=has_substance, score=0.8 if has_substance else 0.3)

    # ------------------------------------------------------------------
    # Documentation (from DocumentationAgent)
    # ------------------------------------------------------------------

    def _execute_documentation(self, task: AgentTask) -> AgentResult:
        content = task.context.get("content", task.description)
        doc_type = task.context.get("doc_type", "api_reference")
        audience = task.context.get("audience", "developers")

        prompt = (
            f"Generate {doc_type} documentation for:\n{content[:TRUNCATE_CONTENT_ANALYSIS]}\n\n"
            f"Audience: {audience}\n\n"
            "Respond as JSON:\n"
            '{"content": "...markdown content...", "type": "' + doc_type + '", '
            '"sections": [{"title": "...", "content": "..."}], '
            '"metadata": {"audience": "' + audience + '", "word_count": 0}}'
        )
        result = self._infer_json(prompt, fallback={"content": "", "type": doc_type, "sections": []})
        return AgentResult(success=True, output=result, metadata={"mode": "documentation", "doc_type": doc_type})

    # ------------------------------------------------------------------
    # Creative Writing (from SynthesizerAgent)
    # ------------------------------------------------------------------

    def _execute_creative_writing(self, task: AgentTask) -> AgentResult:
        content = task.context.get("content", task.description)
        style = task.context.get("style", "professional")

        prompt = (
            f"Create creative content:\n{content[:TRUNCATE_CONTENT_ANALYSIS]}\n\nStyle: {style}\n\n"
            "Respond as JSON:\n"
            '{"content": "...", "type": "creative", "word_count": 0, "tone": "' + style + '"}'
        )
        result = self._infer_json(prompt, fallback={"content": "", "type": "creative"})
        return AgentResult(success=True, output=result, metadata={"mode": "creative_writing"})

    # ------------------------------------------------------------------
    # Cost Analysis (from CostPlannerAgent — preserves MODEL_PRICING)
    # ------------------------------------------------------------------

    @staticmethod
    def _execute_cost_analysis(task: AgentTask) -> AgentResult:
        context = task.context or {}
        analysis_type = context.get("analysis_type", "model_comparison")

        if analysis_type == "model_comparison":
            models = context.get("models", list(_MODEL_PRICING.keys()))
            estimated_tokens = context.get("estimated_tokens", 10000)

            comparisons = []
            for model_id in models:
                pricing = _MODEL_PRICING.get(
                    model_id,
                    {"input_per_1k": 0.001, "output_per_1k": 0.002, "tier": "unknown"},
                )
                input_cost = (estimated_tokens / 1000) * pricing["input_per_1k"]
                output_cost = (estimated_tokens / 1000) * pricing["output_per_1k"]
                comparisons.append({
                    "model": model_id,
                    "tier": pricing["tier"],
                    "input_cost": round(input_cost, 4),
                    "output_cost": round(output_cost, 4),
                    "total_cost": round(input_cost + output_cost, 4),
                })
            comparisons.sort(key=lambda c: c["total_cost"])

            return AgentResult(
                success=True,
                output={
                    "comparisons": comparisons,
                    "recommendation": comparisons[0]["model"] if comparisons else "unknown",
                    "estimated_tokens": estimated_tokens,
                    "cheapest": comparisons[0] if comparisons else None,
                    "most_expensive": comparisons[-1] if comparisons else None,
                },
                metadata={"mode": "cost_analysis", "analysis_type": analysis_type},
            )

        # General cost analysis — deterministic (bounded math, no model needed)
        description = task.description or ""
        word_count = len(description.split())
        estimated_tokens = int(word_count * 1.3)  # rough word-to-token ratio
        recommendations = []
        for model_id, pricing in sorted(
            _MODEL_PRICING.items(),
            key=lambda x: _pricing_float(x[1], "input_per_1k") + _pricing_float(x[1], "output_per_1k"),
        ):
            cost = (estimated_tokens / 1000) * (
                _pricing_float(pricing, "input_per_1k") + _pricing_float(pricing, "output_per_1k")
            )
            recommendations.append({
                "model": model_id,
                "tier": _pricing_tier(pricing),
                "estimated_cost": round(cost, 6),
            })

        logger.debug(
            "Cost analysis for %d-word description: %d estimated tokens, %d models compared",
            word_count,
            estimated_tokens,
            len(recommendations),
        )
        result = {
            "analysis": f"Estimated {estimated_tokens} tokens based on {word_count} words in task description",
            "recommendations": recommendations[:3],
            "pricing_basis": "configured model pricing table",
            "source": "deterministic",
        }
        return AgentResult(success=True, output=result, metadata={"mode": "cost_analysis"})

    # ------------------------------------------------------------------
    # Experiment (from ExperimentationManagerAgent)
    # ------------------------------------------------------------------

    def _execute_experiment(self, task: AgentTask) -> AgentResult:
        context = task.context or {}
        experiment_type = context.get("experiment_type", "design")

        prompt = (
            f"Design/analyze experiment:\n{task.description[:TRUNCATE_CONTENT_ANALYSIS]}\n\n"
            f"Type: {experiment_type}\n\n"
            "Respond as JSON:\n"
            '{"experiment": {"name": "...", "hypothesis": "...", "type": "' + experiment_type + '"}, '
            '"metrics": [{"name": "...", "type": "primary|secondary", "target": 0.0}], '
            '"variants": [{"name": "control", "description": "..."}, {"name": "treatment", "description": "..."}], '
            '"sample_size": 0, "duration_days": 7, '
            '"analysis_plan": "..."}'
        )
        result = self._infer_json(prompt, fallback={"experiment": {"name": "", "hypothesis": ""}, "metrics": []})
        return AgentResult(success=True, output=result, metadata={"mode": "experiment"})

    # ------------------------------------------------------------------
    # Error Recovery (from ErrorRecoveryAgent — preserves pattern registry)
    # ------------------------------------------------------------------

    def _execute_error_recovery(self, task: AgentTask) -> AgentResult:
        error_text = task.context.get("error", task.description)

        # Pattern matching
        matched_patterns = []
        for pattern_name, info in _ERROR_PATTERNS.items():
            for regex in info["patterns"]:
                try:
                    if re.search(regex, error_text, re.IGNORECASE):
                        matched_patterns.append({
                            "pattern": pattern_name,
                            "category": info["category"],
                            "severity": info["severity"],
                            "quick_fix": info["quick_fix"],
                        })
                        break
                except re.error:
                    logger.warning("Invalid regex in error pattern %s: %s", pattern_name, regex)

        # LLM analysis
        pattern_context = ""
        if matched_patterns:
            pattern_context = "\n\nPattern matches:\n" + "\n".join(
                f"- [{p['severity']}] {p['pattern']}: {p['quick_fix']}" for p in matched_patterns
            )

        prompt = (
            f"Analyze this error and provide recovery strategy:\n{error_text[:TRUNCATE_CONTENT_ANALYSIS]}\n"
            f"{pattern_context}\n\n"
            "Respond as JSON:\n"
            '{"root_cause": "...", "category": "...", "severity": "critical|high|medium|low", '
            '"recovery_strategy": {"immediate": "...", "retry_policy": {"type": "exponential_backoff", '
            '"max_retries": 3, "base_delay_ms": 1000}, "fallback": "..."}, '
            '"prevention": [...], "post_mortem": "..."}'
        )
        result = self._infer_json(
            prompt,
            fallback={
                "root_cause": "Analysis unavailable",
                "matched_patterns": matched_patterns,
                "recovery_strategy": {
                    "immediate": matched_patterns[0]["quick_fix"]
                    if matched_patterns
                    else "Manual investigation required",
                },
            },
        )
        if result and isinstance(result, dict):
            result["matched_patterns"] = matched_patterns
        return AgentResult(
            success=True,
            output=result,
            metadata={"mode": "error_recovery", "patterns_matched": len(matched_patterns)},
        )

    # ------------------------------------------------------------------
    # Synthesis (from SynthesizerAgent)
    # ------------------------------------------------------------------

    def _execute_synthesis(self, task: AgentTask) -> AgentResult:
        sources = task.context.get("sources", [])
        content = task.context.get("content", task.description)

        prompt = (
            f"Synthesize the following sources into a unified artifact:\n\n"
            f"Content: {content[:TRUNCATE_CONTENT_ANALYSIS]}\n"
            f"Sources ({len(sources)}): {str(sources)[:TRUNCATE_OUTPUT_PREVIEW]}\n\n"
            "Respond as JSON:\n"
            '{"synthesis": "...", "sources_used": [...], '
            '"conflicts_resolved": [...], "confidence": 0.8}'
        )
        result = self._infer_json(prompt, fallback={"synthesis": "", "sources_used": []})
        return AgentResult(success=True, output=result, metadata={"mode": "synthesis"})

    # ------------------------------------------------------------------
    # Improvement (from ImprovementAgent)
    # ------------------------------------------------------------------

    def _execute_improvement(self, task: AgentTask) -> AgentResult:
        context = task.context or {}
        focus = context.get("focus", "general")

        prompt = (
            f"Analyze system performance and recommend improvements:\n{task.description[:TRUNCATE_CONTENT_ANALYSIS]}\n\n"
            f"Focus: {focus}\n\n"
            "Respond as JSON:\n"
            '{"analysis": {"summary": "...", "bottlenecks": [...]}, '
            '"recommendations": [{"title": "...", "impact": "high|medium|low", '
            '"effort": "high|medium|low", "description": "..."}], '
            '"metrics": {"current": {...}, "projected": {...}}, '
            '"priority_actions": [...]}'
        )
        result = self._infer_json(prompt, fallback={"analysis": {"summary": ""}, "recommendations": []})
        return AgentResult(success=True, output=result, metadata={"mode": "improvement", "focus": focus})

    # ------------------------------------------------------------------
    # Monitor (absorbed from OrchestratorAgent)
    # ------------------------------------------------------------------

    def _execute_monitor(self, task: AgentTask) -> AgentResult:
        """Track system health and performance metrics."""
        # Try to gather real metrics from telemetry
        metrics = {"status": "healthy"}
        try:
            from vetinari.telemetry import get_telemetry_collector

            telemetry = get_telemetry_collector()
            if hasattr(telemetry, "get_summary"):
                metrics.update(telemetry.get_summary())
        except Exception:
            logger.warning("Telemetry unavailable for monitoring", exc_info=True)

        metrics["health_report"] = report_health(telemetry=metrics)

        # If we have a prompt, use LLM for analysis
        if task.description and len(task.description) > 20:
            prompt = (
                f"Analyze system status:\n{task.description[:TRUNCATE_CONTENT_ANALYSIS]}\n\n"
                "Respond as JSON:\n"
                '{"status": "healthy|degraded|unhealthy", "issues": [...], '
                '"recommendations": [...], "metrics_summary": {...}}'
            )
            result = self._infer_json(prompt, fallback=metrics)
            if result and isinstance(result, dict):
                return AgentResult(success=True, output=result, metadata={"operation": "monitor"})

        return AgentResult(
            success=True,
            output=metrics,
            metadata={"operation": "monitor"},
        )

    # ------------------------------------------------------------------
    # DevOps Operations (absorbed from DevOpsAgent)
    # ------------------------------------------------------------------

    def _execute_devops_ops(self, task: AgentTask) -> AgentResult:
        """Handle deployment, CI/CD, and infrastructure tasks."""
        context = task.context or {}
        focus = context.get("focus", "general")

        prompt = (
            f"Analyze and provide DevOps guidance for:\n{task.description[:TRUNCATE_CONTENT_ANALYSIS]}\n\n"
            f"Focus area: {focus}\n\n"
            "Respond as JSON:\n"
            '{"analysis": "...", "recommendations": [{"title": "...", "priority": "high|medium|low", '
            '"description": "..."}], "scripts": [], '
            '"risks": [{"description": "...", "mitigation": "..."}]}'
        )
        result = self._infer_json(
            prompt,
            fallback={
                "analysis": "DevOps analysis pending",
                "recommendations": [],
                "scripts": [],
                "risks": [],
            },
        )
        return AgentResult(success=True, output=result, metadata={"mode": "devops_ops", "focus": focus})

    def get_capabilities(self) -> list[str]:
        """Return capability strings describing this agent's supported modes and features.

        Returns:
            List of capability identifiers such as documentation,
            cost analysis, and DevOps operations.
        """
        return [
            "documentation",
            "api_docs",
            "user_guides",
            "changelog",
            "creative_writing",
            "narrative_generation",
            "cost_analysis",
            "model_comparison",
            "roi_analysis",
            "experiment_design",
            "ab_testing",
            "metrics_tracking",
            "error_recovery",
            "failure_analysis",
            "retry_strategy",
            "multi_source_synthesis",
            "conflict_resolution",
            "performance_analysis",
            "system_improvement",
            "system_monitoring",
            "health_check",
            "devops",
            "ci_cd",
            "deployment",
            "infrastructure",
        ]


# Singleton
_operations_agent: OperationsAgent | None = None
_operations_agent_lock = threading.Lock()


def get_operations_agent(config: dict[str, Any] | None = None) -> OperationsAgent:
    """Get operations agent.

    Returns:
        The OperationsAgent result.
    """
    global _operations_agent
    if _operations_agent is None:
        with _operations_agent_lock:
            if _operations_agent is None:
                _operations_agent = OperationsAgent(config)
    return _operations_agent
