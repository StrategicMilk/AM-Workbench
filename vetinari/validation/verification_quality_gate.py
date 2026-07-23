"""Quality-gate bridge for the validation verification pipeline."""

from __future__ import annotations

import time
from typing import Any

from vetinari.validation.verification import (
    ValidationVerificationResult,
    VerificationIssue,
    VerificationStatus,
    Verifier,
)


class QualityGateVerifier(Verifier):
    """Verifier that wraps QualityGateRunner for integration with VerificationPipeline."""

    def __init__(self, stage: str = "post_execution", custom_gates: dict | None = None):
        """Initialize the quality-gate verifier."""
        super().__init__(f"quality_gate_{stage}")
        self._stage = stage
        from vetinari.validation.quality_gates import QualityGateRunner

        self._runner = QualityGateRunner(custom_gates=custom_gates)

    def verify(self, content: Any) -> ValidationVerificationResult:
        """Run quality gate checks on code strings or artifact dictionaries.

        Returns:
            Value produced for the caller.
        """
        start = time.monotonic()
        artifacts, rejected = self._artifacts_from_content(content)
        if rejected:
            return rejected

        gate_results = self._runner.run_gate(self._stage, artifacts)
        if not gate_results:
            result = ValidationVerificationResult(
                status=VerificationStatus.FAILED,
                check_name=self.name,
                issues=[
                    VerificationIssue(
                        severity="error",
                        category="quality_gate",
                        message=f"No quality gates ran for stage {self._stage!r}; cannot certify quality gate",
                    )
                ],
            )
            result.execution_time_ms = int((time.monotonic() - start) * 1000)
            return result

        issues, status = self._convert_gate_results(gate_results)
        result = ValidationVerificationResult(status=status, check_name=self.name, issues=issues)
        result.execution_time_ms = int((time.monotonic() - start) * 1000)
        return result

    def verify_gate(
        self,
        *,
        evidence: Any | None = None,
        artifacts: dict[str, Any] | None = None,
        code: str | None = None,
        **extra_artifacts: Any,
    ) -> ValidationVerificationResult:
        """Compatibility entrypoint for callers that submit named gate evidence.

        ``QualityGateRunner.run_gate`` consumes an artifact dictionary. Some
        workflow probes call this bridge with named evidence instead; preserve
        that API while still failing closed when no analyzable code artifact is
        present.

        Returns:
            Verification result from the wrapped quality-gate runner.
        """
        payload: dict[str, Any] = {}
        if artifacts is not None:
            if not isinstance(artifacts, dict):
                return self._failed_input_result("artifacts must be a dictionary; cannot certify quality gate")
            payload.update(artifacts)
        if code is not None:
            payload["code"] = code
        if evidence is not None:
            if isinstance(evidence, dict):
                payload.update(evidence)
            elif isinstance(evidence, str):
                payload.setdefault("code", evidence)
            else:
                payload["evidence"] = evidence
        payload.update(extra_artifacts)
        return self.verify(payload)

    def _artifacts_from_content(
        self,
        content: Any,
    ) -> tuple[dict[str, Any], ValidationVerificationResult | None]:
        if content is None or (isinstance(content, str) and not content.strip()):
            return {}, self._failed_input_result("None or empty input; cannot certify quality gate")
        if isinstance(content, dict):
            return content, None
        if isinstance(content, str):
            return {"code": content}, None
        message = f"unsupported input type {type(content).__name__!r}; cannot certify quality gate"
        return {}, self._failed_input_result(message)

    def _failed_input_result(self, message: str) -> ValidationVerificationResult:
        return ValidationVerificationResult(
            status=VerificationStatus.FAILED,
            check_name=self.name,
            issues=[VerificationIssue(severity="error", category="quality_gate", message=message)],
        )

    def _convert_gate_results(self, gate_results: list[Any]) -> tuple[list[VerificationIssue], VerificationStatus]:
        from vetinari.validation.quality_gates import GateResult

        issues: list[VerificationIssue] = []
        has_failure = False
        has_warning = False
        for gate_result in gate_results:
            has_failure = has_failure or gate_result.result == GateResult.FAILED
            has_warning = has_warning or gate_result.result == GateResult.WARNING
            issues.extend(self._issues_from_gate_result(gate_result))
        if has_failure:
            return issues, VerificationStatus.FAILED
        if has_warning:
            return issues, VerificationStatus.WARNING
        return issues, VerificationStatus.PASSED

    @staticmethod
    def _issues_from_gate_result(gate_result: Any) -> list[VerificationIssue]:
        issues = [
            VerificationIssue(
                severity=_normalize_gate_severity(issue.get("severity", "info")),
                category=issue.get("category", gate_result.mode.value),
                message=issue.get("message", ""),
                location=issue.get("location"),
                suggestion=None,
            )
            for issue in gate_result.issues
        ]
        issues.extend(
            VerificationIssue(severity="info", category="suggestion", message=suggestion)
            for suggestion in gate_result.suggestions
        )
        return issues


def _normalize_gate_severity(severity: str) -> str:
    if severity in ("critical", "high"):
        return "error"
    if severity in ("medium", "low"):
        return "warning"
    return severity


__all__ = ["QualityGateVerifier"]
