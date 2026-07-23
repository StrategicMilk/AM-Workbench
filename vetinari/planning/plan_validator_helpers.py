"""Models and helpers for plan validation."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from vetinari.agents.contracts import Task
from vetinari.exceptions import CircularDependencyError
from vetinari.orchestration.graph_types import TaskNode
from vetinari.types import StatusEnum

# -- Constants ----------------------------------------------------------------

# Words that signal a task output is testable/verifiable.
_TESTABLE_KEYWORDS: frozenset[str] = frozenset([
    "tests",
    "testing",
    "verification",
    "verify",
    "verified",
    "report",
    "result",
    "results",
    "validation",
    "validate",
    "validated",
    "checks",
    "audit",
    "spec",
    "coverage",
])

# Minimum word-overlap ratio between goal tokens and combined task tokens
# before the plan is considered to have adequate coverage.
_DEFAULT_MIN_COVERAGE: float = 0.3


# -- Enums --------------------------------------------------------------------


class IssueSeverity(Enum):
    """Severity of a validation issue."""

    ERROR = "ERROR"  # Plan cannot be executed safely
    WARNING = "WARNING"  # Plan may execute but results may be poor


class IssueCategory(Enum):
    """Category of a validation issue."""

    CYCLE = "CYCLE"  # Circular dependency detected
    DEPENDENCY = "DEPENDENCY"  # Missing or unresolvable dependency
    COVERAGE = "COVERAGE"  # Inadequate goal coverage
    TESTABILITY = "TESTABILITY"  # No testable output produced


# -- Data classes -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """A single issue discovered during plan validation.

    Attributes:
        severity: Whether this issue blocks execution (ERROR) or degrades it (WARNING).
        category: Which validation check produced this issue.
        message: Human-readable description of the problem.
        affected_task_ids: Task IDs implicated in this issue (may be empty for plan-level issues).
    """

    severity: IssueSeverity
    category: IssueCategory
    message: str
    affected_task_ids: tuple[str, ...] = field(default_factory=tuple)

    def __repr__(self) -> str:
        return f"ValidationIssue({self.severity.value}/{self.category.value}: {self.message!r})"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Aggregated result from running all validation checks on a plan.

    Attributes:
        valid: True only when there are no ERROR-severity issues.
        issues: All issues found (ERRORs and WARNINGs).
        is_degraded: True when the plan was produced by keyword fallback rather than LLM.
        degraded_reason: Human-readable explanation of why the plan is in degraded state.
    """

    valid: bool
    issues: tuple[ValidationIssue, ...]
    is_degraded: bool = False
    degraded_reason: str | None = None

    def __repr__(self) -> str:
        return f"ValidationResult(valid={self.valid!r}, issues={len(self.issues)}, is_degraded={self.is_degraded!r})"

    def error_issues(self) -> list[ValidationIssue]:
        """Return only ERROR-severity issues.

        Returns:
            Filtered list of issues with severity ERROR.
        """
        return [i for i in self.issues if i.severity == IssueSeverity.ERROR]

    def format_for_prompt(self) -> str:
        """Format validation issues as a concise re-prompt hint for the LLM.

        Returns:
            Multi-line string describing each issue, suitable for injection
            into a follow-up decomposition prompt.
        """
        lines = ["The previous plan had the following structural issues that must be fixed:"]
        for issue in self.issues:
            prefix = f"[{issue.severity.value}/{issue.category.value}]"
            if issue.affected_task_ids:
                lines.append(f"  {prefix} {issue.message} (tasks: {', '.join(issue.affected_task_ids)})")
            else:
                lines.append(f"  {prefix} {issue.message}")
        return "\n".join(lines)


# -- Internal helpers ---------------------------------------------------------


def _build_nodes(tasks: list[Task]) -> dict[str, TaskNode]:
    """Convert a list of Task objects into a TaskNode dict for graph operations.

    Populates both ``dependencies`` and ``dependents`` sets so the Kahn's
    algorithm in ``GraphPlanningEngine._topological_sort`` can operate correctly.

    Args:
        tasks: The tasks to convert.

    Returns:
        Dict mapping task ID to TaskNode with forward and reverse edges set.
    """
    nodes: dict[str, TaskNode] = {}
    for task in tasks:
        nodes[task.id] = TaskNode(
            task=task,
            dependencies=set(task.dependencies),
            status=StatusEnum.PENDING,
        )

    # Build reverse edges (dependents) — required by Kahn's algorithm
    for task_id, node in nodes.items():
        for dep_id in node.dependencies:
            if dep_id in nodes:
                nodes[dep_id].dependents.add(task_id)

    return nodes


def _kahn_cycle_check(nodes: dict[str, TaskNode]) -> None:
    """Run Kahn's algorithm for cycle detection, matching graph_planner._topological_sort.

    This replicates the same algorithm as ``GraphPlanningEngine._topological_sort``
    (vetinari/orchestration/graph_planner.py:108) so that cycle detection in the
    validator is consistent with cycle detection during execution-plan creation.
    Rather than instantiating the mixin (which carries heavy dependencies), we
    reproduce the 12-line algorithm here. The authoritative implementation lives
    in GraphPlanningEngine — if that changes, this must change in lockstep.

    Args:
        nodes: Task node dict with ``dependencies`` and ``dependents`` sets populated.

    Raises:
        CircularDependencyError: When a cycle is detected (same exception as the mixin).
    """
    in_degree = {tid: len(n.dependencies) for tid, n in nodes.items()}
    queue = deque(tid for tid, d in in_degree.items() if d == 0)
    result: list[str] = []

    while queue:
        current = queue.popleft()
        result.append(current)
        for dependent_id in nodes[current].dependents:
            in_degree[dependent_id] -= 1
            if in_degree[dependent_id] == 0:
                queue.append(dependent_id)

    if len(result) != len(nodes):
        raise CircularDependencyError("Circular dependency detected in task graph")


def _tokenize(text: str) -> set[str]:
    """Split text into lowercase alphabetic word tokens.

    Splits on any non-alpha character (spaces, underscores, hyphens, digits)
    so that compound names like ``test_results`` or ``verification-report``
    are correctly decomposed into their constituent words.

    Args:
        text: The string to tokenize.

    Returns:
        Set of lowercase alphabetic tokens with length >= 2.
    """
    return {w.lower() for w in re.split(r"[^a-zA-Z]+", text) if len(w) >= 2}
