"""
Cost Benchmarking — Token Usage and Estimated Cost Tracking
============================================================

Measures token usage and estimated API cost per task type.
Supports per-run recording and JSON-serialisable summaries for CI
reporting and historical trend analysis.

Usage::

    from vetinari.benchmarks.cost_benchmark import CostBenchmark

    bench = CostBenchmark(task_type="plan_generation")
    bench.record(input_tokens=500, output_tokens=200, cost_per_1k=0.001)
    print(bench.to_dict())
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


# Cost constants for common models (USD per 1k tokens).
# These are reference defaults; callers should pass the actual rate.
DEFAULT_COST_PER_1K_USD = 0.001  # Conservative default for local/cheap models
CLAUDE_HAIKU_COST_PER_1K_USD = 0.00025  # Approximate input cost
CLAUDE_SONNET_COST_PER_1K_USD = 0.003  # Approximate blended cost


@dataclass
class CostBenchmark:
    """Measures token cost per task type.

    Records token usage for a single benchmarked operation and computes
    an estimated USD cost based on a configurable per-1k-token rate.

    Attributes:
        task_type: Logical label for the operation being measured
            (e.g., ``"plan_generation"``, ``"decomposition"``).
        input_tokens: Number of tokens in the prompt/input.
        output_tokens: Number of tokens in the model response.
        total_tokens: Sum of input and output tokens (set by ``record()``).
        estimated_cost_usd: Estimated cost in US dollars (set by ``record()``).
        recorded_at: ISO 8601 timestamp when ``record()`` was last called.
    """

    task_type: str
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    recorded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return f"CostBenchmark(task_type={self.task_type!r}, total_tokens={self.total_tokens!r}, estimated_cost_usd={self.estimated_cost_usd!r})"

    def record(self, input_tokens: int, output_tokens: int, cost_per_1k: float = DEFAULT_COST_PER_1K_USD) -> None:
        """Record token usage and calculate cost.

        Updates ``input_tokens``, ``output_tokens``, ``total_tokens``,
        ``estimated_cost_usd``, and ``recorded_at`` in place.

        Args:
            input_tokens: Number of prompt/input tokens consumed.
            output_tokens: Number of response/output tokens produced.
            cost_per_1k: Price in USD per 1,000 tokens. Defaults to
                ``DEFAULT_COST_PER_1K_USD`` (0.001).

        Raises:
            ValueError: If ``input_tokens`` or ``output_tokens`` is negative,
                or if ``cost_per_1k`` is negative.
        """
        if input_tokens < 0:
            raise ValueError(f"input_tokens must be non-negative, got {input_tokens}")
        if output_tokens < 0:
            raise ValueError(f"output_tokens must be non-negative, got {output_tokens}")
        if cost_per_1k < 0:
            raise ValueError(f"cost_per_1k must be non-negative, got {cost_per_1k}")

        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = input_tokens + output_tokens
        self.estimated_cost_usd = round((self.total_tokens / 1000.0) * cost_per_1k, 8)
        self.recorded_at = datetime.now(timezone.utc).isoformat()

        logger.debug(
            "CostBenchmark[%s]: %d input + %d output = %d tokens, $%.6f",
            self.task_type,
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
            self.estimated_cost_usd,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)


def aggregate_cost_benchmarks(benchmarks: list[CostBenchmark]) -> dict[str, Any]:
    """Aggregate a list of CostBenchmark results into a summary.

    Computes totals and per-task-type averages across all provided
    benchmarks.

    Args:
        benchmarks: List of ``CostBenchmark`` instances to aggregate.

    Returns:
        Dictionary with keys:

        - ``total_input_tokens``: Sum of all input tokens.
        - ``total_output_tokens``: Sum of all output tokens.
        - ``total_tokens``: Sum of all tokens.
        - ``total_cost_usd``: Sum of all estimated costs.
        - ``by_task_type``: Dict mapping task type to its aggregated metrics.
        - ``benchmark_count``: Number of benchmarks included.
    """
    if not benchmarks:
        return {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "by_task_type": {},
            "benchmark_count": 0,
        }

    by_task: dict[str, list[CostBenchmark]] = {}
    for b in benchmarks:
        by_task.setdefault(b.task_type, []).append(b)

    summary_by_type: dict[str, dict[str, Any]] = {}
    for task_type, items in by_task.items():
        total_in = sum(i.input_tokens for i in items)
        total_out = sum(i.output_tokens for i in items)
        total_tok = sum(i.total_tokens for i in items)
        total_cost = sum(i.estimated_cost_usd for i in items)
        count = len(items)
        summary_by_type[task_type] = {
            "count": count,
            "total_input_tokens": total_in,
            "total_output_tokens": total_out,
            "total_tokens": total_tok,
            "total_cost_usd": round(total_cost, 8),
            "avg_tokens_per_run": round(total_tok / count, 1),
            "avg_cost_per_run_usd": round(total_cost / count, 8),
        }

    return {
        "total_input_tokens": sum(b.input_tokens for b in benchmarks),
        "total_output_tokens": sum(b.output_tokens for b in benchmarks),
        "total_tokens": sum(b.total_tokens for b in benchmarks),
        "total_cost_usd": round(sum(b.estimated_cost_usd for b in benchmarks), 8),
        "by_task_type": summary_by_type,
        "benchmark_count": len(benchmarks),
    }
