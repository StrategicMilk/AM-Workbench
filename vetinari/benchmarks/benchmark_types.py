"""Data classes and enums for Vetinari's multi-layer benchmark framework.

Extracted from ``runner.py`` to keep that module under the 550-line file
limit.  All names are re-exported from ``runner.py`` so existing import
paths remain valid.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.boundary_guards import require_nonempty

# ============================================================
# Enums
# ============================================================


class BenchmarkLayer(Enum):
    """Three-layer testing hierarchy."""

    AGENT = 1  # Individual agent tool-calling
    ORCHESTRATION = 2  # Multi-agent tool chains
    PIPELINE = 3  # Full end-to-end


class BenchmarkTier(Enum):
    """Speed tier for benchmark scheduling."""

    FAST = "fast"  # seconds
    MEDIUM = "medium"  # minutes
    SLOW = "slow"  # hours


# ============================================================
# Data classes
# ============================================================


@dataclass
class BenchmarkCase:
    """A single benchmark test case.

    Supports two usage patterns:
    1. api_bank pattern: case_id, suite_name, description, input_data, expected, metadata, tags
    2. suite.py pattern: case_id, agent_type, task_type, description, input, evaluator, expected_keys
    """

    case_id: str
    # suite.py pattern fields (with flexible defaults for backward compat)
    agent_type: str = ""
    task_type: str = ""
    # api_bank pattern fields
    suite_name: str = ""
    description: str = ""
    input_data: dict[str, Any] = field(default_factory=dict)
    input: str = ""  # suite.py pattern: raw input string
    evaluator: Callable[[Any], float] | None = None  # suite.py: scoring function
    expected: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    expected_keys: list[str] = field(default_factory=list)  # suite.py pattern

    def __post_init__(self) -> None:
        """Reject benchmark records that omit required identity fields."""
        self.case_id = require_nonempty(self.case_id, field_name="case_id")
        if self.suite_name:
            self.suite_name = require_nonempty(self.suite_name, field_name="suite_name")
        if self.agent_type:
            self.agent_type = require_nonempty(self.agent_type, field_name="agent_type")
        if self.task_type:
            self.task_type = require_nonempty(self.task_type, field_name="task_type")

    def __repr__(self) -> str:
        """Show identifying fields for debugging."""
        if self.suite_name:
            return f"BenchmarkCase(case_id={self.case_id!r}, suite_name={self.suite_name!r})"
        return f"BenchmarkCase(case_id={self.case_id!r}, agent_type={self.agent_type!r}, task_type={self.task_type!r})"


@dataclass
class BenchmarkResult:
    """Result from running a single benchmark case.

    Supports two usage patterns:
    1. api_bank pattern: case_id, suite_name, run_id, passed, score, latency_ms, tokens_consumed, etc.
    2. suite.py pattern: agent_type, timestamp, cases_run, cases_passed, avg_score, scores, details, duration_ms, error
    """

    # api_bank pattern (required fields with defaults to support suite.py)
    case_id: str = ""
    suite_name: str = ""
    run_id: str = ""
    passed: bool = False
    score: float = 0.0
    latency_ms: float = 0.0
    tokens_consumed: int = 0
    cost_usd: float = 0.0
    error_recovery_count: int = 0
    output: dict[str, Any] | None = None
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    # suite.py pattern fields
    agent_type: str = ""
    cases_run: int = 0
    cases_passed: int = 0
    avg_score: float = 0.0
    scores: list[float] = field(default_factory=list)
    details: list[dict] = field(default_factory=list)
    duration_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        """Show key fields for debugging."""
        if self.case_id and self.run_id:
            return (
                f"BenchmarkResult(case_id={self.case_id!r}, run_id={self.run_id!r}, "
                f"passed={self.passed!r}, score={self.score!r})"
            )
        return (
            f"BenchmarkResult(agent_type={self.agent_type!r}, cases_run={self.cases_run!r}, "
            f"cases_passed={self.cases_passed!r}, avg_score={self.avg_score!r})"
        )


@dataclass
class BenchmarkReport:
    """Aggregated report for a benchmark suite run."""

    run_id: str
    suite_name: str
    layer: BenchmarkLayer
    tier: BenchmarkTier
    results: list[BenchmarkResult]
    started_at: str
    finished_at: str = ""
    total_cases: int = 0
    passed_cases: int = 0
    pass_at_1: float = 0.0  # fraction passing on first try
    pass_k: float = 0.0  # consistency (Tau-bench style)
    avg_score: float = 0.0
    avg_latency_ms: float = 0.0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    error_recovery_rate: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject aggregate reports without benchmark identity fields."""
        self.run_id = require_nonempty(self.run_id, field_name="run_id")
        self.suite_name = require_nonempty(self.suite_name, field_name="suite_name")
        self.started_at = require_nonempty(self.started_at, field_name="started_at")

    def __repr__(self) -> str:
        """Show key fields for debugging."""
        return (
            f"BenchmarkReport(run_id={self.run_id!r}, suite_name={self.suite_name!r}, "
            f"total_cases={self.total_cases!r}, pass_at_1={self.pass_at_1!r})"
        )

    def compute_aggregates(self) -> None:
        """Recompute aggregate metrics from individual results."""
        if not self.results:
            return
        self.total_cases = len(self.results)
        first_attempts: dict[str, BenchmarkResult] = {}
        for result in self.results:
            first_attempts.setdefault(result.case_id, result)
        first_attempt_values = tuple(first_attempts.values())
        self.passed_cases = sum(1 for r in first_attempt_values if r.passed)
        self.pass_at_1 = self.passed_cases / len(first_attempt_values)
        self.avg_score = sum(r.score for r in self.results) / self.total_cases
        self.avg_latency_ms = sum(r.latency_ms for r in self.results) / self.total_cases
        self.total_tokens = sum(r.tokens_consumed for r in self.results)
        self.total_cost_usd = sum(r.cost_usd for r in self.results)
        recoveries = sum(r.error_recovery_count for r in self.results)
        errors_possible = sum(1 for r in self.results if r.error_recovery_count > 0 or r.error)
        self.error_recovery_rate = recoveries / errors_possible if errors_possible > 0 else 0.0
        self.metadata.setdefault("unique_case_count", len(first_attempt_values))
        self.metadata.setdefault("trial_count", len(self.results))

    def summary_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable summary.

        Returns:
            Dict with run_id, suite, layer, tier, and all aggregate metrics.
        """
        return {
            "run_id": self.run_id,
            "suite": self.suite_name,
            "layer": self.layer.name,
            "tier": self.tier.value,
            "total": self.total_cases,
            "passed": self.passed_cases,
            "pass@1": round(self.pass_at_1, 4),
            "pass^k": round(self.pass_k, 4),
            "avg_score": round(self.avg_score, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "error_recovery_rate": round(self.error_recovery_rate, 4),
        }


@dataclass
class ComparisonReport:
    """Comparison between two benchmark runs."""

    run_a: str
    run_b: str
    suite_name: str
    delta_pass_at_1: float = 0.0
    delta_avg_score: float = 0.0
    delta_avg_latency_ms: float = 0.0
    delta_total_tokens: int = 0
    delta_cost_usd: float = 0.0
    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key fields for debugging."""
        return (
            f"ComparisonReport(run_a={self.run_a!r}, run_b={self.run_b!r}, "
            f"suite_name={self.suite_name!r}, delta_pass_at_1={self.delta_pass_at_1!r})"
        )


# ============================================================
# Abstract base: BenchmarkSuiteAdapter
# ============================================================


class BenchmarkSuiteAdapter(ABC):
    """Base class for all benchmark adapters.

    Subclasses must define:
      - name: str
      - layer: BenchmarkLayer
      - tier: BenchmarkTier
      - load_cases() -> List[BenchmarkCase]
      - run_case(case) -> BenchmarkResult
      - evaluate(result) -> float
    """

    name: str = ""
    layer: BenchmarkLayer = BenchmarkLayer.AGENT
    tier: BenchmarkTier = BenchmarkTier.FAST

    @abstractmethod
    def load_cases(self, limit: int | None = None) -> list[BenchmarkCase]:
        """Load benchmark cases.

        Args:
            limit: Optional maximum number of cases to load.

        Returns:
            List of BenchmarkCase instances.
        """
        ...

    @abstractmethod
    def run_case(self, case: BenchmarkCase, run_id: str) -> BenchmarkResult:
        """Execute a single benchmark case and return its result.

        Args:
            case: The benchmark case to execute.
            run_id: Unique identifier for the current run.

        Returns:
            BenchmarkResult with score and pass/fail status.
        """
        ...

    @abstractmethod
    def evaluate(self, result: BenchmarkResult) -> float:
        """Score a result from 0.0 to 1.0.

        Args:
            result: The BenchmarkResult to evaluate.

        Returns:
            Float score in [0.0, 1.0].
        """
        ...

    def description(self) -> str:
        """Human-readable description of this benchmark suite.

        Returns:
            String combining name, layer, and tier.
        """
        return f"{self.name} (Layer {self.layer.value}, {self.tier.value})"
