"""Mode handler mixin for InspectorSkillTool."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from vetinari.skills.inspector_skill_types import _FUNC_DEF_RE, InspectorResult, ReviewIssue

logger = logging.getLogger("vetinari.skills.inspector_skill")


class _InspectorModeHandlersMixin:
    """Provide InspectorSkillTool mode-specific review behavior."""

    if TYPE_CHECKING:
        _get_quality_tool: Any
        _merge_quality_issues: Any
        _score_to_grade: Any

    def _code_review(self, code: str, context: dict[str, Any], focus_areas: list[str]) -> InspectorResult:
        """5-pass code review: correctness, style, security, performance, maintainability.

        Delegates to the InspectorAgent for LLM-powered analysis when available,
        falls back to heuristic scanning.
        """
        issues: list[ReviewIssue] = []

        lines = code.split("\n") if code else []
        issues.extend(self._basic_code_review_issues(lines))

        # Deny-pattern pass: check for dangerous code patterns from standards config.
        try:
            from vetinari.config.standards_loader import get_standards_loader

            deny_findings = get_standards_loader().evaluate_deny_patterns(code)
            issues.extend(
                ReviewIssue(
                    severity=finding.get("severity", "high"),
                    description=finding.get("description", f"Deny pattern matched: {finding.get('pattern', '')}"),
                    category="security",
                    suggestion=f"Remove or replace the matched pattern: {finding.get('match', '')}",
                )
                for finding in deny_findings
            )
        except Exception as exc:
            logger.warning("Inspector: deny pattern evaluation failed: %s", exc)

        # Supplementary pass: delegate to QualitySkillTool for richer detection.
        heuristic_count = len(issues)
        supplementary_count = 0
        try:
            quality_result = self._get_quality_tool().execute(mode="code_review", code=code, thinking_mode="medium")
            issues = self._merge_quality_issues(issues, quality_result, dedup_field="description")
            supplementary_count = len(issues) - heuristic_count
        except Exception as exc:
            logger.error(
                "Inspector: QualitySkillTool code_review failed - using heuristic results only: %s",
                exc,
            )

        issues.extend(self._cascade_code_review_issues(code, context.get("task_description", "")))
        severity_counts = self._severity_counts(issues)
        score = self._code_review_score(severity_counts)
        grade = self._score_to_grade(score)
        passed = len(issues) == 0

        return InspectorResult(
            passed=passed,
            issues=issues,
            grade=grade,
            score=round(score, 3),
            metrics={
                "total_issues": len(issues),
                "critical": severity_counts["critical"],
                "high": severity_counts["high"],
                "medium": severity_counts["medium"],
                "low": severity_counts["low"],
                "lines_reviewed": len(lines),
                "supplementary_issues": supplementary_count,
            },
        )

    def _basic_code_review_issues(self, lines: list[str]) -> list[ReviewIssue]:
        """Return simple line-based code review findings."""
        issues: list[ReviewIssue] = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if "print(" in stripped and not stripped.startswith("#"):
                issues.append(
                    ReviewIssue(
                        severity="medium",
                        description="print statement found in production code - use logging module",
                        line=i,
                        category="style",
                        suggestion="Replace with logger.info() or logger.debug()",
                    )
                )
            if "except:" in stripped and "except Exception" not in stripped:
                issues.append(
                    ReviewIssue(
                        severity="high",
                        description="Bare except clause - catches SystemExit and KeyboardInterrupt",
                        line=i,
                        category="correctness",
                        suggestion="Use 'except Exception as e:' with proper error handling",
                    )
                )
            if "TODO" in stripped and "#" in stripped:
                issues.append(
                    ReviewIssue(
                        severity="low",
                        description="TODO comment found in production code",
                        line=i,
                        category="completeness",
                        suggestion="Resolve or convert to tracked issue",
                    )
                )
        return issues

    @staticmethod
    def _cascade_code_review_issues(code: str, task_description: str) -> list[ReviewIssue]:
        """Return static cascade verification findings when available."""
        try:
            from vetinari.validation.verification import get_cascade_orchestrator

            verdict = get_cascade_orchestrator().verify(code, task_description)
            if verdict.passed or verdict.tier_reached != "static":
                return []
            logger.info("Inspector: cascade Tier 1 added %d static finding(s)", len(verdict.static_findings))
            return [
                ReviewIssue(
                    severity="high",
                    description=finding,
                    category="static_verification",
                    suggestion="Fix the static verification failure before resubmitting",
                )
                for finding in verdict.static_findings
            ]
        except Exception as exc:
            logger.warning("Inspector: cascade verification unavailable (%s) - skipping cascade tier", exc)
            return []

    @staticmethod
    def _severity_counts(issues: list[ReviewIssue]) -> dict[str, int]:
        """Count issues by severity."""
        return {
            "critical": sum(1 for issue in issues if issue.severity == "critical"),
            "high": sum(1 for issue in issues if issue.severity == "high"),
            "medium": sum(1 for issue in issues if issue.severity == "medium"),
            "low": sum(1 for issue in issues if issue.severity == "low"),
        }

    @staticmethod
    def _code_review_score(counts: dict[str, int]) -> float:
        """Compute the zero-tolerance code review score from severity counts."""
        penalty = counts["critical"] * 0.3 + counts["high"] * 0.15 + counts["medium"] * 0.05 + counts["low"] * 0.02
        return max(0.0, 1.0 - penalty)

    def _security_audit(self, code: str, context: dict[str, Any], focus_areas: list[str]) -> InspectorResult:
        """Security audit with OWASP Top 10 and CWE mapping.

        Scans for hardcoded credentials, injection vulnerabilities, insecure
        deserialization, and other security patterns.
        """
        issues: list[ReviewIssue] = []

        # Pattern-based security scan
        security_patterns = [
            ("password\\s*=\\s*[\"']", "Hardcoded password", "CWE-798", "A07:2021"),
            ("api_key\\s*=\\s*[\"']", "Hardcoded API key", "CWE-798", "A07:2021"),
            ("secret\\s*=\\s*[\"']", "Hardcoded secret", "CWE-798", "A07:2021"),
            ("shell\\s*=\\s*True", "Shell injection risk", "CWE-78", "A03:2021"),
            ("yaml\\.load\\(", "Unsafe YAML loading", "CWE-502", "A08:2021"),
            ("eval\\(", "Code injection via eval()", "CWE-95", "A03:2021"),
            ("pickle\\.loads?\\(", "Insecure deserialization", "CWE-502", "A08:2021"),
            ("verify\\s*=\\s*False", "SSL verification disabled", "CWE-295", "A07:2021"),
        ]

        for pattern, desc, cwe, owasp in security_patterns:
            for i, line in enumerate(code.split("\n"), 1):
                if re.search(pattern, line) and not line.strip().startswith("#"):
                    issues.append(
                        ReviewIssue(
                            severity="high",
                            description=desc,
                            line=i,
                            category="security",
                            cwe=cwe,
                            owasp=owasp,
                            suggestion=f"Review and remediate: {desc}",
                        ),
                    )

        # Supplementary pass: delegate to QualitySkillTool for broader security detection.
        heuristic_security_count = len(issues)
        supplementary_security_count = 0
        try:
            quality_result = self._get_quality_tool().execute(mode="security_audit", code=code, thinking_mode="medium")
            issues = self._merge_quality_issues(issues, quality_result, dedup_field="cwe")
            supplementary_security_count = len(issues) - heuristic_security_count
        except Exception as exc:
            logger.error(
                "Inspector: QualitySkillTool security_audit failed - using heuristic results only: %s",
                exc,
            )

        score = max(0.0, 1.0 - len(issues) * 0.15)
        passed = len(issues) == 0

        return InspectorResult(
            passed=passed,
            issues=issues,
            grade=self._score_to_grade(score),
            score=round(score, 3),
            metrics={
                "security_issues": len(issues),
                "patterns_checked": len(security_patterns),
                "supplementary_security_issues": supplementary_security_count,
            },
        )

    @staticmethod
    def _test_generation(code: str, context: dict[str, Any], focus_areas: list[str]) -> InspectorResult:
        """Identify untested paths and generate gap-filling test suggestions.

        Analyzes code to find functions without corresponding tests and
        suggests test cases for happy path, edge cases, and error paths.
        """
        # Find functions that might need tests
        functions = _FUNC_DEF_RE.findall(code)
        public_functions = [f for f in functions if not f.startswith("_")]

        suggestions: list[str] = []
        for func in public_functions:
            suggestions.extend((f"Add test for {func}() - happy path", f"Add test for {func}() - error/edge cases"))

        has_gaps = len(suggestions) > 0
        if not has_gaps:
            grade = "A"
            score = 0.95
        elif len(suggestions) <= 4:
            grade = "B"
            score = 0.75
        elif len(suggestions) <= 8:
            grade = "C"
            score = 0.55
        else:
            grade = "D"
            score = 0.35

        return InspectorResult(
            passed=not has_gaps,
            grade=grade,
            score=score,
            suggestions=suggestions,
            metrics={
                "total_functions": len(functions),
                "public_functions": len(public_functions),
                "test_suggestions": len(suggestions),
            },
        )

    def _simplification(self, code: str, context: dict[str, Any], focus_areas: list[str]) -> InspectorResult:
        """Identify dead code, over-abstraction, and complexity reduction opportunities.

        Analyzes code for YAGNI violations, unnecessary abstractions, and
        overly complex logic that could be simplified.
        """
        issues: list[ReviewIssue] = []
        lines = code.split("\n") if code else []

        # Check for common complexity indicators
        nesting_depth = 0
        max_nesting = 0
        for line in lines:
            stripped = line.strip()
            if stripped.endswith(":") and any(
                stripped.startswith(kw) for kw in ["if ", "for ", "while ", "try:", "with "]
            ):
                nesting_depth += 1
                max_nesting = max(max_nesting, nesting_depth)
            elif stripped in ("", "pass") or (not stripped.startswith(" " * (nesting_depth * 4))):
                nesting_depth = max(0, nesting_depth - 1)

        if max_nesting > 4:
            issues.append(
                ReviewIssue(
                    severity="medium",
                    description=f"Deep nesting detected (depth {max_nesting}) - consider extracting helper functions",
                    category="complexity",
                    suggestion="Break complex nested logic into smaller, named functions",
                ),
            )

        # Check for overly long functions
        current_func = None
        func_start = 0
        for i, line in enumerate(lines, 1):
            if line.strip().startswith("def "):
                if current_func and i - func_start > 50:
                    issues.append(
                        ReviewIssue(
                            severity="low",
                            description=f"Function '{current_func}' is {i - func_start} lines - consider splitting",
                            line=func_start,
                            category="complexity",
                        ),
                    )
                current_func = line.strip().split("(")[0].replace("def ", "")
                func_start = i

        score = max(0.0, 1.0 - len(issues) * 0.1)

        return InspectorResult(
            passed=len(issues) == 0,
            issues=issues,
            grade=self._score_to_grade(score),
            score=round(score, 3),
            metrics={
                "max_nesting_depth": max_nesting,
                "total_lines": len(lines),
                "simplification_opportunities": len(issues),
            },
        )
