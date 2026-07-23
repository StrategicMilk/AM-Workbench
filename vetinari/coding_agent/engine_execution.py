"""Execution and validation helpers for CodeAgentEngine."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.agents.contracts import AgentTask
from vetinari.security.fail_closed import SandboxUnavailableError, require_sandbox_or_raise
from vetinari.types import CodingTaskType, StatusEnum

from .engine_models import CodeArtifact, _CodeTask

logger = logging.getLogger(__name__)


class CodeAgentExecutionMixin:
    """Execution behavior shared by the coding agent facade."""

    if TYPE_CHECKING:
        _generate_generic: Any
        _generate_implementation: Any
        _generate_review: Any
        _generate_scaffold: Any
        _generate_tests: Any
        _generate_via_llm: Any
        _validate_via_sandbox: Any
        enabled: Any

    def run_task(self, task: _CodeTask | AgentTask) -> CodeArtifact:
        """Execute a coding task and return a generated code artifact.

        Accepts either the internal ``_CodeTask`` or a public ``AgentTask``
        created by ``make_code_agent_task()``. AgentTasks are converted
        to _CodeTask internally.

        Args:
            task: The coding task to execute. AgentTasks are auto-converted.

        Returns:
            A CodeArtifact containing the generated code, diff, or review output.

        Raises:
            RuntimeError: If the coding agent is disabled via CODING_AGENT_ENABLED.
        """
        if not self.enabled:
            raise RuntimeError("Coding agent is not enabled")

        if isinstance(task, AgentTask):
            task = _CodeTask.from_agent_task(task)

        task.status = StatusEnum.IN_PROGRESS
        task.updated_at = datetime.now(timezone.utc).isoformat()

        logger.info("Executing coding task: %s (%s)", task.task_id, task.type.value)

        try:
            artifact = self._run_in_process(task)

            task.status = StatusEnum.COMPLETED
            task.updated_at = datetime.now(timezone.utc).isoformat()

            return artifact

        except Exception as e:
            logger.error("Coding task failed: %s", e)
            task.status = StatusEnum.FAILED
            task.updated_at = datetime.now(timezone.utc).isoformat()
            raise

    def _run_in_process(self, task: _CodeTask) -> CodeArtifact:
        """Run task using AdapterManager (LLM-powered) with template fallback.

        Generated Python artifacts are validated through the embedded sandbox
        before being returned. Sandbox failures are logged as warnings but do
        not abort delivery of the artifact; the caller receives the generated
        content regardless, with sandbox diagnostics attached to the provenance.
        """
        llm_result = self._generate_via_llm(task)
        if llm_result:
            return self._validate_via_sandbox(llm_result, task)

        if task.type == CodingTaskType.SCAFFOLD:
            artifact = self._generate_scaffold(task)
        elif task.type == CodingTaskType.IMPLEMENT:
            artifact = self._generate_implementation(task)
        elif task.type == CodingTaskType.TEST:
            artifact = self._generate_tests(task)
        elif task.type == CodingTaskType.REVIEW:
            return self._generate_review(task)
        else:
            artifact = self._generate_generic(task)

        return self._validate_via_sandbox(artifact, task)

    def run_multi_step_task(self, tasks: list[_CodeTask | AgentTask]) -> list[CodeArtifact]:
        """Run a sequence of coding tasks, returning an artifact for each.

        Tasks are executed in order. If any task fails, the exception is
        re-raised immediately and subsequent tasks are not run.

        Args:
            tasks: Ordered list of coding tasks to execute (e.g. scaffold, implement, test).

        Returns:
            List of CodeArtifacts in the same order as the input tasks.

        Raises:
            Exception: Re-raises any exception from a failed task when bridge mode is disabled.
        """
        artifacts = []

        for task in tasks:
            try:
                artifact = self.run_task(task)
                artifacts.append(artifact)
            except Exception as e:
                logger.error("Task %s failed: %s", task.task_id, e)
                raise

        return artifacts


class CodeAgentValidationMixin:
    """Validation behavior shared by the coding agent facade."""

    if TYPE_CHECKING:
        _sandbox: Any

    def _validate_via_sandbox(self, artifact: CodeArtifact, task: _CodeTask) -> CodeArtifact:
        """Execute the artifact's Python content through the embedded sandbox.

        Only Python artifacts are executed. Review artifacts (markdown) and
        artifacts for non-Python languages are skipped. The artifact is always
        returned; sandbox results are recorded in the provenance field for
        diagnostics.

        Args:
            artifact: The generated code artifact to validate.
            task: The originating code task (used for language context).

        Returns:
            The original artifact, with provenance updated to include sandbox
            execution status.
        """
        is_python = task.language.lower() == "python" and artifact.language.lower() in ("python", "")
        is_review = task.type == CodingTaskType.REVIEW
        is_test = task.type == CodingTaskType.TEST
        has_code = bool(artifact.content and artifact.content.strip())

        if not is_python or is_review or is_test or not has_code:
            return artifact

        try:
            sandbox = require_sandbox_or_raise(self._sandbox, label="code artifact sandbox")
            result = sandbox.execute_python(artifact.content)
            if not result.success:
                error_detail = result.error[:200] if result.error else "(no error output)"
                logger.error(
                    "Sandbox validation failed for task %s: %s",
                    task.task_id,
                    error_detail,
                )
                artifact = replace(artifact, provenance=f"{artifact.provenance}|sandbox_error")
                raise RuntimeError(f"Generated code failed sandbox validation for task {task.task_id}: {error_detail}")
            logger.debug("Sandbox validation passed for task %s", task.task_id)
            artifact = replace(artifact, provenance=f"{artifact.provenance}|sandbox_ok")
        except RuntimeError:
            raise
        except SandboxUnavailableError:
            logger.error("Sandbox unavailable for generated code validation on task %s", task.task_id)
            raise
        except Exception as e:
            logger.warning("Sandbox execution raised an exception for task %s: %s", task.task_id, e)
            artifact = replace(artifact, provenance=f"{artifact.provenance}|sandbox_unavailable")

        return artifact

    def validate(self, artifact: CodeArtifact) -> tuple[bool, list[str]]:
        """Validate a code artifact for correctness and completeness.

        Checks that the artifact has code, passes syntax validation, and meets
        quality thresholds. Returns (passed, list_of_issues).

        Args:
            artifact: The CodeArtifact to validate.

        Returns:
            Tuple of (passed: bool, issues: list[str]).
        """
        issues: list[str] = []
        if not artifact.content or not artifact.content.strip():
            issues.append("Artifact has no code")
        else:
            try:
                import ast as _ast

                _ast.parse(artifact.content)
            except SyntaxError as e:
                issues.append(f"Syntax error: {e}")
        quality_score = getattr(artifact, "quality_score", None)
        if quality_score is not None and quality_score < 0.5:
            issues.append(f"Quality score {quality_score:.2f} below threshold 0.50")
        return len(issues) == 0, issues
