"""Intermediate step evaluation for agent plans (P10.10).

Provides deterministic, LLM-free scoring of:
- Plan quality  (logical structure, completeness)
- Plan adherence (did execution follow the plan?)

All scoring is algorithmic — no external calls are made.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ── Result dataclasses ───────────────────────────────────────────────────────


@dataclass
class StepScore:
    """Score produced by a single evaluation metric.

    Attributes:
        metric_name: Human-readable metric identifier.
        score: Points awarded (0 ≤ score ≤ max_score).
        max_score: Maximum possible points for this metric.
        details: Diagnostic key/value pairs explaining the score.
        passed: True when score / max_score ≥ the metric's pass threshold.
    """

    metric_name: str
    score: float
    max_score: float
    details: dict[str, Any]
    passed: bool

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"StepScore(metric_name={self.metric_name!r}, score={self.score!r}, passed={self.passed!r})"

    @property
    def ratio(self) -> float:
        """Score as a fraction of max_score (0.0 - 1.0)."""
        if self.max_score == 0:
            return 1.0
        return self.score / self.max_score


@dataclass
class EvaluationReport:
    """Aggregate result from :meth:`StepEvaluator.evaluate_all`.

    Attributes:
        scores: One :class:`StepScore` per metric evaluated.
        overall_score: Weighted average of score ratios (0.0 - 1.0).
        passed: True when all metrics passed.
        recommendations: Human-readable suggestions for failing metrics.
    """

    scores: list[StepScore]
    overall_score: float
    passed: bool
    recommendations: list[str]

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"EvaluationReport(overall_score={self.overall_score!r},"
            f" passed={self.passed!r}, scores={len(self.scores)!r})"
        )


# ── PlanQualityMetric ────────────────────────────────────────────────────────


class PlanQualityMetric:
    """Evaluates whether a plan dict is logically sound and complete.

    Checks performed:
    - ``has_tasks`` — plan contains at least one task.
    - ``has_dependencies`` — tasks carry a dependency field (even if empty).
    - ``no_cycles`` — dependency graph is acyclic.
    - ``reasonable_task_count`` — task count is between 1 and 100.
    """

    NAME = "plan_quality"
    _MAX_TASKS = 100
    _PASS_THRESHOLD = 0.75

    def evaluate_plan(self, plan: dict[str, Any] | None) -> StepScore:
        """Score plan quality.

        Args:
            plan: Plan dictionary.  Expected keys: ``tasks`` (list of dicts),
                each task optionally carrying ``id`` and ``depends_on`` fields.

        Returns:
            A :class:`StepScore` with up to 4 points (one per check).
        """
        raw_tasks = plan.get("tasks") if plan else None
        tasks: list[dict[str, Any]] = raw_tasks or []
        details: dict[str, Any] = {
            "has_tasks": len(tasks) > 0,
            "has_dependencies": any("depends_on" in task for task in tasks),
            "no_cycles": self._is_acyclic(tasks),
            "task_count": len(tasks),
            "reasonable_task_count": 1 <= len(tasks) <= self._MAX_TASKS,
        }
        if not details["has_tasks"]:
            return StepScore(
                metric_name=self.NAME,
                score=0.0,
                max_score=4.0,
                details={**details, "guard": "empty_or_null_plan"},
                passed=False,
            )

        score = 0.0
        max_score = 4.0

        # Check 1: has_tasks
        has_tasks = details["has_tasks"]
        details["has_tasks"] = has_tasks
        if has_tasks:
            score += 1.0

        # Check 2: has_dependencies field present on at least one task
        has_dep_field = details["has_dependencies"]
        details["has_dependencies"] = has_dep_field
        if has_dep_field and tasks:
            score += 1.0

        # Check 3: no_cycles
        no_cycles = details["no_cycles"]
        details["no_cycles"] = no_cycles
        if no_cycles:
            score += 1.0

        # Check 4: reasonable_task_count
        reasonable = details["reasonable_task_count"]
        details["task_count"] = len(tasks)
        details["reasonable_task_count"] = reasonable
        if reasonable:
            score += 1.0

        passed = (score / max_score) >= self._PASS_THRESHOLD
        return StepScore(
            metric_name=self.NAME,
            score=score,
            max_score=max_score,
            details=details,
            passed=passed,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_acyclic(tasks: list[dict[str, Any]]) -> bool:
        """Return True if the dependency graph has no cycles.

        Uses iterative depth-first search (Kahn's algorithm variant).
        Tasks without an ``id`` field get a synthetic unique id.

        Args:
            tasks: List of task dicts, each optionally with ``id`` and
                ``depends_on`` (list of task id strings).

        Returns:
            True when the graph is a DAG (no cycles).
        """
        if not tasks:
            return True

        # Build adjacency list
        ids = [str(t.get("id", i)) for i, t in enumerate(tasks)]
        id_set = set(ids)
        adj: dict[str, list[str]] = {tid: [] for tid in ids}
        in_degree: dict[str, int] = dict.fromkeys(ids, 0)

        for i, task in enumerate(tasks):
            tid = ids[i]
            deps = task.get("depends_on") or []
            if isinstance(deps, str):
                deps = [deps]
            for dep in deps:
                dep_str = str(dep)
                if dep_str in id_set:
                    adj[dep_str].append(tid)
                    in_degree[tid] += 1

        # Kahn's algorithm
        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        visited = 0
        while queue:
            node = queue.pop()
            visited += 1
            for neighbour in adj[node]:
                in_degree[neighbour] -= 1
                if in_degree[neighbour] == 0:
                    queue.append(neighbour)

        return visited == len(ids)


# ── PlanAdherenceMetric ──────────────────────────────────────────────────────


class PlanAdherenceMetric:
    """Evaluates whether execution followed the declared plan.

    Checks performed:
    - ``tasks_completed_ratio`` — fraction of planned tasks that were executed.
    - ``order_followed`` — tasks were executed in a valid topological order.
    - ``no_skipped_tasks`` — every planned task appeared in the execution log.
    """

    NAME = "plan_adherence"
    _PASS_THRESHOLD = 0.67

    def evaluate_adherence(
        self,
        plan: dict[str, Any],
        execution_log: list[dict[str, Any]],
    ) -> StepScore:
        """Score how closely execution matched the plan.

        Args:
            plan: Plan dict with a ``tasks`` list (same schema as
                :meth:`PlanQualityMetric.evaluate_plan`).
            execution_log: Ordered list of execution event dicts, each with
                at minimum a ``task_id`` key.

        Returns:
            A :class:`StepScore` with up to 3 points (one per check).
        """
        tasks: list[dict[str, Any]] = plan.get("tasks", []) or []
        details: dict[str, Any] = {}
        score = 0.0
        max_score = 3.0

        planned_ids = [str(t.get("id", i)) for i, t in enumerate(tasks)]
        executed_ids = [str(e.get("task_id", "")) for e in execution_log if e.get("task_id")]

        # Check 1: tasks_completed_ratio
        if planned_ids:
            completed = sum(1 for pid in planned_ids if pid in executed_ids)
            ratio = completed / len(planned_ids)
        else:
            ratio = 1.0
            completed = 0
        details["tasks_completed_ratio"] = round(ratio, 4)
        details["planned_count"] = len(planned_ids)
        details["executed_count"] = completed
        if ratio >= 1.0:
            score += 1.0
        elif ratio >= 0.5:
            score += 0.5

        # Check 2: order_followed — executed tasks respect plan order
        order_ok = self._order_respected(planned_ids, executed_ids)
        details["order_followed"] = order_ok
        if order_ok:
            score += 1.0

        # Check 3: no_skipped_tasks
        skipped = [pid for pid in planned_ids if pid not in executed_ids]
        details["skipped_tasks"] = skipped
        details["no_skipped_tasks"] = len(skipped) == 0
        if not skipped:
            score += 1.0

        passed = (score / max_score) >= self._PASS_THRESHOLD
        return StepScore(
            metric_name=self.NAME,
            score=score,
            max_score=max_score,
            details=details,
            passed=passed,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _order_respected(planned: list[str], executed: list[str]) -> bool:
        """Return True if executed tasks appear in the same relative order as planned.

        Args:
            planned: Ordered list of planned task ids.
            executed: Ordered list of executed task ids.

        Returns:
            True when the relative order of common tasks is preserved.
        """
        common_planned = [tid for tid in planned if tid in set(executed)]
        common_executed = [tid for tid in executed if tid in set(planned)]
        return common_planned == common_executed


# ── StepEvaluator ────────────────────────────────────────────────────────────


class StepEvaluator:
    """Orchestrates plan quality and adherence metrics.

    Instantiate via :func:`get_step_evaluator`.

    Example::

        ev = get_step_evaluator()
        report = ev.evaluate_all(plan, execution_log)
        logger.debug(report.overall_score, report.passed)
    """

    def __init__(self) -> None:
        self._quality = PlanQualityMetric()
        self._adherence = PlanAdherenceMetric()

    def evaluate_all(
        self,
        plan: dict[str, Any],
        execution_log: list[dict[str, Any]],
    ) -> EvaluationReport:
        """Run all metrics and return a combined report.

        Args:
            plan: Plan dictionary (see :meth:`PlanQualityMetric.evaluate_plan`).
            execution_log: Ordered execution event list (see
                :meth:`PlanAdherenceMetric.evaluate_adherence`).

        Returns:
            An :class:`EvaluationReport` summarising all metric results.
        """
        quality_score = self._quality.evaluate_plan(plan)
        adherence_score = self._adherence.evaluate_adherence(plan, execution_log)

        scores = [quality_score, adherence_score]
        ratios = [s.ratio for s in scores]
        overall = sum(ratios) / len(ratios) if ratios else 0.0
        passed = all(s.passed for s in scores)

        recommendations: list[str] = []
        if not quality_score.passed:
            recommendations.extend(self._quality_recommendations(quality_score))
        if not adherence_score.passed:
            recommendations.extend(self._adherence_recommendations(adherence_score))

        return EvaluationReport(
            scores=scores,
            overall_score=round(overall, 4),
            passed=passed,
            recommendations=recommendations,
        )

    # ------------------------------------------------------------------
    # Recommendation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _quality_recommendations(score: StepScore) -> list[str]:
        recs: list[str] = []
        d = score.details
        if not d.get("has_tasks"):
            recs.append("Plan has no tasks — add at least one task.")
        if not d.get("has_dependencies"):
            recs.append("Tasks lack dependency declarations — add 'depends_on' fields.")
        if not d.get("no_cycles"):
            recs.append("Dependency graph contains cycles — remove circular references.")
        if not d.get("reasonable_task_count"):
            count = d.get("task_count", 0)
            recs.append(f"Task count ({count}) is outside the acceptable range (1-100).")
        return recs

    @staticmethod
    def _adherence_recommendations(score: StepScore) -> list[str]:
        recs: list[str] = []
        d = score.details
        ratio = d.get("tasks_completed_ratio", 0.0)
        if ratio < 1.0:
            recs.append(f"Only {ratio:.0%} of planned tasks were executed — check for early termination.")
        if not d.get("order_followed"):
            recs.append("Execution order did not match planned order.")
        skipped = d.get("skipped_tasks", [])
        if skipped:
            recs.append(f"Skipped tasks: {', '.join(str(t) for t in skipped[:5])}.")
        return recs


# ── Singleton accessor ───────────────────────────────────────────────────────

_evaluator_instance: StepEvaluator | None = None
_evaluator_lock = threading.Lock()


def get_step_evaluator() -> StepEvaluator:
    """Return the process-global :class:`StepEvaluator` instance.

    Returns:
        The singleton :class:`StepEvaluator`.
    """
    global _evaluator_instance
    if _evaluator_instance is None:
        with _evaluator_lock:
            if _evaluator_instance is None:
                _evaluator_instance = StepEvaluator()
    return _evaluator_instance


def reset_step_evaluator() -> None:
    """Reset the singleton (intended for testing)."""
    global _evaluator_instance
    with _evaluator_lock:
        _evaluator_instance = None
