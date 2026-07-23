"""Multi-Layer Benchmark Runner for Vetinari.

Core framework for running benchmarks across three layers:

  Layer 1 (Agent):         Individual agent tool-calling   -- seconds
  Layer 2 (Orchestration): Multi-agent tool chains         -- minutes
  Layer 3 (Pipeline):      Full end-to-end pipelines       -- hours

Tracks per-run: pass@1, pass^k, tokens, latency, cost, error recovery rate.
Persists metrics to SQLite for historical comparison.

Usage::

    from vetinari.benchmarks.runner import BenchmarkRunner
    runner = BenchmarkRunner()
    report = runner.run_suite("toolbench")
    runner.compare_runs(report.run_id, previous_run_id)
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from vetinari.benchmarks.benchmark_types import (
    BenchmarkCase,
    BenchmarkLayer,
    BenchmarkReport,
    BenchmarkResult,
    BenchmarkSuiteAdapter,
    BenchmarkTier,
    ComparisonReport,
)
from vetinari.benchmarks.runner_execution import _build_suite_report, _notify_benchmark_feedback
from vetinari.exceptions import ExecutionError

logger = logging.getLogger(__name__)


__all__ = [
    "BenchmarkBootstrapError",
    "BenchmarkCase",
    "BenchmarkLayer",
    "BenchmarkReport",
    "BenchmarkResult",
    "BenchmarkRunner",
    "BenchmarkSuiteAdapter",
    "BenchmarkTier",
    "ComparisonReport",
    "MetricStore",
    "get_default_runner",
]


class BenchmarkBootstrapError(ExecutionError):
    """Raised when default benchmark adapters cannot be registered safely."""


def _get_metric_connection() -> Any:
    """Return the current unified DB connection for metric-store operations."""
    from vetinari.database import get_connection

    return get_connection()


# ============================================================
# SQLite metric store
# ============================================================


class MetricStore:
    """Persists benchmark results and reports to the unified SQLite database."""

    def save_report(self, report: BenchmarkReport) -> None:
        """Persist a full benchmark report.

        Args:
            report: The BenchmarkReport to save including all individual results.
        """
        conn = _get_metric_connection()
        with conn:
            conn.execute(
                """INSERT OR REPLACE INTO benchmark_runs
                   (run_id, suite_name, layer, tier, started_at, finished_at,
                    total_cases, passed_cases, pass_at_1, pass_k,
                    avg_score, avg_latency, total_tokens, total_cost,
                    error_recovery_rate, metadata)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    report.run_id,
                    report.suite_name,
                    report.layer.name,
                    report.tier.value,
                    report.started_at,
                    report.finished_at,
                    report.total_cases,
                    report.passed_cases,
                    report.pass_at_1,
                    report.pass_k,
                    report.avg_score,
                    report.avg_latency_ms,
                    report.total_tokens,
                    report.total_cost_usd,
                    report.error_recovery_rate,
                    json.dumps(report.metadata),
                ),
            )
            conn.execute("DELETE FROM benchmark_results WHERE run_id = ?", (report.run_id,))
            for r in report.results:
                conn.execute(
                    """INSERT INTO benchmark_results
                       (run_id, case_id, suite_name, passed, score,
                        latency_ms, tokens, cost_usd, error_recovery,
                        error, output, timestamp)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        report.run_id,
                        r.case_id,
                        report.suite_name,
                        int(r.passed),
                        r.score,
                        r.latency_ms,
                        r.tokens_consumed,
                        r.cost_usd,
                        r.error_recovery_count,
                        r.error,
                        json.dumps(r.output) if r.output else None,
                        r.timestamp,
                    ),
                )

    def load_report(self, run_id: str) -> dict[str, Any] | None:
        """Load a benchmark run summary by run_id.

        Args:
            run_id: The unique run identifier to look up.

        Returns:
            Row dict from ``benchmark_runs`` with all aggregate metrics, or None if
            the run_id does not exist in the store.
        """
        conn = _get_metric_connection()
        row = conn.execute("SELECT * FROM benchmark_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return dict(row)

    def load_results(self, run_id: str) -> list[dict[str, Any]]:
        """Load all individual case results for a benchmark run.

        Args:
            run_id: The unique run identifier to fetch results for.

        Returns:
            List of row dicts from ``benchmark_results``, one per case execution,
            ordered by insertion ID.
        """
        conn = _get_metric_connection()
        rows = conn.execute(
            "SELECT * FROM benchmark_results WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_runs(self, suite_name: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List recent benchmark runs sorted by start time descending.

        Args:
            suite_name: When provided, filters results to runs for this suite only.
            limit: Maximum number of runs to return.

        Returns:
            List of row dicts from ``benchmark_runs``, most recent first.
        """
        conn = _get_metric_connection()
        if suite_name:
            rows = conn.execute(
                """SELECT * FROM benchmark_runs
                   WHERE suite_name = ?
                   ORDER BY started_at DESC LIMIT ?""",
                (suite_name, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM benchmark_runs
                   ORDER BY started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_two_run_ids(self, suite_name: str) -> list[str]:
        """Return the two most recent run IDs for a named benchmark suite.

        Args:
            suite_name: The suite whose run history to query.

        Returns:
            List of up to two run ID strings, most recent first.
        """
        conn = _get_metric_connection()
        rows = conn.execute(
            """SELECT run_id FROM benchmark_runs
               WHERE suite_name = ?
               ORDER BY started_at DESC LIMIT 2""",
            (suite_name,),
        ).fetchall()
        return [r["run_id"] for r in rows]


# ============================================================
# BenchmarkRunner -- main entry point
# ============================================================


class BenchmarkRunner:
    """Run benchmarks against Vetinari's orchestration pipeline."""

    def __init__(self) -> None:
        self._store = MetricStore()
        self._suites: dict[str, BenchmarkSuiteAdapter] = {}

    def register_suite(self, adapter: BenchmarkSuiteAdapter) -> None:
        """Register a benchmark suite adapter.

        Args:
            adapter: The adapter instance to register.
        """
        self._suites[adapter.name] = adapter

    def list_suites(self) -> list[dict[str, str]]:
        """Return metadata for all registered suites.

        Returns:
            List of dicts with name, layer, tier, and description for each suite.
        """
        return [
            {
                "name": a.name,
                "layer": a.layer.name,
                "tier": a.tier.value,
                "description": a.description(),
            }
            for a in self._suites.values()
        ]

    def run_suite(
        self,
        suite_name: str,
        limit: int | None = None,
        trials: int = 1,
    ) -> BenchmarkReport:
        """Run a named benchmark suite and persist results to the metric store.

        Args:
            suite_name: Registered suite name to execute.
            limit: Maximum number of cases to run; None runs all cases.
            trials: Number of trials per case for pass^k consistency score.

        Returns:
            BenchmarkReport with aggregated metrics including pass@1, pass^k,
            avg_score, latency, token usage, cost, and error recovery rate.

        Raises:
            ExecutionError: If ``suite_name`` is not registered with this runner.
        """
        adapter = self._suites.get(suite_name)
        if adapter is None:
            raise ExecutionError(f"Unknown suite '{suite_name}'. Available: {list(self._suites.keys())}")

        run_id = f"{suite_name}-{uuid.uuid4().hex[:8]}"
        cases = adapter.load_cases(limit=limit)
        report = _build_suite_report(
            adapter,
            suite_name=suite_name,
            run_id=run_id,
            cases=cases,
            trials=trials,
        )
        self.track_metrics(report)
        _notify_benchmark_feedback(suite_name, report)
        return report

    def run_single(self, benchmark_id: str) -> BenchmarkResult:
        """Run a single benchmark case by its composite ID.

        Args:
            benchmark_id: Composite identifier in ``suite_name:case_id`` format.

        Returns:
            BenchmarkResult with score and pass/fail status.

        Raises:
            ExecutionError: If format is wrong, suite is not registered, or case not found.
        """
        if ":" not in benchmark_id:
            raise ExecutionError("benchmark_id must be 'suite_name:case_id' format")
        suite_name, case_id = benchmark_id.split(":", 1)
        adapter = self._suites.get(suite_name)
        if adapter is None:
            raise ExecutionError(f"Unknown suite '{suite_name}'")

        cases = adapter.load_cases()
        case = next((c for c in cases if c.case_id == case_id), None)
        if case is None:
            raise ExecutionError(f"Case '{case_id}' not found in suite '{suite_name}'")

        run_id = f"{suite_name}-single-{uuid.uuid4().hex[:8]}"
        result = adapter.run_case(case, run_id)
        result.score = adapter.evaluate(result)
        result.passed = result.score >= 0.5
        return result

    def compare_runs(self, run_a: str, run_b: str) -> ComparisonReport:
        """Compare two benchmark runs and identify regressions and improvements.

        Computes deltas for pass@1, avg_score, avg_latency, tokens, and cost.
        Flags a regression or improvement when the absolute delta exceeds 5%.

        Args:
            run_a: Run ID of the baseline run.
            run_b: Run ID of the run to compare against the baseline.

        Returns:
            ComparisonReport with per-metric deltas and lists of regressions
            and improvements.

        Raises:
            ExecutionError: If either run ID is not found in the metric store.
        """
        a = self._store.load_report(run_a)
        b = self._store.load_report(run_b)

        if a is None or b is None:
            missing = run_a if a is None else run_b
            raise ExecutionError(f"Run '{missing}' not found in metric store")

        suite = a.get("suite_name", b.get("suite_name", "unknown"))
        comp = ComparisonReport(run_a=run_a, run_b=run_b, suite_name=suite)
        comp.delta_pass_at_1 = (b.get("pass_at_1", 0) or 0) - (a.get("pass_at_1", 0) or 0)
        comp.delta_avg_score = (b.get("avg_score", 0) or 0) - (a.get("avg_score", 0) or 0)
        comp.delta_avg_latency_ms = (b.get("avg_latency", 0) or 0) - (a.get("avg_latency", 0) or 0)
        comp.delta_total_tokens = (b.get("total_tokens", 0) or 0) - (a.get("total_tokens", 0) or 0)
        comp.delta_cost_usd = (b.get("total_cost", 0) or 0) - (a.get("total_cost", 0) or 0)

        threshold = 0.05
        if comp.delta_avg_score < -threshold:
            comp.regressions.append(f"avg_score dropped by {abs(comp.delta_avg_score):.4f}")
        elif comp.delta_avg_score > threshold:
            comp.improvements.append(f"avg_score improved by {comp.delta_avg_score:.4f}")

        if comp.delta_pass_at_1 < -threshold:
            comp.regressions.append(f"pass@1 dropped by {abs(comp.delta_pass_at_1):.4f}")
        elif comp.delta_pass_at_1 > threshold:
            comp.improvements.append(f"pass@1 improved by {comp.delta_pass_at_1:.4f}")

        if comp.delta_avg_latency_ms > threshold:
            comp.regressions.append(f"avg_latency_ms increased by {comp.delta_avg_latency_ms:.4f}")
        elif comp.delta_avg_latency_ms < -threshold:
            comp.improvements.append(f"avg_latency_ms decreased by {abs(comp.delta_avg_latency_ms):.4f}")

        if comp.delta_total_tokens > 0:
            comp.regressions.append(f"total_tokens increased by {comp.delta_total_tokens}")
        elif comp.delta_total_tokens < 0:
            comp.improvements.append(f"total_tokens decreased by {abs(comp.delta_total_tokens)}")

        if comp.delta_cost_usd > threshold:
            comp.regressions.append(f"total_cost_usd increased by {comp.delta_cost_usd:.6f}")
        elif comp.delta_cost_usd < -threshold:
            comp.improvements.append(f"total_cost_usd decreased by {abs(comp.delta_cost_usd):.6f}")

        return comp

    def track_metrics(self, report: BenchmarkReport) -> None:
        """Persist a benchmark report to SQLite.

        Args:
            report: The completed BenchmarkReport to persist.
        """
        self._store.save_report(report)

    def list_runs(self, suite_name: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List recent benchmark runs from the metric store.

        Args:
            suite_name: Optional suite filter.
            limit: Maximum number of runs to return.

        Returns:
            List of run summary dicts, most recent first.
        """
        return self._store.list_runs(suite_name, limit)

    def get_last_comparison(self, suite_name: str) -> ComparisonReport | None:
        """Compare the two most recent runs for a suite.

        Args:
            suite_name: The suite whose two most recent runs to compare.

        Returns:
            ComparisonReport comparing the second-most-recent run (baseline) against the
            most recent run, or None if fewer than two runs exist for the suite.
        """
        ids = self._store.last_two_run_ids(suite_name)
        if len(ids) < 2:
            return None
        return self.compare_runs(ids[1], ids[0])  # older vs newer


# ============================================================
# Convenience: default runner with all adapters registered
# ============================================================


def get_default_runner(*, allow_degraded: bool = False) -> BenchmarkRunner:
    """Create a BenchmarkRunner with all built-in adapters registered.

    Attempts to register SWE-bench, Tau-bench, ToolBench, TaskBench, and API-Bank
    adapters. Bootstrap failures fail closed unless ``allow_degraded`` is true.

    Args:
        allow_degraded: When true, return a runner with the adapters that loaded
            successfully instead of failing on adapter bootstrap errors.

    Returns:
        A BenchmarkRunner with all successfully loaded adapters registered.

    Raises:
        BenchmarkBootstrapError: If one or more built-in adapters fail to load
            and degraded startup is not allowed.
    """
    runner = BenchmarkRunner()
    failures: list[str] = []

    def record_failure(label: str, exc: Exception) -> None:
        logger.warning("Could not load %s adapter: %s", label, exc)
        failures.append(f"{label}: {exc}")

    try:
        from vetinari.benchmarks.swe_bench import SWEBenchAdapter

        runner.register_suite(SWEBenchAdapter())
    except Exception as exc:
        record_failure("SWE-bench", exc)

    try:
        from vetinari.benchmarks.tau_bench import TauBenchAdapter

        runner.register_suite(TauBenchAdapter())
    except Exception as exc:
        record_failure("Tau-bench", exc)

    try:
        from vetinari.benchmarks.toolbench import ToolBenchAdapter

        runner.register_suite(ToolBenchAdapter())
    except Exception as exc:
        record_failure("ToolBench", exc)

    try:
        from vetinari.benchmarks.taskbench import TaskBenchAdapter

        runner.register_suite(TaskBenchAdapter())
    except Exception as exc:
        record_failure("TaskBench", exc)

    try:
        from vetinari.benchmarks.api_bank import APIBankAdapter

        runner.register_suite(APIBankAdapter())
    except Exception as exc:
        record_failure("API-Bank", exc)

    try:
        from vetinari.benchmarks.external_probes import (
            AgentSecurityInvariantAdapter,
            EmbeddingQualityAdapter,
            LMEvalHarnessProbeAdapter,
            MCPAgentBenchmarkAdapter,
            OWASPLLMTop10Adapter,
        )

        runner.register_suite(OWASPLLMTop10Adapter())
        runner.register_suite(LMEvalHarnessProbeAdapter())
        runner.register_suite(EmbeddingQualityAdapter())
        runner.register_suite(MCPAgentBenchmarkAdapter())
        runner.register_suite(AgentSecurityInvariantAdapter())
    except Exception as exc:
        record_failure("local external-benchmark probes", exc)

    if failures and not allow_degraded:
        raise BenchmarkBootstrapError("default benchmark runner degraded: " + "; ".join(failures))

    return runner
