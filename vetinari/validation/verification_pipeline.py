"""Concrete verification checks and pipeline orchestration."""

from __future__ import annotations

import abc
import ast
import json
import logging
import re
import time
from abc import ABC
from typing import Any

from vetinari.security import get_secret_scanner
from vetinari.validation.verification_types import (
    ValidationVerificationResult,
    VerificationIssue,
    VerificationLevel,
    VerificationStatus,
)

logger = logging.getLogger(__name__)

DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
VERIFICATION_PIPELINE_WORKFLOW_GUARDS: tuple[str, ...] = (
    "syntax verification fails closed on empty code input",
    "security verification fails when secret patterns are detected",
    "import verification blocks unsafe Python module roots",
    "pipeline summaries require at least one passed check and no failed checks",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return verification-pipeline workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/validation/verification_pipeline.py",
        "guards": VERIFICATION_PIPELINE_WORKFLOW_GUARDS,
    }


class Verifier(ABC):
    """Base class for verification checks."""

    def __init__(self, name: str):
        self.name = name

    @abc.abstractmethod
    def verify(self, content: Any) -> ValidationVerificationResult:
        """Execute the verification check."""


class CodeSyntaxVerifier(Verifier):
    """Verifies Python code syntax."""

    def __init__(self):
        super().__init__("code_syntax")

    def verify(self, content: str) -> ValidationVerificationResult:
        """Check if content is valid Python syntax.

        Args:
            content: Python source code, optionally wrapped in fences.

        Returns:
            Verification result for syntax correctness.
        """
        start = time.monotonic()

        result = ValidationVerificationResult(
            status=VerificationStatus.PASSED,
            check_name=self.name,
        )

        if not isinstance(content, str) or not content.strip():
            result.status = VerificationStatus.FAILED
            result.issues.append(
                VerificationIssue(
                    severity="error",
                    category="syntax",
                    message="empty or non-string input - cannot certify code syntax",
                )
            )
            return result

        cleaned = re.sub(r"```[\w]*\n", "\n", content)
        cleaned = re.sub(r"```$", "", cleaned)
        cleaned = cleaned.strip()

        if not cleaned:
            result.status = VerificationStatus.FAILED
            result.issues.append(
                VerificationIssue(
                    severity="error",
                    category="syntax",
                    message="input contains only markdown fences with no code body - cannot certify syntax",
                )
            )
            return result

        try:
            ast.parse(cleaned)
            logger.info("Code syntax validation passed")
        except SyntaxError as e:
            result.status = VerificationStatus.FAILED
            result.issues.append(
                VerificationIssue(
                    severity="error",
                    category="syntax",
                    message=f"Syntax error: {e!s}",
                    location=f"line {e.lineno}",
                ),
            )
        except Exception as e:
            result.status = VerificationStatus.WARNING
            result.issues.append(
                VerificationIssue(
                    severity="warning",
                    category="syntax",
                    message=f"Could not parse code: {e!s}",
                ),
            )

        result.execution_time_ms = int((time.monotonic() - start) * 1000)
        return result


class SecurityVerifier(Verifier):
    """Verifies content for security issues."""

    def __init__(self):
        super().__init__("security")
        self.scanner = get_secret_scanner()

    def verify(self, content: str) -> ValidationVerificationResult:
        """Check for security issues in content.

        Args:
            content: Text or code to scan.

        Returns:
            Verification result for secrets and unsafe patterns.
        """
        start = time.monotonic()

        result = ValidationVerificationResult(
            status=VerificationStatus.PASSED,
            check_name=self.name,
        )

        if not isinstance(content, str):
            result.status = VerificationStatus.SKIPPED
            return result

        secrets = self.scanner.scan(content)
        for pattern, _matches in secrets.items():
            result.status = VerificationStatus.FAILED
            result.issues.append(
                VerificationIssue(
                    severity="error",
                    category="security",
                    message=f"Potential secret detected: {pattern}",
                    suggestion="Sanitize sensitive information before storing",
                ),
            )

        dangerous_patterns = [
            (r"exec\s*\(", "exec() allows arbitrary code execution"),
            (r"eval\s*\(", "eval() is unsafe"),
            (r"__import__\s*\(", "Direct imports may be unsafe"),
            (r"os\.system\s*\(", "os.system() is unsafe"),
            (
                r"subprocess\.(?:run|Popen|call|check_call|check_output)\s*\([^)]*shell\s*=\s*True",
                "shell=True in subprocess is dangerous",
            ),
            (r"pickle\.loads?\s*\(", "pickle deserialization can execute attacker-controlled payloads"),
            (r"yaml\.load\s*\((?![^)]*SafeLoader)", "yaml.load without SafeLoader is unsafe"),
            (r"marshal\.loads?\s*\(", "marshal deserialization is unsafe for untrusted data"),
            (r"compile\s*\(", "compile() enables dynamic code execution"),
        ]

        for pattern, message in dangerous_patterns:
            if re.search(pattern, content, re.DOTALL):
                result.status = VerificationStatus.FAILED
                result.issues.append(
                    VerificationIssue(
                        severity="error",
                        category="security",
                        message=message,
                        suggestion="Use safer alternatives",
                    ),
                )

        result.execution_time_ms = int((time.monotonic() - start) * 1000)
        return result


class ImportVerifier(Verifier):
    """Verifies Python imports are safe and available."""

    def __init__(self, allowed_modules: list[str] | None = None):
        super().__init__("imports")
        self.allowed_modules = allowed_modules or []
        self.blocked_modules = ["ctypes", "importlib", "mmap", "msvcrt", "os", "socket", "subprocess", "winreg"]

    def verify(self, content: str) -> ValidationVerificationResult:
        """Check Python imports in content.

        Args:
            content: Python source code to inspect.

        Returns:
            Verification result for blocked import usage.
        """
        start = time.monotonic()

        result = ValidationVerificationResult(
            status=VerificationStatus.PASSED,
            check_name=self.name,
        )

        if not isinstance(content, str):
            result.status = VerificationStatus.SKIPPED
            return result

        imports = self._extract_imports(content)

        for imp in imports:
            module = imp.split(".")[0]

            if module in self.blocked_modules:
                result.status = VerificationStatus.FAILED
                result.issues.append(
                    VerificationIssue(
                        severity="error",
                        category="import",
                        message=f"Import '{module}' is unsafe",
                    ),
                )

        result.execution_time_ms = int((time.monotonic() - start) * 1000)
        return result

    @staticmethod
    def _extract_imports(content: str) -> list[str]:
        """Extract import statements from code."""
        imports: list[str] = []
        for match in re.finditer(r"^import\s+(.+)$", content, re.MULTILINE):
            imports.extend(part.strip().split(" as ", 1)[0] for part in match.group(1).split(",") if part.strip())
        imports.extend(match.group(1) for match in re.finditer(r"^from\s+([\w.]+)\s+import", content, re.MULTILINE))
        return imports


class JSONStructureVerifier(Verifier):
    """Verifies JSON structure and completeness."""

    def __init__(self, required_fields: list[str] | None = None):
        super().__init__("json_structure")
        self.required_fields = required_fields or []

    def verify(self, content: str) -> ValidationVerificationResult:
        """Check JSON structure and required fields.

        Args:
            content: JSON text, optionally wrapped in fences.

        Returns:
            Verification result for parse and required-field checks.
        """
        start = time.monotonic()

        result = ValidationVerificationResult(
            status=VerificationStatus.PASSED,
            check_name=self.name,
        )

        if not isinstance(content, str):
            result.status = VerificationStatus.SKIPPED
            return result

        json_str = content.strip()
        if json_str.startswith("```"):
            match = re.search(r"```(?:json)?\s*(.*?)\s*```", json_str, re.DOTALL)
            if match:
                json_str = match.group(1)

        try:
            data = json.loads(json_str)

            if isinstance(data, dict):
                for field in self.required_fields:
                    if field not in data:
                        result.status = VerificationStatus.WARNING
                        result.issues.append(
                            VerificationIssue(
                                severity="warning",
                                category="structure",
                                message=f"Missing required field: {field}",
                            ),
                        )
        except json.JSONDecodeError as e:
            result.status = VerificationStatus.FAILED
            result.issues.append(
                VerificationIssue(
                    severity="error",
                    category="structure",
                    message=f"Invalid JSON: {e!s}",
                ),
            )

        result.execution_time_ms = int((time.monotonic() - start) * 1000)
        return result


class VerificationPipeline:
    """Pipeline of verification checks."""

    def __init__(self, level: VerificationLevel = VerificationLevel.STANDARD):
        self.level = level
        self.verifiers: list[Verifier] = []
        self._setup_verifiers()

    def _setup_verifiers(self) -> None:
        """Setup verifiers based on verification level."""
        if self.level in (
            VerificationLevel.BASIC,
            VerificationLevel.STANDARD,
            VerificationLevel.STRICT,
            VerificationLevel.PARANOID,
        ):
            self.verifiers.append(CodeSyntaxVerifier())
            self.verifiers.append(SecurityVerifier())
            self.verifiers.append(ImportVerifier())

    def add_verifier(self, verifier: Verifier) -> None:
        """Add a custom verifier to the pipeline."""
        self.verifiers.append(verifier)

    def verify(self, content: Any) -> dict[str, ValidationVerificationResult]:
        """Run all verifiers on content.

        Args:
            content: Content passed to each configured verifier.

        Returns:
            Mapping of verifier name to verification result.
        """
        results = {}

        for verifier in self.verifiers:
            try:
                results[verifier.name] = verifier.verify(content)
            except Exception as e:
                logger.error("Error in verifier %s: %s", verifier.name, e)
                results[verifier.name] = ValidationVerificationResult(
                    status=VerificationStatus.SKIPPED,
                    check_name=verifier.name,
                )

        return results

    def get_summary(self, results: dict[str, ValidationVerificationResult]) -> dict[str, Any]:
        """Aggregate verification results into a pipeline summary.

        Args:
            results: Mapping returned by verify().

        Returns:
            Summary with overall status, counts, and per-check details.
        """
        total_issues = sum(len(r.issues) for r in results.values())
        total_errors = sum(r.error_count for r in results.values())
        total_warnings = sum(r.warning_count for r in results.values())

        has_passed = any(r.status == VerificationStatus.PASSED for r in results.values())
        has_failures = any(
            r.status in {VerificationStatus.FAILED, VerificationStatus.SKIPPED} for r in results.values()
        )
        overall_passed = bool(results) and has_passed and not has_failures

        return {
            "overall_status": "PASSED" if overall_passed and not has_failures else "FAILED",
            "all_passed": overall_passed,
            "total_checks": len(results),
            "total_issues": total_issues,
            "error_count": total_errors,
            "warning_count": total_warnings,
            "checks": {name: r.to_dict() for name, r in results.items()},
        }
