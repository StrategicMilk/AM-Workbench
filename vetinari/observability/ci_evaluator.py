"""CI/CD continuous evaluation pipeline (P10.12).

Provides a lightweight evaluation harness for running eval suites against
LLM adapters, detecting quality drift, and gating deployments.

No LLM calls are made internally -- the caller supplies the adapter.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from vetinari.exceptions import ConfigurationError

logger = logging.getLogger(__name__)


# Minimum latency increase (ms) that triggers a regression flag, regardless of
# pass-rate changes.  Chosen to catch significant real-world slowdowns without
# false-positives from normal jitter.
_LATENCY_REGRESSION_THRESHOLD_MS: float = 500.0

# Sentinel values that read as "field present" syntactically but carry no
# correctness signal. score_result_dict must reject these so a `{"health_score":
# None}` dict cannot false-green the CI pipeline.
_INVALID_FIELD_VALUES: frozenset[Any] = frozenset({None, 0, "", "unknown", "none", "n/a", "null"})


def score_result_dict(result: dict[str, Any], required_fields: list[str]) -> float:
    """Score a result dict by value validity, not key presence.

    Replaces the pre-fix pattern ``1.0 if key in result else 0.0`` which scored a
    field as fully passing whenever the key existed, even when the value was
    ``None``, ``False``, ``0``, ``""``, or a sentinel like ``"unknown"``. The
    fixed scoring counts a field as valid only when its value is non-empty and
    non-sentinel.

    Args:
        result: Result dict produced upstream (e.g. a CI health report row).
        required_fields: Field names that must be present *and* carry a valid
            value to count toward the score.

    Returns:
        Fraction of required fields whose values are valid. ``1.0`` when every
        field is valid, ``0.0`` when none are. Returns ``0.0`` for an empty
        ``required_fields`` list (no fields, no evidence — fail-closed).
    """
    if not required_fields:
        return 0.0
    valid = 0
    for field_name in required_fields:
        value = result.get(field_name)
        if value in _INVALID_FIELD_VALUES:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        valid += 1
    return valid / len(required_fields)


# ---------------------------------------------------------------------------
# Adapter protocol
# ---------------------------------------------------------------------------


class AdapterProtocol(Protocol):
    """Minimal interface expected from an LLM adapter."""

    def complete(self, prompt: str, model_id: str, max_tokens: int = 512) -> str:
        """Generate a completion for prompt using model_id.

        Args:
            prompt: Input prompt string.
            model_id: Identifier of the model to use.
            max_tokens: Upper bound on tokens to generate.

        Returns:
            Generated text string.
        """
        ...


# ---------------------------------------------------------------------------
# Core dataclasses
# ---------------------------------------------------------------------------


@dataclass
class EvalCase:
    """A single evaluation test case.

    Attributes:
        name: Unique case identifier.
        prompt: Input prompt sent to the adapter.
        expected_contains: Substrings that must appear in the output.
        expected_not_contains: Substrings that must NOT appear in the output.
        max_latency_ms: Maximum acceptable latency in milliseconds.
        min_quality_score: Minimum pass threshold (0.0 - 1.0).
    """

    name: str
    prompt: str
    expected_contains: list[str] = field(default_factory=list)
    expected_not_contains: list[str] = field(default_factory=list)
    max_latency_ms: int = 10_000
    min_quality_score: float = 0.5

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"EvalCase(name={self.name!r},"
            f" max_latency_ms={self.max_latency_ms!r},"
            f" min_quality_score={self.min_quality_score!r})"
        )


@dataclass(frozen=True, slots=True)
class CaseResult:
    """Result for a single EvalCase.

    Attributes:
        name: Case name (matches EvalCase.name).
        passed: True when all checks passed.
        latency_ms: Measured latency in milliseconds.
        quality_score: Computed quality score (0.0 - 1.0).
        output_snippet: First 200 characters of the adapter output.
        failure_reason: Human-readable explanation when passed is False.
    """

    name: str
    passed: bool
    latency_ms: float
    quality_score: float
    output_snippet: str
    failure_reason: str = ""

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"CaseResult(name={self.name!r}, passed={self.passed!r}, quality_score={self.quality_score!r})"


@dataclass
class CIReport:
    """Aggregate report for a full eval suite run.

    Attributes:
        total: Total number of cases attempted.
        passed: Cases that passed all checks.
        failed: Cases that failed at least one check.
        skipped: Cases skipped due to errors.
        pass_rate: passed / total (0.0 - 1.0).
        cases: Ordered list of CaseResult objects.
        baseline_comparison: Drift dict produced by compare_to_baseline.
    """

    total: int
    passed: int
    failed: int
    skipped: int
    pass_rate: float
    cases: list[CaseResult]
    baseline_comparison: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"CIReport(total={self.total!r}, passed={self.passed!r}, pass_rate={self.pass_rate!r})"


# ---------------------------------------------------------------------------
# CIEvaluator
# ---------------------------------------------------------------------------


class CIEvaluator:
    """Runs evaluation suites and gates deployments.

    Instantiate via get_ci_evaluator().

    Example::

        checker = get_ci_evaluator()
        cases = checker.load_eval_suite("evals/suite.json")
        report = checker.run_eval(cases, adapter, "qwen-32b")
        if checker.should_halt_deployment(report):
            raise SystemExit("Quality gate failed")
    """

    def __init__(self) -> None:
        self._last_report: CIReport | None = None

    # ------------------------------------------------------------------
    # Suite loading
    # ------------------------------------------------------------------

    def load_eval_suite(self, filepath: str) -> list[EvalCase]:
        """Load eval cases from a JSON file.

        The JSON must be an array of objects with at minimum a ``name`` and
        ``prompt`` key. All other fields are optional and fall back to
        EvalCase defaults.

        Args:
            filepath: Path to the JSON file.

        Returns:
            List of EvalCase objects.

        Raises:
            FileNotFoundError: If filepath does not exist.
            ValueError: If the JSON is malformed or missing required keys.
        """
        path = Path(filepath)
        if not path.exists():
            raise FileNotFoundError(f"Eval suite not found: {filepath}")

        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)

        if not isinstance(raw, list):
            raise ConfigurationError(f"Eval suite must be a JSON array, got {type(raw).__name__}")

        cases: list[EvalCase] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ConfigurationError(f"Each eval case must be a JSON object, got {type(item).__name__}")
            try:
                cases.append(
                    EvalCase(
                        name=item["name"],
                        prompt=item["prompt"],
                        expected_contains=item.get("expected_contains", []),
                        expected_not_contains=item.get("expected_not_contains", []),
                        max_latency_ms=int(item.get("max_latency_ms", 10_000)),
                        min_quality_score=float(item.get("min_quality_score", 0.5)),
                    ),
                )
            except KeyError as exc:
                raise ConfigurationError(f"Eval case missing required key: {exc}") from exc

        logger.info("Loaded %d eval cases from %s", len(cases), filepath)
        return cases

    # ------------------------------------------------------------------
    # Eval execution
    # ------------------------------------------------------------------

    def run_eval(
        self,
        cases: list[EvalCase],
        adapter: Any,
        model_id: str,
    ) -> CIReport:
        """Run all eval cases against adapter and return a report.

        Args:
            cases: List of EvalCase objects to check.
            adapter: Object with a complete(prompt, model_id) method.
            model_id: Model identifier forwarded to the adapter.

        Returns:
            A CIReport aggregating all results.
        """
        results: list[CaseResult] = []
        n_passed = n_failed = n_skipped = 0

        for case in cases:
            result = self._run_single(case, adapter, model_id)
            results.append(result)
            if result.passed:
                n_passed += 1
            elif result.failure_reason.startswith("SKIPPED"):
                n_skipped += 1
            else:
                n_failed += 1

        total = len(cases)
        pass_rate = n_passed / total if total else 0.0

        report = CIReport(
            total=total,
            passed=n_passed,
            failed=n_failed,
            skipped=n_skipped,
            pass_rate=round(pass_rate, 4),
            cases=results,
        )
        self._last_report = report
        logger.info(
            "Eval complete: %d/%d passed (%.1f%%)",
            n_passed,
            total,
            pass_rate * 100,
        )
        return report

    # ------------------------------------------------------------------
    # Baseline comparison
    # ------------------------------------------------------------------

    def compare_to_baseline(
        self,
        current: CIReport,
        baseline: CIReport,
    ) -> dict[str, Any]:
        """Detect quality drift between two reports.

        Args:
            current: The most-recent eval report.
            baseline: A prior reference report to compare against.

        Returns:
            Dictionary with keys: pass_rate_delta, latency_delta_ms,
            regression_detected, improvement_detected, regressed_cases,
            baseline_pass_rate, current_pass_rate.
        """
        pass_rate_delta = current.pass_rate - baseline.pass_rate
        latency_delta = self._mean_latency(current) - self._mean_latency(baseline)

        baseline_map = {r.name: r for r in baseline.cases}
        regressions: list[str] = []
        for result in current.cases:
            b = baseline_map.get(result.name)
            if b and b.passed and not result.passed:
                regressions.append(result.name)

        return {
            "pass_rate_delta": round(pass_rate_delta, 4),
            "latency_delta_ms": round(latency_delta, 2),
            "regression_detected": pass_rate_delta <= -0.05 or latency_delta >= _LATENCY_REGRESSION_THRESHOLD_MS,
            "improvement_detected": pass_rate_delta >= 0.05,
            "regressed_cases": regressions,
            "baseline_pass_rate": baseline.pass_rate,
            "current_pass_rate": current.pass_rate,
        }

    # ------------------------------------------------------------------
    # Deployment gate
    # ------------------------------------------------------------------

    def should_halt_deployment(
        self,
        report: CIReport,
        min_pass_rate: float = 0.95,
    ) -> bool:
        """Determine whether a deployment should be blocked.

        Args:
            report: The CIReport to check.
            min_pass_rate: Minimum acceptable pass rate (default 0.95).

        Returns:
            True when the deployment should be halted.
        """
        halt = report.pass_rate < min_pass_rate
        if halt:
            logger.warning(
                "Deployment halted: pass_rate=%.2f%% < threshold=%.2f%%",
                report.pass_rate * 100,
                min_pass_rate * 100,
            )
        return halt

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_single(
        self,
        case: EvalCase,
        adapter: Any,
        model_id: str,
    ) -> CaseResult:
        """Execute one eval case and return its result.

        Args:
            case: The case to run.
            adapter: LLM adapter.
            model_id: Model to invoke.

        Returns:
            A CaseResult.
        """
        t0 = time.monotonic()
        output = ""
        try:
            output = adapter.complete(case.prompt, model_id)
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1_000
            logger.warning("Eval case %r raised: %s", case.name, exc)
            return CaseResult(
                name=case.name,
                passed=False,
                latency_ms=round(latency_ms, 2),
                quality_score=0.0,
                output_snippet="",
                failure_reason=f"adapter_error: {exc}",
            )

        latency_ms = (time.monotonic() - t0) * 1_000
        quality_score, failure_reason = self._score_output(case, output, latency_ms)
        return CaseResult(
            name=case.name,
            passed=not failure_reason,
            latency_ms=round(latency_ms, 2),
            quality_score=round(quality_score, 4),
            output_snippet=output[:200],
            failure_reason=failure_reason,
        )

    @staticmethod
    def _score_output(
        case: EvalCase,
        output: str,
        latency_ms: float,
    ) -> tuple[float, str]:
        """Score output against case criteria.

        Args:
            case: The eval case with expectations.
            output: Raw output string from the adapter.
            latency_ms: Observed latency.

        Returns:
            Tuple of (quality_score 0.0-1.0, failure_reason string).
            failure_reason is empty on pass.
        """
        failures: list[str] = []
        checks_total = 0
        checks_passed = 0

        for substring in case.expected_contains:
            checks_total += 1
            if substring.lower() in output.lower():
                checks_passed += 1
            else:
                failures.append(f"missing expected substring: {substring!r}")

        for substring in case.expected_not_contains:
            checks_total += 1
            if substring.lower() not in output.lower():
                checks_passed += 1
            else:
                failures.append(f"found forbidden substring: {substring!r}")

        checks_total += 1
        if latency_ms <= case.max_latency_ms:
            checks_passed += 1
        else:
            failures.append(f"latency {latency_ms:.0f}ms exceeds limit {case.max_latency_ms}ms")

        quality_score = checks_passed / checks_total if checks_total else 1.0
        if quality_score < case.min_quality_score and not failures:
            failures.append(f"quality score {quality_score:.2f} below threshold {case.min_quality_score}")

        return quality_score, "; ".join(failures)

    @staticmethod
    def _mean_latency(report: CIReport) -> float:
        """Return mean latency across all cases in report.

        Args:
            report: The CI report.

        Returns:
            Mean latency in milliseconds, or 0.0 if no cases.
        """
        if not report.cases:
            return 0.0
        return sum(r.latency_ms for r in report.cases) / len(report.cases)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_ci_instance: CIEvaluator | None = None
_ci_lock = threading.Lock()


def get_ci_evaluator() -> CIEvaluator:
    """Return the process-global CIEvaluator instance.

    Returns:
        The singleton CIEvaluator.
    """
    global _ci_instance
    if _ci_instance is None:
        with _ci_lock:
            if _ci_instance is None:
                _ci_instance = CIEvaluator()
    return _ci_instance


def reset_ci_evaluator() -> None:
    """Reset the singleton (intended for testing)."""
    global _ci_instance
    with _ci_lock:
        _ci_instance = None
