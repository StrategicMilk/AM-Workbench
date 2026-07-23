"""Mode handlers for the quality skill tool."""

from __future__ import annotations

import ast
import logging

from vetinari.skills.quality_skill_contracts import (
    SECURITY_PATTERNS,
    QualityIssue,
    QualityMode,
    QualityResult,
)
from vetinari.skills.quality_skill_patterns import OWASP_TOP_10
from vetinari.types import QualityGrade, SeverityLevel, ThinkingMode

logger = logging.getLogger("vetinari.skills.quality_skill")


def _extract_function_specs(code: str) -> list[tuple[str, list[str], int]]:
    """Return public function names, parameter names, and required arg counts."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        logger.warning("Unable to parse code for function specification extraction", exc_info=True)
        return []
    specs: list[tuple[str, list[str], int]] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name.startswith("_"):
            continue
        args = [arg.arg for arg in node.args.args]
        required_count = max(0, len(args) - len(node.args.defaults))
        specs.append((node.name, args, required_count))
    return specs


class _QualitySkillModeMixin:
    """Internal implementation mixin for QualitySkillTool review modes."""

    def _run_mode(
        self,
        mode: QualityMode,
        code: str,
        context: str | None,
        thinking_mode: ThinkingMode,
        focus_areas: list[str],
    ) -> QualityResult:
        dispatch = {
            QualityMode.CODE_REVIEW: self._code_review,
            QualityMode.SECURITY_AUDIT: self._security_audit,
            QualityMode.TEST_GENERATION: self._generate_tests,
            QualityMode.SIMPLIFICATION: self._simplification_review,
            QualityMode.PERFORMANCE_REVIEW: self._performance_review,
            QualityMode.BEST_PRACTICES: self._best_practices,
        }
        handler = dispatch.get(mode)
        if handler is None:
            return QualityResult(success=False, summary=f"Unknown mode: {mode.value}")
        return handler(code, context, thinking_mode)

    def _code_review(
        self,
        code: str,
        context: str | None,
        thinking_mode: ThinkingMode,
    ) -> QualityResult:
        logger.info("Code review (depth: %s)", thinking_mode.value)
        issues: list[QualityIssue] = []
        recommendations: list[str] = []
        lines = code.split("\n")
        line_count = len(lines)

        issues.extend(self._basic_code_review_issues(code, line_count))

        func_defs = [line for line in lines if line.strip().startswith(("def ", "async def "))]
        if len(func_defs) > 10:
            issues.append(
                QualityIssue(
                    title="Too many functions",
                    severity=SeverityLevel.LOW,
                    description=f"{len(func_defs)} functions in a single file",
                    suggestion="Split into multiple modules by responsibility",
                ),
            )

        if thinking_mode in (ThinkingMode.HIGH, ThinkingMode.XHIGH):
            if "import *" in code:
                issues.append(
                    QualityIssue(
                        title="Wildcard import",
                        severity=SeverityLevel.MEDIUM,
                        description="Wildcard imports pollute namespace",
                        suggestion="Use explicit imports",
                    ),
                )
            recommendations.extend([
                "Add type hints to all function signatures",
                "Ensure docstrings on all public functions and classes",
            ])

        score, grade = self._code_review_grade(issues)

        return QualityResult(
            success=True,
            issues=issues,
            grade=grade,
            score=score,
            summary=f"Code review: {len(issues)} issue(s) in {line_count} lines. Grade: {grade.value}",
            recommendations=recommendations,
            metrics={"lines_of_code": line_count, "function_count": len(func_defs)},
        )

    @staticmethod
    def _basic_code_review_issues(code: str, line_count: int) -> list[QualityIssue]:
        """Return structural code review findings."""
        issues: list[QualityIssue] = []
        checks = [
            (
                code.count("{") != code.count("}"),
                "Unbalanced braces",
                SeverityLevel.HIGH,
                "Opening and closing braces do not match",
                "Verify brace pairing throughout the code",
            ),
            (
                code.count("(") != code.count(")"),
                "Unbalanced parentheses",
                SeverityLevel.HIGH,
                "Opening and closing parentheses do not match",
                "Check parenthesis pairing",
            ),
            (
                "TODO" in code or "FIXME" in code,
                "Unresolved TODOs/FIXMEs",
                SeverityLevel.MEDIUM,
                "Code contains TODO or FIXME comments",
                "Address all outstanding TODOs and FIXMEs before merge",
            ),
            (
                line_count > 300,
                "File too long",
                SeverityLevel.MEDIUM,
                f"File has {line_count} lines - exceeds 300-line guideline",
                "Break into smaller, focused modules",
            ),
        ]
        for condition, title, severity, description, suggestion in checks:
            if condition:
                issues.append(
                    QualityIssue(title=title, severity=severity, description=description, suggestion=suggestion)
                )
        return issues

    @staticmethod
    def _code_review_grade(issues: list[QualityIssue]) -> tuple[int, QualityGrade]:
        """Compute code review score and grade from issue severities."""
        crit = sum(1 for issue in issues if issue.severity == SeverityLevel.CRITICAL)
        high = sum(1 for issue in issues if issue.severity == SeverityLevel.HIGH)
        score = max(0, 100 - crit * 25 - high * 10 - len(issues) * 3)
        if crit:
            return score, QualityGrade.F
        if high > 2:
            return score, QualityGrade.D
        if len(issues) > 5:
            return score, QualityGrade.C
        return score, QualityGrade.B if issues else QualityGrade.A

    @staticmethod
    def _security_audit(
        code: str,
        context: str | None,
        thinking_mode: ThinkingMode,
    ) -> QualityResult:
        logger.info("Security audit (depth: %s)", thinking_mode.value)
        issues: list[QualityIssue] = []
        code_lower = code.lower()

        for pattern, info in SECURITY_PATTERNS.items():
            if pattern.lower() in code_lower:
                issues.append(
                    QualityIssue(
                        title=f"Security: {info['desc']}",
                        severity=SeverityLevel(info["severity"]),
                        description=f"Pattern '{pattern}' detected",
                        suggestion=f"Review usage. See {info['cwe']}",
                        cwe_id=info["cwe"],
                        owasp_category=info["owasp"],
                    ),
                )

        if "except:" in code and "except Exception" not in code:
            issues.append(
                QualityIssue(
                    title="Bare except clause",
                    severity=SeverityLevel.MEDIUM,
                    description="Bare except catches SystemExit and KeyboardInterrupt",
                    suggestion="Use 'except Exception as e:' and log the error",
                    cwe_id="CWE-396",
                    owasp_category="A09:2021",
                ),
            )

        covered = {i.owasp_category for i in issues if i.owasp_category}
        crit = sum(1 for i in issues if i.severity == SeverityLevel.CRITICAL)
        score = max(0, 100 - crit * 30 - len(issues) * 5)
        grade = (
            QualityGrade.F
            if crit
            else QualityGrade.C
            if len(issues) > 3
            else QualityGrade.B
            if issues
            else QualityGrade.A
        )

        return QualityResult(
            success=True,
            issues=issues,
            grade=grade,
            score=score,
            summary=f"Security audit: {len(issues)} issue(s). {len(covered)} OWASP categories covered.",
            recommendations=[
                f"OWASP coverage: {len(covered)}/{len(OWASP_TOP_10)} categories",
                "Use parameterized queries to prevent injection (A03)",
                "Store secrets in environment variables (A07)",
                "Enable security logging for auth events (A09)",
            ],
            metrics={"security_issues_count": len(issues), "owasp_coverage": len(covered)},
        )

    def _generate_tests(
        self,
        code: str,
        context: str | None,
        thinking_mode: ThinkingMode,
    ) -> QualityResult:
        logger.info("Test generation (depth: %s)", thinking_mode.value)
        funcs = [line.strip() for line in code.split("\n") if line.strip().startswith(("def ", "async def "))]
        classes = [line.strip() for line in code.split("\n") if line.strip().startswith("class ")]

        categories = ["happy_path", "edge_cases", "error_cases"]
        if thinking_mode in (ThinkingMode.HIGH, ThinkingMode.XHIGH):
            categories.extend(["integration", "performance"])

        est = len(funcs) * len(categories)
        function_specs = _extract_function_specs(code)
        test_lines = ["import inspect", "import pytest", "", code.rstrip(), ""]
        if not function_specs:
            test_lines.extend((
                "def test_generated_source_defines_testable_surface():",
                "    public_names = [name for name in globals() if not name.startswith('_')]",
                "    assert public_names, 'generated tests require at least one public symbol'",
                "",
            ))
        for name, params, required_count in function_specs:
            test_lines.extend((
                f"def test_{name}_signature_is_stable():",
                f"    assert callable({name})",
                f"    assert list(inspect.signature({name}).parameters) == {params!r}",
                "",
            ))
            if required_count:
                test_lines.extend((
                    f"def test_{name}_missing_required_arguments_raises_type_error():",
                    "    with pytest.raises(TypeError):",
                    f"        {name}()",
                    "",
                ))
        tests_artifact = "\n".join(test_lines).rstrip()
        return QualityResult(
            success=True,
            issues=[],
            grade=QualityGrade.B,
            score=80,
            summary=f"Test strategy: {est} tests across {len(categories)} categories.",
            recommendations=[
                "Follow Arrange-Act-Assert pattern",
                f"Cover {len(funcs)} functions and {len(classes)} classes",
                f"Categories: {', '.join(categories)}",
                "Mock external dependencies",
                "Naming: test_<fn>_<scenario>_<expected>",
            ],
            tests=tests_artifact,
            metrics={
                "testable_functions": len(funcs),
                "testable_classes": len(classes),
                "estimated_tests": est,
                "test_categories": categories,
            },
        )

    @staticmethod
    def _simplification_review(
        code: str,
        context: str | None,
        thinking_mode: ThinkingMode,
    ) -> QualityResult:
        logger.info("Simplification review (depth: %s)", thinking_mode.value)
        issues: list[QualityIssue] = []
        lines = code.split("\n")

        if len(lines) > 200:
            issues.append(
                QualityIssue(
                    title="Long file",
                    severity=SeverityLevel.LOW,
                    description=f"{len(lines)} lines -- candidate for splitting",
                    suggestion="Split by single responsibility principle",
                ),
            )

        max_indent = max((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
        if max_indent > 20:
            issues.append(
                QualityIssue(
                    title="Deep nesting",
                    severity=SeverityLevel.MEDIUM,
                    description=f"Max indentation: {max_indent // 4} levels",
                    suggestion="Use early returns, guard clauses, or extract helpers",
                ),
            )

        return QualityResult(
            success=True,
            issues=issues,
            grade=QualityGrade.B if len(issues) <= 2 else QualityGrade.C,
            score=max(0, 100 - len(issues) * 10),
            summary=f"Simplification: {len(issues)} opportunities.",
            recommendations=[
                "Early return pattern",
                "Extract complex conditionals",
                "Self-documenting code over comments",
                "Data-driven patterns",
            ],
        )

    @staticmethod
    def _performance_review(
        code: str,
        context: str | None,
        thinking_mode: ThinkingMode,
    ) -> QualityResult:
        logger.info("Performance review (depth: %s)", thinking_mode.value)
        issues: list[QualityIssue] = []
        for_count = code.count("for ")

        if for_count > 2:
            issues.append(
                QualityIssue(
                    title="Potential quadratic complexity",
                    severity=SeverityLevel.MEDIUM,
                    description=f"{for_count} for-loops -- check for nested iteration",
                    suggestion="Use dict/set lookups or algorithmic optimization",
                ),
            )
        if "while True" in code:
            issues.append(
                QualityIssue(
                    title="Infinite loop risk",
                    severity=SeverityLevel.HIGH,
                    description="while True needs clear exit conditions",
                    suggestion="Add max_iterations guard and termination condition",
                ),
            )

        return QualityResult(
            success=True,
            issues=issues,
            grade=QualityGrade.B if len(issues) <= 1 else QualityGrade.C,
            score=max(0, 100 - len(issues) * 15),
            summary=f"Performance: {len(issues)} potential issue(s).",
            recommendations=[
                "Profile before optimizing",
                "Use dict/set for lookups",
                "functools.lru_cache for expensive computations",
                "Generators for large datasets",
            ],
            metrics={"loop_count": for_count},
        )

    @staticmethod
    def _best_practices(
        code: str,
        context: str | None,
        thinking_mode: ThinkingMode,
    ) -> QualityResult:
        logger.info("Best practices (depth: %s)", thinking_mode.value)
        issues: list[QualityIssue] = []

        if "except:" in code:
            issues.append(
                QualityIssue(
                    title="Bare except",
                    severity=SeverityLevel.MEDIUM,
                    description="Catches SystemExit and KeyboardInterrupt",
                    suggestion="Use 'except Exception as e:'",
                ),
            )
        if "def " in code and "=[]" in code.replace(" ", ""):
            issues.append(
                QualityIssue(
                    title="Mutable default argument",
                    severity=SeverityLevel.HIGH,
                    description="Mutable defaults are shared between calls",
                    suggestion="Use None default, create list inside function",
                ),
            )
        if "global " in code:
            issues.append(
                QualityIssue(
                    title="Global variable usage",
                    severity=SeverityLevel.MEDIUM,
                    description="Globals make code harder to test",
                    suggestion="Use parameters or dependency injection",
                ),
            )

        grade = QualityGrade.A if not issues else QualityGrade.B if len(issues) <= 2 else QualityGrade.C
        return QualityResult(
            success=True,
            issues=issues,
            grade=grade,
            score=max(0, 100 - len(issues) * 10),
            summary=f"Best practices: {len(issues)} issue(s).",
            recommendations=[
                "SOLID principles",
                "Composition over inheritance",
                "Consistent type hints",
                "Docstrings on public APIs",
                "Functions under 50 lines",
            ],
        )


__all__ = ["_QualitySkillModeMixin"]
