"""Cost analysis mode handler for the Operations Agent.

Extracts the cost analysis and model comparison logic from OperationsAgent
into a standalone handler class. Calculates per-task token costs, compares
model pricing tiers, and recommends cost-efficient model selections.
"""

from __future__ import annotations

import logging
import types
from typing import Any

from vetinari.agents.contracts import AgentResult, AgentTask
from vetinari.agents.handlers import BaseHandler
from vetinari.boundary_guards import require_nonempty
from vetinari.constants import TRUNCATE_CONTENT_ANALYSIS
from vetinari.security.redaction import redact_text, redact_value

logger = logging.getLogger(__name__)


_DEFAULT_QUALITY_SCORE = 1.0


def _pricing_float(pricing: Any, key: str) -> float:
    return float(pricing[key])


def _pricing_tier(pricing: Any) -> str:
    return str(pricing["tier"])


def _safe_prompt_text(value: str) -> str:
    """Redact sensitive free text before sending it to model-backed helpers."""
    return redact_text(value)


def _quality_score(task_context: dict[str, Any], model_id: str) -> float:
    raw_scores = task_context.get("model_quality_scores") or task_context.get("quality_scores") or {}
    if isinstance(raw_scores, dict) and model_id in raw_scores:
        try:
            score = float(raw_scores[model_id])
        except (TypeError, ValueError):
            logger.warning("Invalid model quality score for %s; using zero quality", model_id)
            return 0.0
        return max(0.0, min(1.0, score))
    return _DEFAULT_QUALITY_SCORE


def _minimum_quality_score(task_context: dict[str, Any]) -> float:
    raw = task_context.get("min_quality_score", task_context.get("quality_threshold", 0.0))
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        logger.warning("Invalid minimum quality score; using zero threshold")
        return 0.0


# Model pricing table -- immutable mapping to prevent accidental mutation.
# Keys are model identifiers; values contain per-1k-token costs and tier.
MODEL_PRICING = types.MappingProxyType({
    "qwen2.5-coder-7b": types.MappingProxyType({"input_per_1k": 0.0001, "output_per_1k": 0.0002, "tier": "small"}),
    "qwen2.5-72b": types.MappingProxyType({"input_per_1k": 0.001, "output_per_1k": 0.002, "tier": "large"}),
    "qwen3-30b-a3b": types.MappingProxyType({"input_per_1k": 0.0005, "output_per_1k": 0.001, "tier": "medium"}),
    "qwen2.5-vl-32b": types.MappingProxyType({"input_per_1k": 0.0005, "output_per_1k": 0.001, "tier": "medium"}),
    "claude-opus-4-8": types.MappingProxyType({"input_per_1k": 0.005, "output_per_1k": 0.025, "tier": "premium"}),
    "claude-opus-4-7": types.MappingProxyType({"input_per_1k": 0.005, "output_per_1k": 0.025, "tier": "premium"}),
    "claude-sonnet-4-6": types.MappingProxyType({"input_per_1k": 0.003, "output_per_1k": 0.015, "tier": "premium"}),
    "claude-haiku-4-5-20251001": types.MappingProxyType({
        "input_per_1k": 0.001,
        "output_per_1k": 0.005,
        "tier": "premium",
    }),
    "gpt-4o": types.MappingProxyType({"input_per_1k": 0.005, "output_per_1k": 0.015, "tier": "premium"}),
    "gemini-3.5-flash": types.MappingProxyType({"input_per_1k": 0.0015, "output_per_1k": 0.009, "tier": "large"}),
    "gemini-3.1-flash-lite": types.MappingProxyType({
        "input_per_1k": 0.00025,
        "output_per_1k": 0.0015,
        "tier": "small",
    }),
})


def compute_cost(*, model_id: str, tokens: int) -> dict[str, Any]:
    """Compute token cost for a known model, failing closed for unknown IDs.

    Returns:
        Cost breakdown for input, output, and total token cost.

    Raises:
        ValueError: If the model is unknown or token count is not positive.
    """
    model = require_nonempty(model_id, field_name="model_id")
    if model not in MODEL_PRICING:
        raise ValueError(f"unknown model_id: {model}")
    if tokens <= 0:
        raise ValueError("tokens must be positive")
    pricing = MODEL_PRICING[model]
    input_cost = (tokens / 1000) * _pricing_float(pricing, "input_per_1k")
    output_cost = (tokens / 1000) * _pricing_float(pricing, "output_per_1k")
    return {
        "model": model,
        "tokens": tokens,
        "input_cost": round(input_cost, 6),
        "output_cost": round(output_cost, 6),
        "total_cost": round(input_cost + output_cost, 6),
    }


class CostAnalysisHandler(BaseHandler):
    """Handler for the 'cost_analysis' mode of OperationsAgent.

    Performs model cost comparisons using a static pricing table, and falls
    back to LLM-assisted general cost analysis when the analysis type is not
    a direct model comparison.
    """

    def __init__(self) -> None:
        super().__init__(
            mode_name="cost_analysis",
            description="Calculate token costs, compare models, recommend cost-efficient selections",
        )

    def get_system_prompt(self) -> str:
        """Return the cost-analyst system prompt.

        Returns:
            A multi-section prompt defining the cost analyst's responsibilities,
            analysis framework, optimisation strategies, and output format.
        """
        return (
            "You are Vetinari's Cost Analyst -- an expert in AI/ML economics, token pricing,\n"
            "and infrastructure cost optimisation. You help teams make data-driven decisions\n"
            "about model selection, deployment strategy, and resource allocation.\n\n"
            "## Core Responsibilities\n"
            "- Calculate per-task token costs across all available models and providers\n"
            "- Compare local inference vs cloud API costs with full TCO analysis\n"
            "- Recommend cost-efficient model selections that meet quality thresholds\n"
            "- Forecast monthly/quarterly spending based on usage patterns\n"
            "- Identify cost anomalies and optimisation opportunities\n\n"
            "## Analysis Framework\n"
            "- Always include: input token cost, output token cost, latency, quality score\n"
            "- Calculate cost-per-quality-point ($/quality) for meaningful comparisons\n"
            "- Factor in hidden costs: retry overhead, escalation chains, batch vs real-time\n"
            "- Compare tiers: small (7B, <$0.001/1k), medium (30B, ~$0.001/1k),\n"
            "  large (72B, ~$0.002/1k), premium (cloud APIs, $0.003-0.015/1k)\n"
            "- Local models: amortised hardware cost = $0 per token after purchase\n\n"
            "## Optimisation Strategies\n"
            "- Cascade routing: start cheap, escalate only on low confidence (saves 40-60%)\n"
            "- Batch processing: queue non-urgent work for 50% API discount\n"
            "- Prompt caching: reuse system prompts to cut input costs (Anthropic: 90% reduction)\n"
            "- Token budgeting: set per-task max_tokens to prevent runaway costs\n"
            "- SLM preprocessing: use small models for classification/routing before expensive inference\n\n"
            "## Output Format\n"
            "Return JSON with 'comparisons' (array of model cost breakdowns), 'recommendation'\n"
            "(cheapest adequate model), 'estimated_savings', and 'forecast' when applicable.\n"
            "Always include concrete dollar amounts, not just relative comparisons."
        )

    def handle(self, *, model_id: str, tokens: int) -> dict[str, Any]:
        """Compatibility handler used by validation probes."""
        return compute_cost(model_id=model_id, tokens=tokens)

    def execute(self, task: AgentTask, context: dict[str, Any]) -> AgentResult:
        """Perform cost analysis for the given task.

        Supports two analysis types:
        - 'model_comparison': deterministic cost calculation across specified
          models using the static pricing table.
        - Any other type: heuristic token estimation followed by LLM-assisted
          analysis via the 'infer_json' callable in the execution context.

        Args:
            task: The agent task containing the cost analysis request.
            context: Execution context; should contain an 'infer_json' callable
                with signature ``(prompt: str, fallback: Any) -> dict`` for
                non-comparison analysis types.

        Returns:
            An AgentResult with cost comparisons, recommendations, and
            estimated savings in the output field.
        """
        task_context = task.context or {}
        analysis_type = task_context.get("analysis_type", "model_comparison")

        if analysis_type == "model_comparison":
            return self._execute_model_comparison(task_context)

        return self._execute_general_analysis(task, context)

    @staticmethod
    def _execute_model_comparison(task_context: dict[str, Any]) -> AgentResult:
        """Run a deterministic model cost comparison.

        Args:
            task_context: Task context containing optional 'models' list and
                'estimated_tokens' count.

        Returns:
            An AgentResult with sorted cost comparisons and the cheapest model
            as the recommendation.
        """
        models = task_context.get("models", list(MODEL_PRICING.keys()))
        estimated_tokens = task_context.get("estimated_tokens", 10000)

        min_quality_score = _minimum_quality_score(task_context)
        comparisons = []
        for model_id in models:
            if model_id not in MODEL_PRICING:
                raise ValueError(f"unknown model_id: {model_id}")
            pricing = MODEL_PRICING[model_id]
            input_cost = (estimated_tokens / 1000) * _pricing_float(pricing, "input_per_1k")
            output_cost = (estimated_tokens / 1000) * _pricing_float(pricing, "output_per_1k")
            total_cost = round(input_cost + output_cost, 4)
            quality_score = _quality_score(task_context, model_id)
            quality_fit = quality_score >= min_quality_score
            comparisons.append(
                {
                    "model": model_id,
                    "tier": _pricing_tier(pricing),
                    "input_cost": round(input_cost, 4),
                    "output_cost": round(output_cost, 4),
                    "total_cost": total_cost,
                    "quality_score": quality_score,
                    "quality_threshold": min_quality_score,
                    "quality_fit": quality_fit,
                    "cost_per_quality_point": round(total_cost / max(quality_score, 0.001), 6),
                },
            )
        comparisons.sort(key=lambda c: (not c["quality_fit"], c["cost_per_quality_point"], c["total_cost"]))
        adequate = [comparison for comparison in comparisons if comparison["quality_fit"]]
        recommendation = adequate[0]["model"] if adequate else "blocked_no_quality_fit"

        return AgentResult(
            success=True,
            output={
                "comparisons": comparisons,
                "recommendation": recommendation if comparisons else "unknown",
                "estimated_tokens": estimated_tokens,
                "cheapest": min(comparisons, key=lambda c: c["total_cost"]) if comparisons else None,
                "cheapest_adequate": adequate[0] if adequate else None,
                "most_expensive": comparisons[-1] if comparisons else None,
            },
            metadata={"mode": "cost_analysis", "analysis_type": "model_comparison"},
        )

    def _execute_general_analysis(self, task: AgentTask, context: dict[str, Any]) -> AgentResult:
        """Run a heuristic + LLM-assisted general cost analysis.

        Args:
            task: The agent task with a description to analyse.
            context: Execution context with optional 'infer_json' callable.

        Returns:
            An AgentResult with token estimates, model recommendations, and
            savings guidance.
        """
        description = task.description or ""
        safe_description = _safe_prompt_text(description)
        word_count = len(description.split())
        estimated_tokens = int(word_count * 1.3)  # rough word-to-token ratio

        recommendations = []
        for model_id, pricing in sorted(
            MODEL_PRICING.items(),
            key=lambda x: _pricing_float(x[1], "input_per_1k"),
        ):
            cost = (estimated_tokens / 1000) * (
                _pricing_float(pricing, "input_per_1k") + _pricing_float(pricing, "output_per_1k")
            )
            recommendations.append(
                {
                    "model": model_id,
                    "tier": _pricing_tier(pricing),
                    "estimated_cost": round(cost, 6),
                },
            )

        fallback: dict[str, Any] = {
            "analysis": f"Estimated {estimated_tokens} tokens based on {word_count} words",
            "recommendations": recommendations[:3],
            "estimated_savings": "Use local models for 10-100x cost reduction vs cloud APIs",
        }

        prompt = (
            f"Perform cost analysis for:\n{safe_description[:TRUNCATE_CONTENT_ANALYSIS]}\n\n"
            "Respond as JSON:\n"
            '{"analysis": "...", "recommendations": [...], "estimated_savings": "..."}'
        )

        infer_json = context.get("infer_json")
        if infer_json is not None:
            result = infer_json(prompt, fallback=fallback)
            return AgentResult(
                success=True,
                output=redact_value(result),
                metadata={"mode": "cost_analysis"},
            )
        else:
            self._logger.warning("No infer_json callable in context, using fallback")
            return AgentResult(
                success=False,
                output=fallback,
                metadata={"mode": "cost_analysis", "_is_fallback": True},
            )
