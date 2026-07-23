"""Standard quality gate check implementations."""

from __future__ import annotations

import logging
import re
from typing import Any

from vetinari.validation.gate_checks_helpers import _GateCheckHelpers
from vetinari.validation.gate_types import GateCheckResult, GateResult, QualityGateConfig

logger = logging.getLogger(__name__)


class _StandardGateChecks(_GateCheckHelpers):
    """Quality, security, coverage, and architecture gate checks."""

    def check_quality(self, artifacts: dict[str, Any], config: QualityGateConfig) -> GateCheckResult:
        """Run quality verification (style, complexity, best practices).

        Inspects ``artifacts["code"]`` for common quality issues using
        lightweight heuristic analysis. Falls back gracefully when the
        code key is absent.

        Args:
            artifacts: Dict containing at least an optional ``"code"`` key
                with the source code string to inspect.
            config: The quality gate configuration for this check.

        Returns:
            GateCheckResult reflecting quality heuristics outcome.
        """
        code = artifacts.get("code", "")
        issues: list[dict[str, Any]] = []
        suggestions: list[str] = []
        score = 1.0

        if not code:
            return GateCheckResult(
                gate_name=config.name,
                mode=config.mode,
                result=GateResult.FAILED,
                score=0.0,
                issues=[{"severity": "error", "message": "No code artifacts to check"}],
                suggestions=["Provide code artifacts for quality analysis"],
            )

        score = self._add_complexity_quality_issues(code, score, issues, suggestions)
        score = self._add_docstring_quality_issues(code, score, issues, suggestions)
        score = self._add_best_practice_quality_issues(code, score, issues, suggestions)

        score = max(0.0, min(1.0, score))
        result_enum = self._score_to_result(score, config.min_score)

        return GateCheckResult(
            gate_name=config.name,
            mode=config.mode,
            result=result_enum,
            score=round(score, 3),
            issues=issues,
            suggestions=suggestions,
        )

    def _add_complexity_quality_issues(
        self,
        code: str,
        score: float,
        issues: list[dict[str, Any]],
        suggestions: list[str],
    ) -> float:
        long_functions = self._check_long_functions(code)
        if not long_functions:
            return score
        issues.extend(
            {
                "severity": "warning",
                "category": "complexity",
                "message": f"Function '{fn}' appears to be overly long",
            }
            for fn in long_functions
        )
        suggestions.append("Break large functions into smaller, focused helpers")
        return score - min(0.3, len(long_functions) * 0.1)

    def _add_docstring_quality_issues(
        self,
        code: str,
        score: float,
        issues: list[dict[str, Any]],
        suggestions: list[str],
    ) -> float:
        missing_docs = self._check_missing_docstrings(code)
        if not missing_docs:
            return score
        issues.extend(
            {
                "severity": "info",
                "category": "documentation",
                "message": f"Function '{fn}' is missing a docstring",
            }
            for fn in missing_docs
        )
        suggestions.append("Add docstrings to all public functions")
        return score - min(0.2, len(missing_docs) * 0.05)

    @staticmethod
    def _add_best_practice_quality_issues(
        code: str,
        score: float,
        issues: list[dict[str, Any]],
        suggestions: list[str],
    ) -> float:
        bare_excepts = len(re.findall(r"except\s*:", code))
        if bare_excepts:
            score -= min(0.2, bare_excepts * 0.1)
            issues.append(
                {
                    "severity": "warning",
                    "category": "best_practices",
                    "message": f"Found {bare_excepts} bare except clause(s)",
                },
            )
            suggestions.append("Catch specific exception types instead of bare except")
        markers = len(re.findall(r"#\s*(TODO|FIXME|HACK|XXX)\b", code, re.IGNORECASE))
        if markers:
            score -= min(0.1, markers * 0.02)
            issues.append(
                {
                    "severity": "info",
                    "category": "maintenance",
                    "message": f"Found {markers} TODO/FIXME/HACK marker(s)",
                },
            )
        return score

    def check_security(self, artifacts: dict[str, Any], config: QualityGateConfig) -> GateCheckResult:
        """Run security verification.

        Checks ``artifacts["code"]`` for dangerous patterns, potential
        secrets, and unsafe practices.

        Args:
            artifacts: Dict containing at least an optional ``"code"`` key.
            config: The quality gate configuration for this check.

        Returns:
            GateCheckResult reflecting the security scan outcome.
        """
        code = artifacts.get("code", "")
        issues: list[dict[str, Any]] = []
        suggestions: list[str] = []
        score = 1.0

        if not code:
            return GateCheckResult(
                gate_name=config.name,
                mode=config.mode,
                result=GateResult.FAILED,
                score=0.0,
                issues=[{"severity": "error", "message": "No code artifacts for security check"}],
                suggestions=["Provide code artifacts for security analysis"],
            )

        score = self._add_dangerous_pattern_issues(code, score, issues, suggestions)
        score = self._add_secret_pattern_issues(code, score, issues, suggestions)

        score = max(0.0, min(1.0, score))
        result_enum = self._score_to_result(score, config.min_score)

        return GateCheckResult(
            gate_name=config.name,
            mode=config.mode,
            result=result_enum,
            score=round(score, 3),
            issues=issues,
            suggestions=suggestions,
        )

    @staticmethod
    def _add_dangerous_pattern_issues(
        code: str,
        score: float,
        issues: list[dict[str, Any]],
        suggestions: list[str],
    ) -> float:
        dangerous_patterns = [
            (r"eval\s*\(", "eval() allows arbitrary code execution", "critical"),
            (r"exec\s*\(", "exec() allows arbitrary code execution", "critical"),
            (r"__import__\s*\(", "Dynamic __import__() may be unsafe", "high"),
            (r"os\.system\s*\(", "os.system() is vulnerable to shell injection", "high"),
            (r"subprocess.*shell\s*=\s*True", "subprocess with shell=True is dangerous", "high"),
            (r"pickle\.loads?\s*\(", "pickle deserialization can execute arbitrary code", "high"),
            (r"yaml\.load\s*\((?!.*Loader)", "yaml.load without Loader is unsafe", "medium"),
            (r"input\s*\(", "input() in production code may be unintended", "low"),
        ]
        severity_penalties = {"critical": 0.3, "high": 0.2, "medium": 0.1, "low": 0.05}
        for pattern, message, severity in dangerous_patterns:
            matches = re.findall(pattern, code)
            if not matches:
                continue
            score -= severity_penalties.get(severity, 0.1)
            issues.append({"severity": severity, "category": "security", "message": message, "count": len(matches)})
            suggestions.append(f"Review and mitigate: {message}")
        return score

    @staticmethod
    def _add_secret_pattern_issues(
        code: str,
        score: float,
        issues: list[dict[str, Any]],
        suggestions: list[str],
    ) -> float:
        secret_patterns = [
            (r'(?:password|passwd|pwd)\s*=\s*["\'][^"\']+["\']', "Possible hardcoded password"),
            (r'(?:api_key|apikey|api_secret)\s*=\s*["\'][^"\']+["\']', "Possible hardcoded API key"),
            (r'(?:secret|token)\s*=\s*["\'][A-Za-z0-9+/=]{20,}["\']', "Possible hardcoded secret/token"),
        ]
        for pattern, message in secret_patterns:
            if not re.search(pattern, code, re.IGNORECASE):
                continue
            score -= 0.25
            issues.append({"severity": "critical", "category": "secrets", "message": message})
            suggestions.append("Move secrets to environment variables or a secrets manager")
        return score

    def check_coverage(self, artifacts: dict[str, Any], config: QualityGateConfig) -> GateCheckResult:
        """Run coverage verification.

        Checks ``artifacts["tests"]`` for test existence and
        ``artifacts["coverage_percent"]`` for coverage threshold.
        Also checks ``artifacts["code"]`` for testable functions
        without corresponding tests.

        Args:
            artifacts: Dict optionally containing ``"tests"``, ``"code"``,
                and ``"coverage_percent"`` keys.
            config: The quality gate configuration for this check.

        Returns:
            GateCheckResult reflecting the coverage outcome.
        """
        tests = artifacts.get("tests", "")
        code = artifacts.get("code", "")
        coverage_pct = artifacts.get("coverage_percent")
        issues: list[dict[str, Any]] = []
        suggestions: list[str] = []
        score = 1.0

        if not tests:
            score -= 0.4
            issues.append(
                {
                    "severity": "error",
                    "category": "coverage",
                    "message": "No test artifacts provided",
                },
            )
            suggestions.append("Write tests for the implemented code")

        score = self._add_coverage_percent_issues(coverage_pct, score, issues)
        score = self._add_untested_function_issues(code, tests, score, issues, suggestions)

        score = max(0.0, min(1.0, score))
        result_enum = self._score_to_result(score, config.min_score)

        return GateCheckResult(
            gate_name=config.name,
            mode=config.mode,
            result=result_enum,
            score=round(score, 3),
            issues=issues,
            suggestions=suggestions,
        )

    @staticmethod
    def _add_coverage_percent_issues(
        coverage_pct: Any,
        score: float,
        issues: list[dict[str, Any]],
    ) -> float:
        if coverage_pct is None:
            return score
        try:
            cov = float(coverage_pct)
        except (ValueError, TypeError):
            logger.warning("Could not parse coverage value from analysis output")
            return score
        if cov < 50:
            score -= 0.3
            issues.append({
                "severity": "error",
                "category": "coverage",
                "message": f"Test coverage is {cov}%, below 50% minimum",
            })
        elif cov < 70:
            score -= 0.15
            issues.append({
                "severity": "warning",
                "category": "coverage",
                "message": f"Test coverage is {cov}%, below 70% target",
            })
        elif cov < 80:
            score -= 0.05
            issues.append({
                "severity": "info",
                "category": "coverage",
                "message": f"Test coverage is {cov}%, consider improving to 80%+",
            })
        return score

    @staticmethod
    def _add_untested_function_issues(
        code: str,
        tests: str,
        score: float,
        issues: list[dict[str, Any]],
        suggestions: list[str],
    ) -> float:
        if not code or not tests:
            return score
        code_fns = set(re.findall(r"def\s+(\w+)\s*\(", code))
        test_fns_raw = set(re.findall(r"def\s+(test_\w+)\s*\(", tests))
        public_fns = {fn for fn in code_fns if not fn.startswith("_")}
        tested_fns = {
            code_fn for code_fn in public_fns if any(code_fn.lower() in test_fn.lower() for test_fn in test_fns_raw)
        }
        untested = public_fns - tested_fns
        if untested and public_fns:
            score -= min(0.3, len(untested) / len(public_fns) * 0.3)
            names = ", ".join(sorted(untested)[:5])
            issues.append({
                "severity": "warning",
                "category": "coverage",
                "message": f"{len(untested)} public function(s) appear untested: {names}",
            })
            suggestions.append("Add tests for untested public functions")
        return score

    def check_architecture(self, artifacts: dict[str, Any], config: QualityGateConfig) -> GateCheckResult:
        """Run architecture verification.

        Checks ``artifacts["code"]`` for architectural consistency including
        circular imports, layer violations, and naming conventions.

        Args:
            artifacts: Dict optionally containing ``"code"`` and
                ``"architecture"`` (dict with optional ``"package_name"``
                and ``"forbidden_patterns"`` keys).
            config: The quality gate configuration for this check.

        Returns:
            GateCheckResult reflecting the architecture analysis outcome.
        """
        code = artifacts.get("code", "")
        architecture = artifacts.get("architecture", {})
        issues: list[dict[str, Any]] = []
        suggestions: list[str] = []
        score = 1.0

        if not code:
            return GateCheckResult(
                gate_name=config.name,
                mode=config.mode,
                result=GateResult.WARNING,
                score=0.5,
                issues=[{"severity": "warning", "message": "No code artifacts for architecture check"}],
                suggestions=["Provide code artifacts for architecture analysis"],
            )

        score = self._add_import_architecture_issues(code, architecture, score, issues, suggestions)
        score = self._add_module_size_architecture_issues(code, score, issues, suggestions)
        score = self._add_forbidden_architecture_issues(code, architecture, score, issues)

        score = max(0.0, min(1.0, score))
        result_enum = self._score_to_result(score, config.min_score)

        return GateCheckResult(
            gate_name=config.name,
            mode=config.mode,
            result=result_enum,
            score=round(score, 3),
            issues=issues,
            suggestions=suggestions,
        )

    @staticmethod
    def _add_import_architecture_issues(
        code: str,
        architecture: dict[str, Any],
        score: float,
        issues: list[dict[str, Any]],
        suggestions: list[str],
    ) -> float:
        wildcard_imports = re.findall(r"^from\s+\S+\s+import\s+\*", code, re.MULTILINE)
        if wildcard_imports:
            score -= min(0.15, len(wildcard_imports) * 0.05)
            issues.append({
                "severity": "warning",
                "category": "architecture",
                "message": f"Found {len(wildcard_imports)} wildcard import(s)",
            })
            suggestions.append("Replace wildcard imports with explicit imports")
        imports = re.findall(r"^(?:from|import)\s+([\w.]+)", code, re.MULTILINE)
        pkg = architecture.get("package_name")
        back_imports = [item for item in imports if pkg and item.startswith(pkg)]
        if len(back_imports) > 5:
            score -= 0.1
            issues.append({
                "severity": "warning",
                "category": "architecture",
                "message": f"High internal coupling: {len(back_imports)} intra-package imports",
            })
            suggestions.append("Consider reducing coupling between modules")
        return score

    @staticmethod
    def _add_module_size_architecture_issues(
        code: str,
        score: float,
        issues: list[dict[str, Any]],
        suggestions: list[str],
    ) -> float:
        classes = re.findall(r"^class\s+(\w+)", code, re.MULTILINE)
        if len(classes) <= 5:
            return score
        score -= 0.1
        issues.append({
            "severity": "warning",
            "category": "architecture",
            "message": f"File defines {len(classes)} classes, consider splitting",
        })
        suggestions.append("Split large modules into focused, single-responsibility files")
        return score

    @staticmethod
    def _add_forbidden_architecture_issues(
        code: str,
        architecture: dict[str, Any],
        score: float,
        issues: list[dict[str, Any]],
    ) -> float:
        for pattern_info in architecture.get("forbidden_patterns", []):
            pat = pattern_info if isinstance(pattern_info, str) else pattern_info.get("pattern", "")
            if not pat or not re.search(pat, code):
                continue
            score -= 0.2
            msg = (
                pattern_info.get("message", f"Forbidden pattern found: {pat}")
                if isinstance(pattern_info, dict)
                else f"Forbidden pattern found: {pat}"
            )
            issues.append({"severity": "error", "category": "architecture", "message": msg})
        return score
