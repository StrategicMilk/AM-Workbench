"""Inspector agent — the independent quality gate in the factory pipeline.

Replaces: EVALUATOR + SECURITY_AUDITOR + TEST_AUTOMATION

Modes:
- code_review: General code quality, design patterns, maintainability
- security_audit: Vulnerability detection with 45+ heuristic patterns + LLM
- test_generation: pytest-aware test generation with coverage analysis
- simplification: Code simplification and refactoring recommendations

Pattern data and scan functions live in quality_patterns.py to stay under
the 550-line limit. Re-exported here for backward compatibility.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from vetinari.agents.consolidated.quality_patterns import (
    INSPECTOR_PASS_CONFIGS,
    _run_correctness_scan,
    _run_performance_scan,
    _run_standards_scan,
    run_antipattern_scan,
    run_multi_perspective_review,
    run_security_scan,
)
from vetinari.agents.consolidated.quality_prompts import INSPECTOR_MODE_PROMPTS
from vetinari.agents.contracts import AgentResult, AgentTask, VerificationResult
from vetinari.agents.multi_mode_agent import MultiModeAgent
from vetinari.constants import TRUNCATE_CODE_ANALYSIS, TRUNCATE_CONTEXT
from vetinari.exceptions import JurisdictionViolation
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy getters for quality_execution — imported once to break circular dep.
# quality_execution imports vetinari.agents.consolidated which re-imports
# this module, so direct module-level imports would cause ImportError.
# ---------------------------------------------------------------------------

_cached_exec_code_review_fn = None
_cached_exec_security_audit_fn = None
_cached_root_cause_analyzer_class = None
_cached_improvement_log_class = None


def _get_root_cause_analyzer_class() -> type:
    """Return RootCauseAnalyzer from vetinari.validation, importing once on first call.

    Returns:
        The RootCauseAnalyzer class, ready to be instantiated.
    """
    global _cached_root_cause_analyzer_class
    if _cached_root_cause_analyzer_class is None:
        from vetinari.validation.root_cause import RootCauseAnalyzer

        _cached_root_cause_analyzer_class = RootCauseAnalyzer
    return _cached_root_cause_analyzer_class


def _get_improvement_log_class() -> type:
    """Return ImprovementLog from vetinari.kaizen, importing once on first call.

    Returns:
        The ImprovementLog class, ready to be instantiated.
    """
    global _cached_improvement_log_class
    if _cached_improvement_log_class is None:
        from vetinari.kaizen.improvement_log import ImprovementLog

        _cached_improvement_log_class = ImprovementLog
    return _cached_improvement_log_class


def _get_exec_code_review():
    """Return execute_code_review from quality_execution, importing once on first call."""
    global _cached_exec_code_review_fn
    if _cached_exec_code_review_fn is None:
        from vetinari.agents.consolidated.quality_execution import execute_code_review

        _cached_exec_code_review_fn = execute_code_review
    return _cached_exec_code_review_fn


def _get_exec_security_audit():
    """Return execute_security_audit from quality_execution, importing once on first call."""
    global _cached_exec_security_audit_fn
    if _cached_exec_security_audit_fn is None:
        from vetinari.agents.consolidated.quality_execution import execute_security_audit

        _cached_exec_security_audit_fn = execute_security_audit
    return _cached_exec_security_audit_fn


# Re-export public symbols for backward compatibility
__all__ = [
    "INSPECTOR_PASS_CONFIGS",
    "InspectorAgent",
    "_run_correctness_scan",
    "_run_performance_scan",
    "_run_standards_scan",
    "get_inspector_agent",
    "run_antipattern_scan",
    "run_multi_perspective_review",
    "run_security_scan",
]


class InspectorAgent(MultiModeAgent):
    """Inspector agent — the independent quality gate in the factory pipeline.

    The Inspector performs code review, security audits, test generation, and
    simplification analysis. Its gate decisions (pass/fail) are authoritative
    and cannot be overridden by any other agent.
    """

    MODES = {
        "code_review": "_execute_code_review",
        "security_audit": "_execute_security_audit",
        "test_generation": "_execute_test_generation",
        "simplification": "_execute_simplification",
    }
    DEFAULT_MODE = "code_review"
    MODE_KEYWORDS = {
        "code_review": [
            "review",
            "quality",
            "maintainab",
            "readab",
            "refactor",
            "clean",
            "design pattern",
            "solid",
            "code smell",
        ],
        "security_audit": [
            "security",
            "vulnerab",
            "audit",
            "cwe",
            "owasp",
            "injection",
            "xss",
            "csrf",
            "auth",
            "encrypt",
            "credential",
        ],
        "test_generation": [
            "test",
            "pytest",
            "coverage",
            "unit test",
            "integration test",
            "mock",
            "fixture",
            "assert",
            "tdd",
        ],
        "simplification": ["simplif", "complex", "reduce", "clean up", "streamline"],
    }

    # Inspector may only infer in evaluation modes — it must never generate
    # new production content (code, docs, configs). This is the dual of the
    # Foreman's planning-only guard.
    _EVALUATION_MODES: frozenset[str] = frozenset({
        "code_review",
        "security_audit",
        "test_generation",
        "simplification",
    })

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(AgentType.INSPECTOR, config)

    def _infer(self, prompt: str, **kwargs: Any) -> str:
        """Guard inference to evaluation modes only.

        The Inspector is an evaluation-only agent — it must never be used to
        generate production content. This mirrors the Foreman's planning-only
        guard (ADR-0061 scope enforcement).

        Args:
            prompt: The inference prompt.
            **kwargs: Additional inference parameters.

        Returns:
            The inference result string.

        Raises:
            JurisdictionViolation: If current mode is not an evaluation mode.
        """
        if self._current_mode and self._current_mode not in self._EVALUATION_MODES:
            raise JurisdictionViolation(
                f"Inspector cannot infer in mode {self._current_mode!r} — "
                f"only evaluation modes are permitted: {sorted(self._EVALUATION_MODES)}"
            )
        return super()._infer(prompt, **kwargs)

    def _get_base_system_prompt(self) -> str:
        return (
            "You are Vetinari's Inspector — the independent quality gate. "
            "Your pass/fail decisions are authoritative and cannot be overridden "
            "by any other agent. You perform code review, security audits, "
            "test generation, and simplification analysis."
        )

    def _get_mode_system_prompt(self, mode: str) -> str:
        """Return the LLM system prompt for the given Inspector mode.

        Prompts are defined in quality_prompts.py to keep this file under
        the 550-line limit.

        Args:
            mode: One of code_review, security_audit, test_generation, simplification.

        Returns:
            System prompt string, or empty string for unknown modes.
        """
        return INSPECTOR_MODE_PROMPTS.get(mode, "")

    def verify(self, output: Any) -> VerificationResult:
        """Verify that the agent produced substantive review content.

        Requires non-empty issues, findings, or tests as positive evidence of
        inspection — a score field alone is not sufficient because the fallback
        dict ``{"score": 0.5, "issues": [], "summary": "Review unavailable"}``
        also carries a score value and must be treated as failure.

        Args:
            output: The agent output to verify, expected to be a dict with
                ``issues``, ``findings``, or ``tests`` keys containing review results.

        Returns:
            VerificationResult with passed=True only when at least one of
            issues/findings/tests is a non-empty collection, indicating that
            real inspection occurred.
        """
        if output is None:
            return VerificationResult(passed=False, issues=[{"message": "No output produced"}], score=0.0)
        if isinstance(output, dict):
            # Require substantive evidence: non-empty issues, findings, or tests.
            # A score field alone is not positive evidence — the inference fallback
            # dict {"score": 0.5, "issues": [], "summary": "Review unavailable"}
            # would otherwise pass this gate despite containing no review content.
            has_review = bool(output.get("issues") or output.get("findings") or output.get("tests"))
            if not has_review:
                return VerificationResult(
                    passed=False,
                    issues=[
                        {"message": "Review output contains no issues, findings, or tests — likely a fallback response"}
                    ],
                    score=0.0,
                )
            return VerificationResult(passed=True, score=0.8)
        return VerificationResult(
            passed=False,
            issues=[{"message": "No structured verification output — expected dict with issues/findings/tests"}],
            score=0.0,
        )

    # ------------------------------------------------------------------
    # Code Review (from EvaluatorAgent)
    # ------------------------------------------------------------------

    def _execute_code_review(self, task: AgentTask) -> AgentResult:
        """Run the code-review pipeline — delegates to quality_execution.execute_code_review.

        Args:
            task: The AgentTask with code and context.

        Returns:
            AgentResult with review score, issues, and strengths.
        """
        return _get_exec_code_review()(self, task)

    # ------------------------------------------------------------------
    # Security Audit (from SecurityAuditorAgent)
    # ------------------------------------------------------------------

    def _execute_security_audit(self, task: AgentTask) -> AgentResult:
        """Run the security-audit pipeline — delegates to quality_execution.execute_security_audit.

        Args:
            task: The AgentTask with code and optional file_path context.

        Returns:
            AgentResult with security findings, overall_risk, and score.
        """
        return _get_exec_security_audit()(self, task)

    @staticmethod
    def _perform_root_cause_analysis(task: AgentTask, result: AgentResult) -> AgentResult:
        """Perform root cause analysis when quality check fails.

        Adds root_cause metadata to the result for downstream corrective routing.

        Args:
            task: The AgentTask that was quality-checked.
            result: The failed AgentResult to annotate.

        Returns:
            The same AgentResult with root_cause added to metadata.
        """
        output = result.output if isinstance(result.output, dict) else {}

        # Extract rejection reasons from issues or findings
        rejection_reasons: list[str] = []
        for issue in output.get("issues", []):
            msg = issue.get("message", "") if isinstance(issue, dict) else str(issue)
            if msg:
                rejection_reasons.append(msg)
        for finding in output.get("findings", []):
            msg = finding.get("finding", "") if isinstance(finding, dict) else str(finding)
            if msg:
                rejection_reasons.append(msg)
        summary = output.get("summary", "")
        if summary and not rejection_reasons:
            rejection_reasons.append(summary)

        quality_score = float(output.get("score", 0.5))
        task_mode = result.metadata.get("mode", "")

        rca = _get_root_cause_analyzer_class()().analyze(
            task_description=task.description,
            rejection_reasons=rejection_reasons,
            quality_score=quality_score,
            task_mode=task_mode,
        )

        logger.info(
            "RCA for task %s: category=%s, confidence=%.2f",
            task.task_id,
            rca.category.value,
            rca.confidence,
        )

        result.metadata["root_cause"] = {
            "category": rca.category.value,
            "confidence": rca.confidence,
            "corrective_action": rca.corrective_action,
            "preventive_action": rca.preventive_action,
            "evidence": rca.evidence,
        }
        # Wire WO-06: surface root_cause corrective_action in result.errors so
        # downstream callers (AgentGraph recovery, rework router) can act on it
        # without having to inspect metadata directly.
        if rca.corrective_action:
            logger.warning(
                "RCA corrective action for task %s [%s]: %s",
                task.task_id,
                rca.category.value,
                rca.corrective_action,
            )
            result.errors.append(f"[RCA:{rca.category.value}] {rca.corrective_action}")

        # Record defect for trend analysis (Dept 7.5)
        try:
            _defect_log = _get_improvement_log_class()()  # Uses unified DB via get_connection()
            _agent_type = task.context.get("agent_type", "") if hasattr(task, "context") else ""
            _defect_log.record_defect(
                category=rca.category.value,
                agent_type=str(_agent_type),
                mode=task_mode,
                task_id=task.task_id,
                confidence=rca.confidence,
            )
        except Exception:
            logger.warning("Failed to record defect for trend analysis", exc_info=True)

        return result

    def _run_heuristic_scan(self, code: str) -> list[dict[str, Any]]:
        """Run regex-based security heuristic patterns against code.

        Delegates to the standalone run_security_scan function in quality_patterns.

        Args:
            code: The source code string to scan.

        Returns:
            List of finding dicts with severity, finding, line, evidence, and source fields.
        """
        _ = self
        return run_security_scan(code)

    def _run_antipattern_scan(self, code: str) -> list[dict[str, Any]]:
        """Scan code for deterministic AI code generation anti-patterns.

        Delegates to the standalone run_antipattern_scan function in quality_patterns.

        Args:
            code: The source code string to scan.

        Returns:
            List of finding dicts with severity, finding, line, evidence, and source fields.
        """
        _ = self
        return run_antipattern_scan(code)

    # ------------------------------------------------------------------
    # Test Generation (from TestAutomationAgent)
    # ------------------------------------------------------------------

    def _execute_test_generation(self, task: AgentTask) -> AgentResult:
        code = task.context.get("code", task.description)
        test_type = task.context.get("test_type", "unit")
        target_coverage = task.context.get("target_coverage", 0.8)

        prompt = (
            f"Generate comprehensive {test_type} tests for this code:\n\n"
            f"```python\n{code[:TRUNCATE_CODE_ANALYSIS]}\n```\n\n"
            f"Target coverage: {target_coverage * 100}%\n\n"
            "Requirements:\n"
            "- Use pytest framework\n"
            "- Include fixtures and parametrize where appropriate\n"
            "- Test both happy paths and edge cases\n"
            "- Include error/exception testing\n"
            "- Add docstrings explaining test purpose\n\n"
            "Respond as JSON:\n"
            '{"tests": "...full pytest code...", '
            '"test_count": 0, "coverage_estimate": 0.8, '
            '"fixtures": [...], "edge_cases_covered": [...]}'
        )
        result = self._infer_json(prompt, fallback={"tests": "", "test_count": 0, "coverage_estimate": 0.0})
        return AgentResult(success=True, output=result, metadata={"mode": "test_generation", "test_type": test_type})

    # ------------------------------------------------------------------
    # Simplification
    # ------------------------------------------------------------------

    def _execute_simplification(self, task: AgentTask) -> AgentResult:
        code = task.context.get("code", task.description)

        prompt = (
            f"Analyze this code for simplification opportunities:\n\n"
            f"```\n{code[:TRUNCATE_CONTEXT]}\n```\n\n"
            "Respond as JSON:\n"
            '{"score": 0.6, "complexity_issues": [{"location": "...", '
            '"issue": "...", "suggestion": "...", "simplified_code": "..."}], '
            '"overall_recommendations": [...], '
            '"estimated_line_reduction": 0}'
        )
        result = self._infer_json(prompt, fallback={"score": 0.5, "complexity_issues": []})
        return AgentResult(success=True, output=result, metadata={"mode": "simplification"})

    def get_capabilities(self) -> list[str]:
        """Return capability strings describing this agent's supported modes and features.

        Returns:
            List of capability identifiers such as code review,
            security audit, and test generation.
        """
        return [
            "code_review",
            "quality_scoring",
            "design_pattern_check",
            "ai_antipattern_detection",
            "security_audit",
            "vulnerability_detection",
            "cwe_classification",
            "test_generation",
            "coverage_analysis",
            "fixture_generation",
            "code_simplification",
            "complexity_reduction",
        ]


_inspector_agent: InspectorAgent | None = None
_inspector_agent_lock = threading.Lock()


def get_inspector_agent(config: dict[str, Any] | None = None) -> InspectorAgent:
    """Get the singleton Inspector agent instance.

    Args:
        config: Optional configuration dict.

    Returns:
        A configured InspectorAgent instance.
    """
    global _inspector_agent
    if _inspector_agent is None:
        with _inspector_agent_lock:
            if _inspector_agent is None:
                _inspector_agent = InspectorAgent(config)
    return _inspector_agent
