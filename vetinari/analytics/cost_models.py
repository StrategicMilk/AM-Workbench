"""Cost analytics data models and default pricing."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, cast

from vetinari.boundary_guards import require_nonempty
from vetinari.utils.serialization import dataclass_to_dict

_LOCAL_INPUT_PER_1K = 0.00005
_LOCAL_OUTPUT_PER_1K = 0.00010
_LOCAL_PER_REQUEST = 0.00010

# Snapshot date; update when pricing table is refreshed from authoritative source.
_PRICING_BASELINE_DATE: str = "2026-01-01"


@dataclass(frozen=True, slots=True)
class ModelPricing:
    """USD cost per 1,000 tokens and per request."""

    input_per_1k: float = 0.0
    output_per_1k: float = 0.0
    per_request: float = 0.0

    def compute(self, input_tokens: int, output_tokens: int) -> float:
        """Calculate the total USD cost for input and output tokens.

        Args:
            input_tokens: Number of input tokens consumed.
            output_tokens: Number of output tokens generated.

        Returns:
            Total cost in USD.
        """
        return input_tokens / 1000 * self.input_per_1k + output_tokens / 1000 * self.output_per_1k + self.per_request


_DEFAULT_PRICING: dict[str, ModelPricing] = {
    "openai:gpt-4o": ModelPricing(input_per_1k=0.005, output_per_1k=0.015),
    "openai:gpt-4o-mini": ModelPricing(input_per_1k=0.0006, output_per_1k=0.0024),
    "openai:o3-mini": ModelPricing(input_per_1k=0.0011, output_per_1k=0.0044),
    "anthropic:claude-opus-4-8": ModelPricing(input_per_1k=0.005, output_per_1k=0.025),
    "anthropic:claude-opus-4-7": ModelPricing(input_per_1k=0.005, output_per_1k=0.025),
    "anthropic:claude-sonnet-4-6": ModelPricing(input_per_1k=0.003, output_per_1k=0.015),
    "anthropic:claude-haiku-4-5-20251001": ModelPricing(input_per_1k=0.001, output_per_1k=0.005),
    "google:gemini-3.5-flash": ModelPricing(input_per_1k=0.0015, output_per_1k=0.009),
    "google:gemini-3.1-flash-lite": ModelPricing(input_per_1k=0.00025, output_per_1k=0.0015),
    "local:*": ModelPricing(
        input_per_1k=_LOCAL_INPUT_PER_1K,
        output_per_1k=_LOCAL_OUTPUT_PER_1K,
        per_request=_LOCAL_PER_REQUEST,
    ),
    "am_engine:*": ModelPricing(
        input_per_1k=_LOCAL_INPUT_PER_1K,
        output_per_1k=_LOCAL_OUTPUT_PER_1K,
        per_request=_LOCAL_PER_REQUEST,
    ),
}


def require_model_pricing(model_id: str, pricing: dict[str, ModelPricing] | None = None) -> ModelPricing:
    """Return model pricing only when the model is explicitly configured.

    Args:
        model_id: Provider-qualified model id, such as ``"openai:gpt-4o"``.
        pricing: Optional pricing table override.

    Returns:
        Configured model pricing.

    Raises:
        KeyError: If no pricing row exists for the model id.
    """
    key = require_nonempty(model_id, field_name="model_id")
    table = _DEFAULT_PRICING if pricing is None else pricing
    try:
        return table[key]
    except KeyError as exc:
        raise KeyError(
            f"no pricing for model {key!r}; update _DEFAULT_PRICING (baseline: {_PRICING_BASELINE_DATE})"
        ) from exc


@dataclass(frozen=True, slots=True)
class CostEntry:
    """A single billable inference call."""

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    agent: str | None = None
    task_id: str | None = None
    project_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    timestamp: float = field(default_factory=time.time)
    cost_usd: float | None = None
    latency_ms: float = 0.0

    def __repr__(self) -> str:
        return (
            f"CostEntry(provider={self.provider!r}, model={self.model!r}, "
            f"agent={self.agent!r}, task_id={self.task_id!r}, cost_usd={self.cost_usd!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this cost entry to a plain dictionary.

        Returns:
            Dictionary containing all cost entry fields.
        """
        return cast(dict[str, Any], dataclass_to_dict(self))


@dataclass
class CostReport:
    """Aggregated cost breakdown."""

    total_cost_usd: float
    total_tokens: int
    total_requests: int
    by_agent: dict[str, float]
    by_provider: dict[str, float]
    by_model: dict[str, float]
    by_task: dict[str, float]
    by_project: dict[str, float]
    entries: int

    def __repr__(self) -> str:
        return (
            f"CostReport(total_cost_usd={self.total_cost_usd!r}, "
            f"total_tokens={self.total_tokens!r}, total_requests={self.total_requests!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this cost report to a plain dictionary.

        Returns:
            Dictionary containing aggregated cost breakdowns.
        """
        return cast(dict[str, Any], dataclass_to_dict(self))
