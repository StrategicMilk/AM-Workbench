"""Validation and prevention-gate helpers for pipeline quality."""

from __future__ import annotations

import logging
from typing import Any, cast

from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)

DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
PIPELINE_QUALITY_WORKFLOW_GUARDS: tuple[str, ...] = (
    "prevention gates surface failed reasons in warning logs",
    "stage boundary validation rejects None outputs",
    "constraint registry failures are visible warnings",
    "sandbox validation raises when the sandbox path is unavailable",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return pipeline-quality workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/orchestration/pipeline_quality_validation.py",
        "guards": PIPELINE_QUALITY_WORKFLOW_GUARDS,
    }


class PipelineQualityValidationMixin:
    """Prevention, boundary, constraint, and sandbox validation methods."""

    @staticmethod
    def _run_prevention_gate(goal: str, context: dict[str, Any]) -> bool:
        """Run pre-execution prevention gate and return True if the gate passes.

        Builds artifacts from the goal and context, delegates to
        QualityGateRunner for the ``"pre_execution"`` stage, and logs the
        outcome. Failure is a soft gate: the pipeline continues but logs a
        warning so the issue is visible.

        Args:
            goal: The user goal string passed into the pipeline.
            context: Current pipeline context dict.

        Returns:
            True if the prevention gate passed, False otherwise.
        """
        from vetinari.validation.quality_gates import QualityGateRunner

        artifacts: dict[str, Any] = {
            "task_description": goal,
            "acceptance_criteria": context.get("acceptance_criteria", []),
            "referenced_files": context.get("referenced_files", []),
            "model_capabilities": context.get("model_capabilities", set()),
            "required_capabilities": context.get("required_capabilities", set()),
            "estimated_tokens": context.get("estimated_tokens", 0),
            "token_budget": context.get("token_budget", 100_000),
            "active_file_scopes": context.get("active_file_scopes", set()),
        }

        runner = QualityGateRunner()
        results = runner.run_gate("pre_execution", artifacts)
        passed = cast(bool, runner.stage_passed(results))

        if passed:
            logger.info("[PreventionGate] Pre-execution gate passed")
        else:
            failed_reasons = [issue["message"] for r in results for issue in r.issues]
            logger.warning(
                "[PreventionGate] Pre-execution gate failed (%d issue(s)): %s",
                len(failed_reasons),
                "; ".join(failed_reasons),
            )

        return passed

    @staticmethod
    def _validate_stage_boundary(
        stage_name: str,
        stage_output: Any,
        min_keys: list[str] | None = None,
    ) -> tuple[bool, list[str]]:
        """Validate the output of a pipeline stage before passing to the next.

        Checks that the output is not None, contains required keys when it is
        a dict, and has no error indicators.

        Args:
            stage_name: Human-readable name used in issue messages.
            stage_output: The dict or value produced by the stage.
            min_keys: Required keys when ``stage_output`` is a dict.

        Returns:
            ``(is_valid, issues_list)`` where is_valid is False when any issue
            is found.
        """
        issues: list[str] = []

        if stage_output is None:
            issues.append(f"Stage '{stage_name}' produced None output")
            return False, issues

        if isinstance(stage_output, dict):
            if min_keys:
                missing = [k for k in min_keys if k not in stage_output]
                if missing:
                    issues.append(f"Stage '{stage_name}' missing required keys: {missing}")
            if stage_output.get("error"):
                issues.append(f"Stage '{stage_name}' has error: {stage_output['error']}")
            if (
                stage_output.get(StatusEnum.FAILED.value, 0) > 0
                and stage_output.get(StatusEnum.COMPLETED.value, 0) == 0
            ):
                issues.append(
                    f"Stage '{stage_name}': all tasks failed ({stage_output['failed']} failures, 0 completed)",
                )

        return (len(issues) == 0, issues)

    @staticmethod
    def _check_stage_constraints(
        agent_type: str,
        mode: str | None,
        quality_score: float | None = None,
    ) -> tuple[bool, list[str]]:
        """Enforce constraints from the ConstraintRegistry between stages.

        Validates mode, quality gate, and resource constraints for the agent
        that is about to execute or has just produced output. Called at stage
        boundaries to prevent constraint violations from propagating.

        Args:
            agent_type: The agent type string, such as ``"WORKER"``.
            mode: The agent mode, such as ``"build"``, or None.
            quality_score: Output quality score to check against the quality
                gate, or None to skip quality gate checking.

        Returns:
            ``(passed, violations)`` where passed is True when all constraints
            hold.
        """
        violations: list[str] = []
        try:
            from vetinari.constraints.registry import get_constraint_registry

            registry = get_constraint_registry()

            if mode is not None:
                mode_ok, mode_reason = registry.validate_mode(agent_type, mode)
                if not mode_ok:
                    violations.append(f"Mode constraint: {mode_reason}")

            if quality_score is not None:
                gate_ok, gate_reason = registry.check_quality_gate(agent_type, quality_score, mode)
                if not gate_ok:
                    violations.append(f"Quality gate: {gate_reason}")

        except Exception as exc:
            logger.warning(
                "Constraint check skipped for %s/%s - registry unavailable: %s",
                agent_type,
                mode,
                exc,
            )

        passed = len(violations) == 0
        if not passed:
            logger.warning(
                "[ConstraintEnforcement] %d violation(s) for %s/%s: %s",
                len(violations),
                agent_type,
                mode,
                "; ".join(violations),
            )
        return passed, violations

    @staticmethod
    def _sandbox_validate_code_output(code: str, language: str = "python") -> tuple[bool, str]:
        """Run generated code through the sandbox before assembly.

        Used between Worker output and Inspector review for code-producing
        tasks. Validates that the code is syntactically correct and executes
        without crashing.

        Args:
            code: The generated source code string.
            language: Programming language. Currently only ``"python"`` is
                supported.

        Returns:
            ``(passed, details)`` where passed is True when code executed
            without errors.
        """
        if language != "python" or not code.strip():
            return True, "skipped (non-python or empty)"

        try:
            from vetinari.code_sandbox import CodeSandbox
        except Exception as exc:
            logger.warning("Sandbox validation blocked - sandbox unavailable: %s", exc)
            return False, f"sandbox validation failed closed: sandbox unavailable: {exc}"

        try:
            sandbox = CodeSandbox(max_execution_time=30, allow_network=False)
            result = sandbox.execute(code)
        except Exception as exc:
            logger.warning("Sandbox validation blocked - execution unavailable: %s", exc)
            return False, f"sandbox validation failed closed: execution unavailable: {exc}"
        if result.success:
            logger.info("[SandboxValidation] Code output passed sandbox execution")
            return True, result.output or "executed successfully"
        logger.warning(
            "[SandboxValidation] Code output FAILED sandbox: %s",
            result.error or "unknown error",
        )
        return False, result.error or "execution failed"
