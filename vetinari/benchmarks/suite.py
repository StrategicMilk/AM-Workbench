"""
Vetinari Comprehensive Benchmark Suite
========================================
Standardized evaluation tasks per agent type.

Tracks quality over time and alerts on regressions after prompt/model changes.
Benchmarks are offline by default and can also load production trace JSONL
exports for component-level replay without making network calls.

Usage::

    from vetinari.benchmarks.suite import BenchmarkSuite, run_benchmark

    suite = BenchmarkSuite()
    results = suite.run_all()
    suite.print_report(results)

    # Or run a single agent
    agent_results = suite.run_agent("WORKER")
    logger.debug("Worker: %.3f", agent_results.avg_score)
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.benchmarks.benchmark_types import BenchmarkCase, BenchmarkResult
from vetinari.benchmarks.suite_cases import _build_default_cases
from vetinari.constants import _PROJECT_ROOT
from vetinari.types import AgentType
from vetinari.workbench.cost.jsonl_rotator import RotatingJsonlStore
from vetinari.workbench.cost.token_cost_split import PricingConfigError, load_rotation_settings

logger = logging.getLogger(__name__)


_RESULTS_PATH = _PROJECT_ROOT / "vetinari_benchmarks.jsonl"
_RESULTS_ROTATION_KEY = "benchmarks_jsonl"
_PLACEHOLDER_STRINGS = {
    "",
    "n/a",
    "na",
    "none",
    "no issues",
    "no issue",
    "no changes",
    "no changes needed",
    "ok",
    "pass",
    "passed",
    "todo",
    "tbd",
    "unknown",
}


@dataclass
class SuiteCase:
    """A single benchmark test case."""

    case_id: str
    agent_type: str
    task_type: str
    description: str
    input: str
    evaluator: Callable[[Any], float]  # Returns score 0.0-1.0
    expected_keys: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"SuiteCase(case_id={self.case_id!r}, agent_type={self.agent_type!r}, task_type={self.task_type!r})"


@dataclass
class SuiteResult:
    """Result of running a set of benchmark cases."""

    agent_type: str
    timestamp: str
    cases_run: int
    cases_passed: int
    avg_score: float
    scores: list[float] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)
    duration_ms: float = 0.0
    error: str = ""

    def __repr__(self) -> str:
        return (
            f"SuiteResult(agent_type={self.agent_type!r}, cases_run={self.cases_run!r}, "
            f"cases_passed={self.cases_passed!r}, avg_score={self.avg_score!r})"
        )


def compute_velocity_trend(recent_scores: list[float]) -> str:
    """Classify benchmark run velocity from the last N scores.

    Replaces the previously-hardcoded ``velocity_trend = "flat"`` field. The
    classifier compares the first and last score in ``recent_scores``: a >2%
    rise marks ``"improving"``, a >2% fall marks ``"degrading"``, anything
    else marks ``"flat"``. Fewer than 2 data points fall back to ``"flat"``.

    Args:
        recent_scores: Ordered list of benchmark suite scores, oldest first.

    Returns:
        One of ``"improving"``, ``"degrading"``, or ``"flat"``.
    """
    if len(recent_scores) < 2:
        return "flat"
    first = float(recent_scores[0])
    last = float(recent_scores[-1])
    if first <= 0.0:
        return "improving" if last > first else "flat"
    ratio = last / first
    if ratio > 1.02:
        return "improving"
    if ratio < 0.98:
        return "degrading"
    return "flat"


def score_token_f1(expected: str, actual: str) -> float:
    """Token-F1 overlap between expected and actual strings.

    Replaces the prior ``expected in actual`` substring-containment scorer.
    Whitespace-split lowercased tokens form the comparison sets; F1 is the
    harmonic mean over precision and recall on those sets. Identical strings
    score ``1.0``; disjoint strings score ``0.0``; partial overlaps score
    proportionally so a single matching token in a long sentence cannot
    false-green the case.

    Args:
        expected: Reference text. Empty input returns ``0.0`` (no signal).
        actual: Candidate text.

    Returns:
        F1 value in the closed interval ``[0.0, 1.0]``.
    """
    expected_tokens = expected.lower().split()
    actual_tokens = actual.lower().split()
    if not expected_tokens or not actual_tokens:
        return 0.0
    expected_set = set(expected_tokens)
    actual_set = set(actual_tokens)
    intersection = expected_set & actual_set
    if not intersection:
        return 0.0
    return (2 * len(intersection)) / (len(expected_set) + len(actual_set))


def _score_by_keys(output: Any, required_keys: list[str]) -> float:
    """Score output based on required keys containing substantive content."""
    if not isinstance(output, dict) and isinstance(output, str):
        try:
            output = json.loads(output)
        except Exception:
            logger.warning("Could not parse benchmark output as JSON - scoring as 0.0")
            return 0.0
    if not isinstance(output, dict):
        return 0.0
    found = sum(1 for k in required_keys if _is_substantive_benchmark_value(output.get(k)))
    return found / max(len(required_keys), 1)


def _is_substantive_benchmark_value(value: Any, *, nested: bool = False) -> bool:
    """Reject key-fill placeholders while accepting concrete benchmark payloads."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return True
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.lower() in _PLACEHOLDER_STRINGS:
            return False
        if nested:
            return bool(stripped)
        return len(stripped) >= 3
    if isinstance(value, list):
        return bool(value) and any(_is_substantive_benchmark_value(item, nested=True) for item in value)
    if isinstance(value, dict):
        return bool(value) and any(_is_substantive_benchmark_value(item, nested=True) for item in value.values())
    return bool(value)


class BenchmarkSuite:
    """Runs standardized benchmarks across all Vetinari agents."""

    PASS_THRESHOLD = 0.6  # Score >= this is considered passing

    def __init__(self):
        self._cases: list[BenchmarkCase] = self._build_cases()

    # ------------------------------------------------------------------
    # Case definitions
    # ------------------------------------------------------------------

    @staticmethod
    def _build_cases() -> list[BenchmarkCase]:
        """Define benchmark cases for the 3-agent pipeline (FOREMAN, WORKER, INSPECTOR).

        Includes both structured-output cases (scored by key presence via
        ``_score_by_keys``) and a text-output case for INSPECTOR whose expected
        answer is evaluated via :func:`make_text_evaluator` (token-F1 scorer).
        """
        cases = _build_default_cases(_score_by_keys)
        # Text-output case: expected plain-English answer evaluated via token-F1.
        # make_text_evaluator is the canonical way to build text evaluators for
        # BenchmarkCase instances whose output is a plain string rather than a
        # structured dict.
        cases.append(
            BenchmarkCase(
                case_id="inspector_text_quality_001",
                agent_type=AgentType.INSPECTOR.value,
                task_type="analysis",
                description="Summarise security risk of eval() usage",
                input="What is the security risk of using eval() in Python?",
                evaluator=make_text_evaluator(
                    "eval executes arbitrary code and is a security risk that allows code injection"
                ),
                expected_keys=[],
            )
        )
        return cases

    def load_production_trace_cases(self, trace_path: Path) -> int:
        """Load production trace replay cases from JSONL.

        Each line must contain ``case_id``, ``agent_type``, ``input``, and
        ``expected_keys``. Invalid or incomplete rows fail closed with
        ``ValueError`` so trace replay cannot silently degrade into mocked-only
        benchmark coverage.

        Args:
            trace_path: JSONL file containing exported production trace cases.

        Returns:
            Number of trace replay cases added to the suite.

        Raises:
            ValueError: If a row is missing required fields, has invalid
                ``expected_keys``, or the file contains no replayable cases.
        """
        loaded: list[BenchmarkCase] = []
        with trace_path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                required = ("case_id", "agent_type", "input", "expected_keys")
                missing = [key for key in required if key not in row]
                if missing:
                    raise ValueError(f"{trace_path}:{lineno} missing required trace field(s): {', '.join(missing)}")
                expected_keys = row["expected_keys"]
                if not isinstance(expected_keys, list) or not all(isinstance(key, str) for key in expected_keys):
                    raise ValueError(f"{trace_path}:{lineno} expected_keys must be a list of strings")
                loaded.append(
                    BenchmarkCase(
                        case_id=str(row["case_id"]),
                        agent_type=str(row["agent_type"]),
                        task_type=str(row.get("task_type", "production_trace")),
                        description=str(row.get("description", "Production trace replay")),
                        input=str(row["input"]),
                        evaluator=lambda output, keys=expected_keys: _score_by_keys(output, keys),
                        expected_keys=expected_keys,
                        metadata={"source_trace": str(trace_path), "trace_lineno": lineno},
                    )
                )
        if not loaded:
            raise ValueError(f"{trace_path} contained no replayable trace cases")
        self._cases.extend(loaded)
        return len(loaded)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run_all(self, agent_types: list[str] | None = None) -> list[BenchmarkResult]:
        """Run all benchmark cases, optionally filtered to specific agents.

        Args:
            agent_types: When provided, only runs cases for agents in this list.
                Defaults to all distinct agent types defined in the suite.

        Returns:
            List of BenchmarkResult, one per agent type, persisted to the JSONL results file.
        """
        results = []
        types_to_test = agent_types or list({c.agent_type for c in self._cases})

        for agent_type in types_to_test:
            result = self.run_agent(agent_type)
            results.append(result)
            self._persist(result)

        return results

    def run_agent(self, agent_type: str) -> BenchmarkResult:
        """Run all benchmark cases for a specific agent type and aggregate the scores.

        Args:
            agent_type: Agent type string to run cases for (e.g. ``"FOREMAN"``, ``"WORKER"``).

        Returns:
            BenchmarkResult with avg_score, pass/fail count, per-case scores and details,
            and total duration. Returns a zero-score result if no cases are defined for
            the given agent type.
        """
        cases = [c for c in self._cases if c.agent_type == agent_type]
        if not cases:
            return BenchmarkResult(
                agent_type=agent_type,
                timestamp=datetime.now(timezone.utc).isoformat(),
                cases_run=0,
                cases_passed=0,
                avg_score=0.0,
                error="No benchmark cases defined",
            )

        scores = []
        details = []
        start = time.time()

        for case in cases:
            score, detail = self._run_case(case)
            scores.append(score)
            details.append(detail)

        duration = (time.time() - start) * 1000
        avg = sum(scores) / max(len(scores), 1)
        passed = sum(1 for s in scores if s >= self.PASS_THRESHOLD)

        return BenchmarkResult(
            agent_type=agent_type,
            timestamp=datetime.now(timezone.utc).isoformat(),
            cases_run=len(cases),
            cases_passed=passed,
            avg_score=round(avg, 3),
            scores=scores,
            details=details,
            duration_ms=round(duration, 1),
        )

    def _run_case(self, case: BenchmarkCase) -> tuple:
        """Execute a single benchmark case. Returns (score, detail_dict)."""
        try:
            from vetinari.agents.contracts import AgentTask
            from vetinari.orchestration.agent_graph import get_agent_graph

            graph = get_agent_graph()
            agent_type_enum = AgentType(case.agent_type)
            agent = graph.get_agent(agent_type_enum)

            if agent is None:
                return 0.0, {
                    "case_id": case.case_id,
                    "score": 0.0,
                    "error": "Agent not available in graph",
                }

            task = AgentTask(
                task_id=f"bench_{case.case_id}",
                agent_type=agent_type_enum,
                description=case.input,
                prompt=case.input,
            )

            result = agent.execute(task)
            if not result.success:
                return 0.2, {
                    "case_id": case.case_id,
                    "score": 0.2,
                    "error": f"Agent returned failure: {result.errors}",
                }

            score = case.evaluator(result.output)
            return score, {
                "case_id": case.case_id,
                "score": round(score, 3),
                "passed": score >= self.PASS_THRESHOLD,
                "output_keys": list(result.output.keys())
                if isinstance(result.output, dict)
                else type(result.output).__name__,
            }

        except Exception as e:
            logger.warning("[Benchmark] Case %s failed: %s", case.case_id, e)
            return 0.0, {"case_id": case.case_id, "score": 0.0, "error": str(e)[:200]}

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def print_report(self, results: list[BenchmarkResult]) -> None:
        """Print a human-readable benchmark report."""
        logger.info("\n" + "=" * 60)
        logger.info("VETINARI BENCHMARK REPORT — %s", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
        logger.info("=" * 60)
        for r in sorted(results, key=lambda x: -x.avg_score):
            status = "PASS" if r.avg_score >= self.PASS_THRESHOLD else "FAIL"
            logger.info(
                f"  [{status}] {r.agent_type:<25} "
                f"score={r.avg_score:.3f}  "
                f"passed={r.cases_passed}/{r.cases_run}  "
                f"({r.duration_ms:.0f}ms)",
            )
        overall = sum(r.avg_score for r in results) / max(len(results), 1)
        logger.info("=" * 60)
        logger.info("  OVERALL AVG: %.3f", overall)
        logger.info("=" * 60 + "\n")

    def check_regression(self, new_results: list[BenchmarkResult], threshold: float = 0.05) -> list[str]:
        """Compare new results against the historical baseline from persisted JSONL results.

        Args:
            new_results: BenchmarkResult objects from the current run to compare.
            threshold: Minimum score drop to report as a regression (default 0.05 = 5%).

        Returns:
            List of human-readable regression strings for any agent whose average score
            dropped by more than ``threshold`` compared to historical averages. Empty list
            if no regressions are detected.
        """
        regressions = []
        historical = self._load_historical()

        for result in new_results:
            baseline = historical.get(result.agent_type)
            if baseline and (baseline - result.avg_score) > threshold:
                regressions.append(
                    f"{result.agent_type}: {baseline:.3f} -> {result.avg_score:.3f} "
                    f"(delta=-{baseline - result.avg_score:.3f})",
                )
        return regressions

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self, result: BenchmarkResult) -> None:
        try:
            import dataclasses

            _benchmark_results_store().append(dataclasses.asdict(result))
        except Exception as e:
            logger.warning("[Benchmark] Persist failed: %s", e)

    @staticmethod
    def _load_historical() -> dict[str, float]:
        """Load per-agent average scores from historical results."""
        if not _RESULTS_PATH.exists():
            return {}
        by_agent: dict[str, list[float]] = {}
        try:
            for row in _benchmark_results_store().read_rows(include_archives=True):
                agent = row.get("agent_type", "")
                score = row.get("avg_score", 0.0)
                by_agent.setdefault(agent, []).append(score)
            return {k: sum(v) / len(v) for k, v in by_agent.items()}
        except Exception:
            logger.warning("Failed to compute agent benchmark averages", exc_info=True)
            return {}


def _benchmark_results_store() -> RotatingJsonlStore:
    """Return the configured rotating benchmark-results JSONL store."""
    try:
        rotation = load_rotation_settings(_RESULTS_ROTATION_KEY)
    except PricingConfigError:
        logger.warning("Benchmark result rotation config unavailable; using defaults", exc_info=True)
        return RotatingJsonlStore(_RESULTS_PATH)
    return RotatingJsonlStore(
        _RESULTS_PATH,
        max_bytes=rotation.max_bytes,
        max_lines=rotation.max_lines,
        backup_count=rotation.backup_count,
    )


def make_text_evaluator(expected: str) -> Callable[[Any], float]:
    """Build a token-F1 evaluator for text-output benchmark cases.

    Wraps ``score_token_f1`` so callers get a reusable ``evaluator`` callable
    for ``BenchmarkCase`` instances whose output is a plain string. Passing
    ``expected`` at construction time pins the reference text.

    Args:
        expected: Reference text that the agent output will be compared against.

    Returns:
        A zero-argument-free callable ``(output: Any) -> float`` that converts
        ``output`` to a string and returns its token-F1 score against
        ``expected``.
    """

    def _evaluator(output: Any) -> float:
        actual = output if isinstance(output, str) else str(output)
        return score_token_f1(expected, actual)

    return _evaluator


def run_benchmark(agent_types: list[str] | None = None) -> list[BenchmarkResult]:
    """Run the benchmark suite and log a report with regression warnings.

    Args:
        agent_types: When provided, limits the run to the specified agent types.
            Defaults to all agent types defined in the suite.

    Returns:
        List of BenchmarkResult, one per agent type, after printing the report and
        emitting a warning log for any detected regressions.
    """
    suite = BenchmarkSuite()
    results = suite.run_all(agent_types)
    suite.print_report(results)
    regressions = suite.check_regression(results)
    if regressions:
        logger.warning("[Benchmark] REGRESSIONS DETECTED:\n%s", "\n".join(regressions))

    # Compute velocity trend across the current batch of agent avg_scores so
    # callers have a quick signal on whether overall quality is improving or
    # degrading.  A single run produces a one-point sequence → "flat" is
    # expected; repeated runs from the caller show meaningful trends.
    recent_scores = [r.avg_score for r in sorted(results, key=lambda r: r.timestamp)]
    velocity = compute_velocity_trend(recent_scores)
    logger.info("[Benchmark] velocity_trend=%s (agents=%d)", velocity, len(results))

    return results
