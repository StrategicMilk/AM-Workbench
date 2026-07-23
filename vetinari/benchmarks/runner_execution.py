"""Execution helpers for the multi-layer benchmark runner."""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

from vetinari.benchmarks.benchmark_types import (
    BenchmarkCase,
    BenchmarkReport,
    BenchmarkResult,
    BenchmarkSuiteAdapter,
)

logger = logging.getLogger(__name__)


def _run_benchmark_trials(
    adapter: BenchmarkSuiteAdapter,
    *,
    suite_name: str,
    run_id: str,
    cases: list[BenchmarkCase],
    trials: int,
) -> tuple[list[BenchmarkResult], dict[str, int]]:
    """Run all benchmark case trials and return raw results plus pass counts."""
    all_results: list[BenchmarkResult] = []
    pass_counts: dict[str, int] = {}
    for case in cases:
        pass_counts[case.case_id] = 0
        for _trial in range(trials):
            try:
                result = adapter.run_case(case, run_id)
                result.score = adapter.evaluate(result)
                result.passed = result.score >= 0.5
            except Exception as exc:
                result = BenchmarkResult(
                    case_id=case.case_id,
                    suite_name=suite_name,
                    run_id=run_id,
                    passed=False,
                    score=0.0,
                    error=str(exc),
                )
            if result.passed:
                pass_counts[case.case_id] += 1
            all_results.append(result)
    return all_results, pass_counts


def _compute_pass_k(cases: list[BenchmarkCase], pass_counts: dict[str, int], trials: int) -> float:
    """Compute the pass^k score from per-case trial pass counts."""
    if trials <= 0 or not cases:
        return 0.0
    fully_passed = sum(1 for case in cases if pass_counts[case.case_id] == trials)
    return fully_passed / len(cases)


def _build_suite_report(
    adapter: BenchmarkSuiteAdapter,
    *,
    suite_name: str,
    run_id: str,
    cases: list[BenchmarkCase],
    trials: int,
) -> BenchmarkReport:
    """Run suite cases and aggregate them into a benchmark report."""
    started = datetime.now(timezone.utc).isoformat()
    all_results, pass_counts = _run_benchmark_trials(
        adapter,
        suite_name=suite_name,
        run_id=run_id,
        cases=cases,
        trials=trials,
    )
    finished = datetime.now(timezone.utc).isoformat()
    report = BenchmarkReport(
        run_id=run_id,
        suite_name=suite_name,
        layer=adapter.layer,
        tier=adapter.tier,
        results=all_results,
        started_at=started,
        finished_at=finished,
    )
    report.compute_aggregates()
    report.pass_k = _compute_pass_k(cases, pass_counts, trials)
    return report


def _notify_workflow_learner(suite_name: str, report: BenchmarkReport) -> None:
    """Send benchmark outcomes to the workflow learner feedback path."""
    try:
        from vetinari.learning.workflow_learner import get_workflow_learner

        get_workflow_learner().learn_from_benchmark({
            "suite_name": suite_name,
            "task_type": suite_name,
            "pass_rate": report.pass_at_1,
            "avg_score": report.avg_score,
            "total_cases": report.total_cases,
            "passed_cases": report.passed_cases,
            "results": [{"passed": result.passed, "score": result.score} for result in report.results],
            "metadata": report.metadata,
        })
    except Exception as exc:
        logger.warning(
            "Workflow learner update failed after suite %s — benchmark results will not improve decomposition: %s",
            suite_name,
            exc,
        )


def _notify_model_feedback_loop(suite_name: str, report: BenchmarkReport) -> None:
    """Send benchmark outcomes to the model-routing feedback path."""
    try:
        from vetinari.learning.feedback_loop import get_feedback_loop

        model_id = _benchmarked_model_id(report)
        if not model_id:
            logger.warning(
                "Skipping model feedback for suite %s because no benchmarked model identity was recorded",
                suite_name,
            )
            return
        get_feedback_loop().record_benchmark_outcome(
            model_id=model_id,
            benchmark_result={
                "suite_name": suite_name,
                "model_id": model_id,
                "pass_rate": report.pass_at_1,
                "avg_score": report.avg_score,
                "total_cases": report.total_cases,
                "passed_cases": report.passed_cases,
            },
        )
    except Exception as exc:
        logger.warning(
            "Feedback loop update failed after suite %s — benchmark scores will not influence model routing: %s",
            suite_name,
            exc,
        )


def _notify_benchmark_feedback(suite_name: str, report: BenchmarkReport) -> None:
    """Notify all learning feedback paths that consume benchmark reports."""
    _notify_workflow_learner(suite_name, report)
    _notify_model_feedback_loop(suite_name, report)


def _benchmarked_model_id(report: BenchmarkReport) -> str | None:
    """Resolve the model identity under test from report/result metadata."""
    metadata_model = report.metadata.get("model_id") or report.metadata.get("benchmarked_model_id")
    if metadata_model:
        return str(metadata_model)
    candidates = [
        result.metadata.get("model_id") or result.metadata.get("benchmarked_model_id")
        for result in report.results
        if result.metadata.get("model_id") or result.metadata.get("benchmarked_model_id")
    ]
    if not candidates:
        return None
    [(model_id, _count)] = Counter(str(candidate) for candidate in candidates).most_common(1)
    return model_id
