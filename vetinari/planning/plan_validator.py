"""Plan decomposition validator — checks structural and semantic validity before execution.

Validates that decomposed plans have no cycles, complete dependency chains, adequate
goal coverage, and at least one testable output. Validation failures trigger LLM
re-prompting with specific issues (up to 2 retries).

This runs after goal decomposition (step 2) and before execution plan creation (step 3).
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Any

from vetinari.agents.contracts import Task
from vetinari.boundary_guards import require_nonempty
from vetinari.exceptions import CircularDependencyError
from vetinari.planning.plan_validator_helpers import (
    _DEFAULT_MIN_COVERAGE,
    _TESTABLE_KEYWORDS,
    IssueCategory,
    IssueSeverity,
    ValidationIssue,
    ValidationResult,
    _build_nodes,
    _kahn_cycle_check,
    _tokenize,
)

logger = logging.getLogger(__name__)


# -- Public validation checks -------------------------------------------------


def check_cycles(tasks: list[Task]) -> list[ValidationIssue]:
    """Detect circular dependencies in the task graph using Kahn's algorithm.

    Calls the same topological-sort logic as ``GraphPlanningEngine._topological_sort``
    (vetinari/orchestration/graph_planner.py:108), which raises
    ``CircularDependencyError`` when a cycle is found.

    Args:
        tasks: The decomposed task list to check.

    Returns:
        List containing a single CYCLE/ERROR issue when a cycle is detected,
        or an empty list when the graph is acyclic.
    """
    if not tasks:
        return []

    nodes = _build_nodes(tasks)
    try:
        _kahn_cycle_check(nodes)
    except CircularDependencyError as exc:
        # Find which tasks are implicated (those not reachable in Kahn's pass)
        in_degree = {tid: len(n.dependencies) for tid, n in nodes.items()}
        queue = deque(tid for tid, d in in_degree.items() if d == 0)
        reached: set[str] = set()
        while queue:
            current = queue.popleft()
            reached.add(current)
            for dependent_id in nodes[current].dependents:
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    queue.append(dependent_id)
        cyclic_ids = tuple(sorted(set(nodes.keys()) - reached))
        logger.warning(
            "Cycle detected in plan — %d tasks implicated: %s",
            len(cyclic_ids),
            cyclic_ids,
        )
        return [
            ValidationIssue(
                severity=IssueSeverity.ERROR,
                category=IssueCategory.CYCLE,
                message=str(exc),
                affected_task_ids=cyclic_ids,
            )
        ]
    return []


def check_dependency_completeness(
    tasks: list[Task],
    initial_context: list[str] | None = None,
) -> list[ValidationIssue]:
    """Verify that every task input is produced by a predecessor or initial context.

    For each task, checks that every string listed in ``task.inputs`` is either
    present in ``initial_context`` or appears in the ``outputs`` of at least one
    task that the current task depends on (directly or via the resolved
    predecessor chain).

    Args:
        tasks: The decomposed task list to check.
        initial_context: Available inputs before any task executes (e.g., ["goal"]).
            Defaults to ["goal"] when None.

    Returns:
        List of DEPENDENCY/ERROR issues, one per unsatisfied input token.
    """
    if initial_context is None:
        initial_context = ["goal"]

    available: set[str] = set(initial_context)
    id_to_task: dict[str, Task] = {t.id: t for t in tasks}
    issues: list[ValidationIssue] = []

    # Process tasks in dependency order so predecessor outputs accumulate correctly
    # Use a simple topological pass — cycle check runs first so order is safe here
    processed: set[str] = set()
    ordered: list[Task] = []
    remaining = list(tasks)
    max_passes = len(tasks) + 1
    passes = 0
    while remaining and passes < max_passes:
        passes += 1
        next_remaining: list[Task] = []
        for task in remaining:
            if all(dep in processed for dep in task.dependencies):
                ordered.append(task)
                processed.add(task.id)
            else:
                next_remaining.append(task)
        remaining = next_remaining
    # Append any still-remaining tasks (cycle survivors) in original order
    ordered.extend(remaining)

    task_available: dict[str, set[str]] = {}  # task_id -> outputs visible to it

    for task in ordered:
        # Collect outputs available from all predecessors
        visible: set[str] = set(available)
        for dep_id in task.dependencies:
            dep_task = id_to_task.get(dep_id)
            if dep_task:
                visible.update(dep_task.outputs)
                # Also bring in what was visible to that predecessor
                visible.update(task_available.get(dep_id, set()))

        task_available[task.id] = visible

        for inp in task.inputs:
            if inp not in visible:
                logger.warning(
                    "Task %r input %r not satisfied by any predecessor or initial context",
                    task.id,
                    inp,
                )
                issues.append(
                    ValidationIssue(
                        severity=IssueSeverity.ERROR,
                        category=IssueCategory.DEPENDENCY,
                        message=(
                            f"Task '{task.id}' requires input '{inp}' "
                            f"but no predecessor produces it and it is not in initial context"
                        ),
                        affected_task_ids=(task.id,),
                    )
                )

    return issues


def check_goal_coverage(
    tasks: list[Task],
    goal: str,
    min_coverage: float = _DEFAULT_MIN_COVERAGE,
) -> list[ValidationIssue]:
    """Check keyword/token overlap between the goal and combined task descriptions.

    Uses simple word overlap ratio — NOT embedding similarity — so this check
    is available even when no LLM model is loaded. The ratio is computed as:

        |goal_tokens ∩ task_tokens| / |goal_tokens|

    Args:
        tasks: The decomposed task list.
        goal: The original user goal string.
        min_coverage: Minimum overlap ratio (0.0-1.0) below which a warning is issued.
            Defaults to 0.3 (30% of goal words must appear in task descriptions).

    Returns:
        List containing a single COVERAGE/WARNING issue when coverage is below
        the threshold, or an empty list when coverage is adequate.

    Raises:
        ValueError: If the goal string is present but invalid after validation.
    """
    if not tasks or not goal.strip():
        return []

    coverage_goal = require_nonempty(goal, field_name="goal_coverage")
    goal_tokens = _tokenize(coverage_goal)
    if not goal_tokens:
        return []

    combined_description = " ".join(t.description for t in tasks)
    task_tokens = _tokenize(combined_description)

    overlap = goal_tokens & task_tokens
    ratio = len(overlap) / len(goal_tokens)

    logger.debug(
        "Goal coverage: %d/%d tokens matched (%.1f%%)",
        len(overlap),
        len(goal_tokens),
        ratio * 100,
    )

    if ratio < min_coverage:
        missing = sorted(goal_tokens - task_tokens)
        return [
            ValidationIssue(
                severity=IssueSeverity.WARNING,
                category=IssueCategory.COVERAGE,
                message=(
                    f"Plan covers only {ratio:.0%} of goal tokens "
                    f"(minimum {min_coverage:.0%}); goal words not addressed: {', '.join(missing[:10])}"
                ),
                affected_task_ids=tuple(t.id for t in tasks),
            )
        ]
    return []


def check_testable_output(tasks: list[Task]) -> list[ValidationIssue]:
    """Verify that at least one task produces a testable or verifiable output.

    Scans every task's ``outputs`` list for entries containing words from the
    ``_TESTABLE_KEYWORDS`` set (e.g., "test", "verification", "report", "result").

    Args:
        tasks: The decomposed task list to check.

    Returns:
        List containing a single TESTABILITY/WARNING issue when no testable
        output is found, or an empty list when at least one task qualifies.
    """
    if not tasks:
        return []

    for task in tasks:
        for output in task.outputs:
            tokens = _tokenize(output)
            if tokens & _TESTABLE_KEYWORDS:
                return []

    # Also scan descriptions as a secondary signal
    for task in tasks:
        tokens = _tokenize(task.description)
        if tokens & _TESTABLE_KEYWORDS:
            return []

    return [
        ValidationIssue(
            severity=IssueSeverity.WARNING,
            category=IssueCategory.TESTABILITY,
            message=(
                "No task produces a testable or verifiable output. "
                "Add a task whose outputs include words like 'test', 'verification', 'report', or 'result'."
            ),
            affected_task_ids=(),
        )
    ]


def validate_plan(
    tasks: list[Task],
    goal: str,
    initial_context: list[str] | None = None,
) -> ValidationResult:
    """Run all structural and semantic validation checks on a decomposed plan.

    Checks (in order):
      1. Cycle detection — uses Kahn's algorithm (same as graph_planner)
      2. Dependency completeness — every input must be produced by a predecessor
      3. Goal coverage — keyword overlap between goal and task descriptions
      4. Testability — at least one task must produce a verifiable output

    Args:
        tasks: The decomposed task list to validate.
        goal: The original user goal string used to assess semantic coverage.
        initial_context: Names of inputs available before any task runs.
            Defaults to ["goal"].

    Returns:
        ValidationResult with ``valid=True`` only when there are no ERROR issues.
    """
    all_issues: list[ValidationIssue] = []
    if not tasks:
        all_issues.append(
            ValidationIssue(
                severity=IssueSeverity.ERROR,
                category=IssueCategory.TESTABILITY,
                message="Plan validation received no tasks; refusing to treat an empty plan as valid",
                affected_task_ids=(),
            )
        )
    if not goal or not goal.strip():
        all_issues.append(
            ValidationIssue(
                severity=IssueSeverity.ERROR,
                category=IssueCategory.COVERAGE,
                message="Plan validation requires the original goal; refusing unknown-goal validation",
                affected_task_ids=(),
            )
        )

    all_issues.extend(check_cycles(tasks))
    all_issues.extend(check_dependency_completeness(tasks, initial_context))
    all_issues.extend(check_goal_coverage(tasks, goal))
    all_issues.extend(check_testable_output(tasks))

    has_errors = any(i.severity == IssueSeverity.ERROR for i in all_issues)
    result = ValidationResult(
        valid=not has_errors,
        issues=tuple(all_issues),
    )

    if has_errors:
        logger.warning(
            "Plan validation failed: %d error(s), %d warning(s)",
            sum(1 for i in all_issues if i.severity == IssueSeverity.ERROR),
            sum(1 for i in all_issues if i.severity == IssueSeverity.WARNING),
        )
    else:
        logger.info(
            "Plan validation passed with %d warning(s)",
            sum(1 for i in all_issues if i.severity == IssueSeverity.WARNING),
        )

    return result


def flag_degraded_fallback(tasks: list[Task], method: str, goal: str | None = None) -> ValidationResult:
    """Mark a keyword-fallback plan as being in a degraded state with telemetry.

    When ``decompose_goal_keyword()`` is used instead of ``decompose_goal_llm()``,
    the resulting plan is structurally valid but semantically shallow. This
    function runs full validation AND explicitly marks ``is_degraded=True`` so
    downstream consumers can decide whether to surface a warning or block execution.

    Degraded-state flag is a MANDATORY anti-pattern guard — see anti-patterns.md
    "Fallback as success": keyword-fallback outputs must NEVER be recorded as
    training data or treated as high-quality plans.

    Args:
        tasks: Tasks produced by the keyword fallback decomposition.
        method: Name of the fallback method used (e.g., "decompose_goal_keyword").
        goal: Optional original goal. When omitted, a conservative synthetic
            goal is built from task descriptions so structural validation does
            not fail solely because fallback telemetry lacks goal context.

    Returns:
        ValidationResult with ``is_degraded=True`` and a descriptive ``degraded_reason``.
    """
    validation_goal = (goal or " ".join(t.description for t in tasks) or "keyword fallback plan").strip()
    base_result = validate_plan(tasks, goal=validation_goal)
    degraded_reason = (
        f"Plan produced by keyword fallback '{method}' — LLM decomposition was unavailable. "
        "This plan is structurally minimal and may not accurately reflect the user's intent. "
        "Do NOT record this plan as training data."
    )
    logger.warning(
        "Plan flagged as DEGRADED (method=%s) — keyword fallback in use",
        method,
    )
    return ValidationResult(
        valid=base_result.valid,
        issues=base_result.issues,
        is_degraded=True,
        degraded_reason=degraded_reason,
    )


def validate_and_retry(
    agent: Any,
    goal: str,
    context: dict[str, Any],
    max_retries: int = 2,
) -> tuple[list[Task], ValidationResult]:
    """Decompose a goal via LLM, validate, and re-prompt with issues on failure.

    Calls ``decompose_goal_llm`` with the provided agent, validates the result,
    and — if validation fails — injects the formatted issue list into a retry
    prompt (up to ``max_retries`` times).

    Args:
        agent: ForemanAgent instance with an ``_infer_json`` method.
        goal: The user goal string to decompose.
        context: Context dict passed to ``decompose_goal_llm``.
        max_retries: Maximum number of re-prompt attempts after the initial call.
            Defaults to 2.

    Returns:
        Tuple of (tasks, validation_result). If all retries are exhausted the
        final (possibly invalid) task list is returned along with its result so
        the caller can decide how to proceed.
    """
    from vetinari.agents.planner_decompose import decompose_goal_llm

    attempt = 0
    tasks: list[Task] = []
    result: ValidationResult = ValidationResult(valid=False, issues=())

    while attempt <= max_retries:
        if attempt == 0:
            prompt_context = context
        else:
            # Inject validation issues into the context so the LLM can fix them
            issue_hint = result.format_for_prompt()
            prompt_context = dict(context)
            existing_hint = prompt_context.get("_validation_hint", "")
            prompt_context["_validation_hint"] = (
                f"{existing_hint}\n\n{issue_hint}".strip() if existing_hint else issue_hint
            )
            logger.info(
                "Re-prompting LLM for plan (attempt %d/%d) with %d issue(s)",
                attempt,
                max_retries,
                len(result.issues),
            )

        tasks = decompose_goal_llm(agent, goal, prompt_context)
        result = validate_plan(tasks, goal)

        if result.valid:
            logger.info(
                "Plan validation passed on attempt %d",
                attempt + 1,
            )
            return tasks, result

        attempt += 1

    logger.warning(
        "Plan validation still failing after %d attempts — returning best-effort plan with %d issue(s)",
        max_retries + 1,
        len(result.error_issues()),
    )
    return tasks, result
