"""Vetinari LLM Evaluation Framework.

This module provides a pure-Python scoring protocol for evaluating LLM outputs
from Vetinari agents. It is intentionally free of external eval libraries so that
evaluation tests can run in any environment without additional dependencies.

Responsibilities:
    - Define the canonical ``EvalResult`` data contract for evaluation scoring.
    - Provide ``evaluate_plan_quality`` to score structured plan outputs.
    - Provide ``evaluate_code_quality`` to score generated Python code.
    - Provide ``combine_results`` to aggregate multiple ``EvalResult`` instances.
"""

from __future__ import annotations

from vetinari.evaluation.prompt_wiring import (
    cached_prompt_lookup,
    get_prompt_cache_stats,
    invalidate_prompt_cache,
    optimize_prompt_for_budget,
    wire_prompt_optimization,
)

__all__ = [
    "MAX_PLAN_TASKS",
    "MIN_PLAN_TASKS",
    "PASS_THRESHOLD",
    "EvalResult",
    "cached_prompt_lookup",
    "combine_results",
    "evaluate_code_quality",
    "evaluate_plan_quality",
    "get_prompt_cache_stats",
    "invalidate_prompt_cache",
    "optimize_prompt_for_budget",
    "wire_prompt_optimization",
]

import ast
import logging
from dataclasses import dataclass, field
from typing import Any

from vetinari.exceptions import ExecutionError

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────────

MIN_PLAN_TASKS = 1  # Minimum sensible task count for a non-trivial plan
MAX_PLAN_TASKS = 50  # Upper bound before a plan is considered overly decomposed
PASS_THRESHOLD = 0.5  # Minimum score for an evaluation to be considered passing


# ── Data Contract ──────────────────────────────────────────────────────────────


@dataclass
class PlanEvalResult:
    """Result of a single evaluation check.

    Attributes:
        score: Normalised quality score in the range [0.0, 1.0].
        passed: Whether the evaluation meets the acceptance threshold.
        reasoning: Human-readable explanation of the score.
        metrics: Detailed per-dimension metrics collected during evaluation.
    """

    score: float
    passed: bool
    reasoning: str
    metrics: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"EvalResult(score={self.score!r}, passed={self.passed!r})"


EvalResult = PlanEvalResult


# ── Plan Evaluation ────────────────────────────────────────────────────────────


def evaluate_plan_quality(plan_dict: dict[str, Any]) -> EvalResult:
    """Score a structured plan output produced by a planner agent.

    The plan is evaluated across three dimensions:

    1. **Task count** (40 %): the plan must contain between
       ``MIN_PLAN_TASKS`` and ``MAX_PLAN_TASKS`` tasks.
    2. **Dependency validity** (30 %): task IDs referenced in dependency lists
       must correspond to actual tasks, and there must be no circular
       dependency chains.
    3. **Goal adherence** (30 %): a ``goal`` field must be present and
       non-empty.

    Args:
        plan_dict: Dictionary representation of a plan, expected keys:
            - ``"tasks"`` (list[dict]): each task dict should contain at
              least an ``"id"`` key.
            - ``"dependencies"`` (dict[str, list[str]]): mapping of task ID
              to a list of prerequisite task IDs.
            - ``"goal"`` (str): the high-level objective for the plan.

    Returns:
        An ``EvalResult`` with a composite score and detailed ``metrics``.
    """
    metrics: dict[str, Any] = {}
    penalties: list[str] = []
    score_components: list[float] = []

    if not isinstance(plan_dict, dict):
        return _plan_hard_fail(metrics, "plan must be a dict")

    tasks: list[dict[str, Any]] = plan_dict.get("tasks", [])
    dependencies: dict[str, list[str]] = plan_dict.get("dependencies", {})
    goal: str = plan_dict.get("goal", "")

    if not isinstance(tasks, list):
        return _plan_hard_fail(metrics, "tasks must be a list")
    if not all(isinstance(task, dict) for task in tasks):
        return _plan_hard_fail(metrics, "tasks must contain dict entries")
    missing_ids = [index for index, task in enumerate(tasks) if not str(task.get("id", "")).strip()]
    if missing_ids:
        metrics["missing_task_id_indexes"] = missing_ids
        return _plan_hard_fail(metrics, f"missing task id at indexes: {missing_ids}")
    if not isinstance(dependencies, dict):
        return _plan_hard_fail(metrics, "dependencies must be a dict")
    malformed_dependency_sources = [str(src) for src, prereqs in dependencies.items() if not isinstance(prereqs, list)]
    if malformed_dependency_sources:
        metrics["malformed_dependency_sources"] = malformed_dependency_sources
        return _plan_hard_fail(
            metrics,
            f"dependency values must be lists: {sorted(malformed_dependency_sources)}",
        )

    # ── Dimension 1: Task count ────────────────────────────────────────────
    task_count = len(tasks)
    metrics["task_count"] = task_count

    # Hard fail — a plan with no tasks has no actionable value regardless of
    # whether goal or dependency fields are present.
    if task_count == 0:
        metrics["task_score"] = 0.0
        metrics["dependency_score"] = 0.0
        metrics["goal_score"] = 0.0
        logger.debug("evaluate_plan_quality -> hard fail: no tasks")
        return EvalResult(
            score=0.0,
            passed=False,
            reasoning="Score 0.00. Plan has no tasks — evaluation cannot proceed.",
            metrics=metrics,
        )

    if MIN_PLAN_TASKS <= task_count <= MAX_PLAN_TASKS:
        task_score = 1.0
    else:
        # task_count > MAX_PLAN_TASKS (zero was handled above as a hard fail)
        overage = task_count - MAX_PLAN_TASKS
        task_score = max(0.0, 1.0 - (overage / MAX_PLAN_TASKS))
        penalties.append(f"plan has {task_count} tasks (>{MAX_PLAN_TASKS})")

    score_components.append(task_score * 0.40)
    metrics["task_score"] = task_score

    # ── Dimension 2: Dependency validity ──────────────────────────────────
    task_ids: set[str] = {str(t.get("id", "")) for t in tasks if t.get("id") is not None}
    metrics["task_ids"] = sorted(task_ids)

    dep_score, dep_penalties = _score_dependencies(task_ids, dependencies)
    penalties.extend(dep_penalties)
    score_components.append(dep_score * 0.30)
    metrics["dependency_score"] = dep_score
    metrics["dependency_issues"] = dep_penalties

    # ── Dimension 3: Goal adherence ────────────────────────────────────────
    if goal and goal.strip():
        goal_score = 1.0
    else:
        goal_score = 0.0
        penalties.append("plan is missing a non-empty goal")

    score_components.append(goal_score * 0.30)
    metrics["goal_score"] = goal_score

    # ── Aggregate ─────────────────────────────────────────────────────────
    composite = sum(score_components)
    passed = composite >= PASS_THRESHOLD and task_count >= MIN_PLAN_TASKS

    if penalties:
        reasoning = f"Score {composite:.2f}. Issues: {'; '.join(penalties)}."
    else:
        reasoning = f"Score {composite:.2f}. Plan structure is well-formed."

    logger.debug("evaluate_plan_quality -> score=%.2f passed=%s penalties=%s", composite, passed, penalties)
    return EvalResult(score=composite, passed=passed, reasoning=reasoning, metrics=metrics)


def _plan_hard_fail(metrics: dict[str, Any], reason: str) -> EvalResult:
    """Return a zero-score plan result for malformed or non-actionable plans."""
    metrics.setdefault("task_count", 0)
    metrics["task_score"] = 0.0
    metrics["dependency_score"] = 0.0
    metrics["goal_score"] = 0.0
    return EvalResult(
        score=0.0,
        passed=False,
        reasoning=f"Score 0.00. {reason}.",
        metrics=metrics,
    )


def _score_dependencies(task_ids: set[str], dependencies: dict[str, list[str]]) -> tuple[float, list[str]]:
    """Evaluate the validity of a dependency graph.

    Checks that all referenced task IDs exist and that there are no cycles.

    Args:
        task_ids: Set of all known task IDs in the plan.
        dependencies: Mapping from task ID to list of prerequisite task IDs.

    Returns:
        A tuple of (score in [0.0, 1.0], list of penalty reason strings).
    """
    penalties: list[str] = []

    if not dependencies:
        return 1.0, penalties

    # Check for unknown task references
    unknown: list[str] = []
    for src, prereqs in dependencies.items():
        if src not in task_ids and src:
            unknown.append(src)
        unknown.extend(dep for dep in prereqs if dep not in task_ids and dep)

    if unknown:
        penalties.append(f"unknown task IDs in dependencies: {sorted(set(unknown))}")

    # Check for cycles using DFS
    if _has_cycle(dependencies):
        penalties.append("circular dependency detected in task graph")

    if penalties:
        return 0.0, penalties
    return 1.0, penalties


def _has_cycle(dependencies: dict[str, list[str]]) -> bool:
    """Detect a cycle in a dependency graph using iterative DFS.

    Args:
        dependencies: Adjacency list representation of the dependency graph.

    Returns:
        ``True`` if a cycle is found, ``False`` otherwise.
    """
    visited: set[str] = set()
    in_stack: set[str] = set()

    def _dfs(node: str) -> bool:
        visited.add(node)
        in_stack.add(node)
        for neighbour in dependencies.get(node, []):
            if neighbour not in visited:
                if _dfs(neighbour):
                    return True
            elif neighbour in in_stack:
                return True
        in_stack.discard(node)
        return False

    return any(node not in visited and _dfs(node) for node in list(dependencies.keys()))


# ── Code Evaluation ────────────────────────────────────────────────────────────


def evaluate_code_quality(code: str | None) -> EvalResult:
    """Score a Python code string produced by a builder agent.

    The code is evaluated across three dimensions:

    1. **Syntax validity** (50 %): the code must parse without errors via
       ``ast.parse``.
    2. **Import correctness** (25 %): import statements are checked for
       obvious structural issues (``ImportError`` at parse time counts
       against this, though runtime resolution is not attempted).
    3. **Function completeness** (25 %): functions detected by the AST must
       not consist solely of a ``pass`` statement (stub bodies incur a
       penalty proportional to the fraction of stub functions).

    Args:
        code: Python source code string to evaluate.

    Returns:
        An ``EvalResult`` with a composite score and detailed ``metrics``.
    """
    metrics: dict[str, Any] = {}
    penalties: list[str] = []
    score_components: list[float] = []

    if not isinstance(code, str) or not code.strip():
        metrics["syntax_score"] = 0.0
        metrics["import_score"] = 0.0
        metrics["function_score"] = 0.0
        return EvalResult(
            score=0.0,
            passed=False,
            reasoning="Score 0.00. Code is empty.",
            metrics=metrics,
        )

    # ── Dimension 1: Syntax validity ──────────────────────────────────────
    tree: ast.Module | None = None
    try:
        tree = ast.parse(code)
        syntax_score = 1.0
    except SyntaxError as exc:
        syntax_score = 0.0
        penalties.append(f"syntax error: {exc.msg} (line {exc.lineno})")
        logger.warning("evaluate_code_quality: SyntaxError: %s", exc)

    if tree is not None and not tree.body:
        metrics["syntax_score"] = 0.0
        metrics["import_score"] = 0.0
        metrics["function_score"] = 0.0
        return EvalResult(
            score=0.0,
            passed=False,
            reasoning="Score 0.00. Code has no executable statements.",
            metrics=metrics,
        )

    score_components.append(syntax_score * 0.50)
    metrics["syntax_score"] = syntax_score

    # ── Dimension 2: Import correctness ───────────────────────────────────
    if tree is not None:
        import_score, import_penalties = _score_imports(tree)
        penalties.extend(import_penalties)
        metrics["import_issues"] = import_penalties
    else:
        import_score = 0.0
        metrics["import_issues"] = ["skipped — syntax error prevented AST parse"]

    score_components.append(import_score * 0.25)
    metrics["import_score"] = import_score

    # ── Dimension 3: Function completeness ────────────────────────────────
    if tree is not None:
        func_score, func_penalties = _score_function_completeness(tree)
        penalties.extend(func_penalties)
        metrics["function_issues"] = func_penalties
    else:
        func_score = 0.0
        metrics["function_issues"] = ["skipped — syntax error prevented AST parse"]

    score_components.append(func_score * 0.25)
    metrics["function_score"] = func_score

    # ── Aggregate ─────────────────────────────────────────────────────────
    composite = sum(score_components)
    passed = composite >= PASS_THRESHOLD and syntax_score > 0.0

    if penalties:
        reasoning = f"Score {composite:.2f}. Issues: {'; '.join(penalties)}."
    else:
        reasoning = f"Score {composite:.2f}. Code quality looks acceptable."

    logger.debug("evaluate_code_quality -> score=%.2f passed=%s penalties=%s", composite, passed, penalties)
    return EvalResult(score=composite, passed=passed, reasoning=reasoning, metrics=metrics)


def _score_imports(tree: ast.Module) -> tuple[float, list[str]]:
    """Evaluate import statements in a parsed AST.

    Checks for star imports (``from module import *``) which violate project
    conventions and may indicate incomplete code generation.

    Args:
        tree: Parsed AST module to inspect.

    Returns:
        A tuple of (score in [0.0, 1.0], list of penalty reason strings).
    """
    penalties: list[str] = []
    import_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom | ast.Import)]

    if not import_nodes:
        # No imports is neutral — not all code needs them
        return 1.0, penalties

    star_imports = [
        n for n in import_nodes if isinstance(n, ast.ImportFrom) and any(alias.name == "*" for alias in n.names)
    ]
    if star_imports:
        penalties.append(f"wildcard imports detected ({len(star_imports)} occurrences)")
        return 0.5, penalties

    return 1.0, penalties


def _score_function_completeness(tree: ast.Module) -> tuple[float, list[str]]:
    """Detect stub functions (bodies consisting only of ``pass``).

    A stub function body is defined as a function whose entire body is a
    single ``ast.Pass`` node (optionally preceded by a docstring).

    Args:
        tree: Parsed AST module to inspect.

    Returns:
        A tuple of (score in [0.0, 1.0], list of penalty reason strings).
    """
    penalties: list[str] = []
    func_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]

    if not func_nodes:
        return 1.0, penalties

    stub_names: list[str] = [func.name for func in func_nodes if _is_stub_function(func)]

    if stub_names and func_nodes:
        stub_ratio = len(stub_names) / len(func_nodes)
        # Score penalty is proportional to the fraction of stubs
        score = 1.0 - stub_ratio
        penalties.append(f"stub functions detected (pass-only body): {stub_names}")
        return score, penalties

    return 1.0, penalties


def _is_stub_function(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True if a function body consists only of pass (ignoring docstring).

    Args:
        func: An AST FunctionDef or AsyncFunctionDef node to inspect.

    Returns:
        ``True`` if the function body is a stub (pass-only after any docstring).
    """
    body = func.body
    # Skip leading docstring constant
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]

    return len(body) == 1 and isinstance(body[0], ast.Pass)


# ── Aggregation ────────────────────────────────────────────────────────────────


def combine_results(results: list[EvalResult]) -> EvalResult:
    """Aggregate multiple evaluation results into a single combined result.

    The combined score is the arithmetic mean of all individual scores.
    The combined result passes only if ALL individual results passed.

    Args:
        results: List of ``EvalResult`` instances to combine. Must be
            non-empty.

    Returns:
        A single ``EvalResult`` representing the aggregate evaluation.

    Raises:
        ValueError: If ``results`` is empty.
    """
    if not results:
        raise ExecutionError("combine_results requires at least one EvalResult")

    combined_score = sum(r.score for r in results) / len(results)
    all_passed = all(r.passed for r in results)

    failed_indices = [i for i, r in enumerate(results) if not r.passed]
    if failed_indices:
        reasoning = (
            f"Combined score {combined_score:.2f}. "
            f"{len(failed_indices)} of {len(results)} evaluations failed "
            f"(indices: {failed_indices})."
        )
    else:
        reasoning = f"Combined score {combined_score:.2f}. All {len(results)} evaluations passed."

    combined_metrics: dict[str, Any] = {
        "result_count": len(results),
        "individual_scores": [r.score for r in results],
        "individual_passed": [r.passed for r in results],
    }

    logger.debug("combine_results -> score=%.2f passed=%s count=%d", combined_score, all_passed, len(results))
    return EvalResult(
        score=combined_score,
        passed=all_passed,
        reasoning=reasoning,
        metrics=combined_metrics,
    )
