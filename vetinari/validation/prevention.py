"""Poka-Yoke prevention layer for Department 5 of the Vetinari factory pipeline.

Validates task inputs BEFORE Builder executes, making it structurally impossible
for Builder to receive a task it cannot succeed at. Each check is a discrete
gate; all gates run and failures are collected before a recommendation is issued.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────────

MIN_DESCRIPTION_LENGTH = 20  # characters; shorter descriptions are too vague
TOKEN_BUDGET_THRESHOLD = 0.9  # fail if estimated tokens exceed 90% of budget


# ── Dataclasses ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Result of a single prevention check.

    Attributes:
        passed: Whether the check passed.
        reason: Human-readable explanation when the check fails.
    """

    passed: bool
    reason: str = ""


@dataclass
class PreventionResult:
    """Aggregate result of all prevention checks for a task.

    Attributes:
        passed: True only when every individual check passed.
        failures: List of CheckResult objects that did not pass.
        recommendation: Action the orchestrator should take next.
    """

    passed: bool
    failures: list[CheckResult] = field(default_factory=list)
    recommendation: str = "proceed"


# ── Prevention gate ────────────────────────────────────────────────────────────


class PreventionGate:
    """Poka-Yoke gate that validates a task before Builder receives it.

    All checks are run regardless of earlier failures so that the caller
    receives a complete picture of every problem with the task description.
    """

    def validate(
        self,
        task_description: str,
        acceptance_criteria: list[str],
        referenced_files: list[str],
        model_capabilities: set[str],
        required_capabilities: set[str],
        estimated_tokens: int,
        token_budget: int,
        active_file_scopes: set[str],
    ) -> PreventionResult:
        """Run all prevention checks and return an aggregated result.

        Args:
            task_description: Free-text description of the work to be done.
            acceptance_criteria: List of criteria that define task completion.
            referenced_files: File paths the task intends to read or modify.
            model_capabilities: Capability tags advertised by the chosen model.
            required_capabilities: Capability tags the task requires from a model.
            estimated_tokens: Estimated total token cost of the task.
            token_budget: Maximum tokens allowed for this task.
            active_file_scopes: File paths currently locked by another in-flight task.

        Returns:
            PreventionResult with pass/fail status, all failure details, and a
            recommended next action for the orchestrator.
        """
        checks = [
            self._check_acceptance_criteria(acceptance_criteria),
            self._check_files_exist(referenced_files),
            self._check_context_completeness(task_description, acceptance_criteria),
            self._check_model_capability(model_capabilities, required_capabilities),
            self._check_token_budget(estimated_tokens, token_budget),
            self._check_no_concurrent_conflicts(referenced_files, active_file_scopes),
        ]

        failures = [c for c in checks if not c.passed]
        passed = len(failures) == 0

        if passed:
            logger.info("PreventionGate: all checks passed for task")
        else:
            for failure in failures:
                logger.warning("PreventionGate check failed: %s", failure.reason)

        recommendation = self._recommend_action(failures)
        return PreventionResult(passed=passed, failures=failures, recommendation=recommendation)

    # ── Individual checks ──────────────────────────────────────────────────────

    @staticmethod
    def _check_acceptance_criteria(criteria: list[str]) -> CheckResult:
        """Fail if no acceptance criteria are provided.

        Args:
            criteria: List of acceptance criteria strings.

        Returns:
            CheckResult indicating whether criteria are present.
        """
        if not criteria:
            return CheckResult(
                passed=False,
                reason="No acceptance criteria provided; Builder cannot know when the task is done.",
            )
        logger.info("_check_acceptance_criteria: passed (%d criteria)", len(criteria))
        return CheckResult(passed=True)

    @staticmethod
    def _check_files_exist(files: list[str]) -> CheckResult:
        """Fail if any referenced file does not exist on disk.

        Args:
            files: List of file path strings to check.

        Returns:
            CheckResult naming any missing files.
        """
        missing = [f for f in files if not Path(f).exists()]
        if missing:
            return CheckResult(
                passed=False,
                reason=f"Referenced files do not exist: {', '.join(missing)}",
            )
        logger.info("_check_files_exist: all %d files exist", len(files))
        return CheckResult(passed=True)

    @staticmethod
    def _check_context_completeness(
        task_description: str,
        acceptance_criteria: list[str],
    ) -> CheckResult:
        """Fail if the description is too short or acceptance criteria are absent.

        Args:
            task_description: Free-text task description.
            acceptance_criteria: List of criteria strings.

        Returns:
            CheckResult describing any context deficiency.
        """
        if len(task_description) < MIN_DESCRIPTION_LENGTH:
            return CheckResult(
                passed=False,
                reason=f"Task description is too short ({len(task_description)} chars); minimum is {MIN_DESCRIPTION_LENGTH}.",
            )
        if not acceptance_criteria:
            return CheckResult(
                passed=False,
                reason="Task description is present but acceptance criteria are empty.",
            )
        logger.info("_check_context_completeness: passed")
        return CheckResult(passed=True)

    @staticmethod
    def _check_model_capability(
        model_caps: set[str],
        required_caps: set[str],
    ) -> CheckResult:
        """Fail if the selected model lacks any required capability.

        Args:
            model_caps: Capability tags the chosen model supports.
            required_caps: Capability tags the task requires.

        Returns:
            CheckResult listing any missing capabilities.
        """
        missing = required_caps - model_caps
        if missing:
            return CheckResult(
                passed=False,
                reason=f"Model is missing required capabilities: {', '.join(sorted(missing))}",
            )
        logger.info("_check_model_capability: all required capabilities present")
        return CheckResult(passed=True)

    @staticmethod
    def _check_token_budget(estimated: int, budget: int) -> CheckResult:
        """Fail if estimated token usage exceeds 90% of the token budget.

        Args:
            estimated: Estimated number of tokens the task will consume.
            budget: Maximum tokens allocated to the task.

        Returns:
            CheckResult explaining any budget overrun.
        """
        if budget > 0 and estimated > budget * TOKEN_BUDGET_THRESHOLD:
            return CheckResult(
                passed=False,
                reason=f"Estimated tokens ({estimated}) exceed {int(TOKEN_BUDGET_THRESHOLD * 100)}% of budget ({budget}).",
            )
        logger.info(
            "_check_token_budget: %d estimated vs %d budget",
            estimated,
            budget,
        )
        return CheckResult(passed=True)

    @staticmethod
    def _check_no_concurrent_conflicts(
        referenced_files: list[str],
        active_scopes: set[str],
    ) -> CheckResult:
        """Fail if any referenced file is already locked by an active task.

        Args:
            referenced_files: Files this task intends to modify.
            active_scopes: File paths currently locked by in-flight tasks.

        Returns:
            CheckResult listing any conflicting files.
        """
        conflicts = [f for f in referenced_files if f in active_scopes]
        if conflicts:
            return CheckResult(
                passed=False,
                reason=f"Files already being modified by another task: {', '.join(conflicts)}",
            )
        logger.info("_check_no_concurrent_conflicts: no conflicts")
        return CheckResult(passed=True)

    # ── Recommendation ─────────────────────────────────────────────────────────

    @staticmethod
    def _recommend_action(failures: list[CheckResult]) -> str:
        """Return a single recommended action based on the set of failures.

        Priority order when multiple failure types are present:
        1. Missing model capability  → change_model
        2. Token budget exceeded     → split_task
        3. Context / description     → add_context
        4. Missing criteria only     → clarify_with_user
        5. No failures               → proceed

        Args:
            failures: List of CheckResult objects that did not pass.

        Returns:
            One of: "proceed", "add_context", "change_model", "split_task",
            "clarify_with_user".
        """
        if not failures:
            return "proceed"

        reasons = " ".join(f.reason for f in failures).lower()

        if "capabilities" in reasons:
            return "change_model"
        if "budget" in reasons or "tokens" in reasons:
            return "split_task"
        if "description is too short" in reasons:
            return "add_context"
        # Acceptance-criteria failures with no description issue
        return "clarify_with_user"
