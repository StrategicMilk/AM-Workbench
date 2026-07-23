"""LLM inference behavior for Vetinari agents."""

from __future__ import annotations

import logging
import sys
from typing import Any

from vetinari.adapters import adapter_cache as _adapter_cache
from vetinari.adapters.base import InferenceRequest
from vetinari.agents import inference_dependencies as _inference_dependencies
from vetinari.agents import inference_json as _inference_json
from vetinari.agents import prompt_loader as _prompt_loader
from vetinari.agents.inference_steps import _infer_steps
from vetinari.agents.observability import _ObservabilitySpan
from vetinari.analytics.cost import get_cost_tracker
from vetinari.exceptions import InferenceError, ModelUnavailableError
from vetinari.prompting import memory_packer as _memory_packer

logger = logging.getLogger(__name__)

__all__ = ["InferenceBehavior", "_ObservabilitySpan"]


_JSON_EXTRACT_RE = _inference_json._JSON_EXTRACT_RE
_MAX_JSON_RETRIES = _inference_json._MAX_JSON_RETRIES
_json_retry_counts = _inference_json._json_retry_counts
_json_retry_lock = _inference_json._json_retry_lock
get_json_retry_stats = _inference_json.get_json_retry_stats
get_local_inference_adapter = _adapter_cache.get_local_inference_adapter
check_prompt_budget = _prompt_loader.check_prompt_budget
build_memory_recall_pack = _memory_packer.build_memory_recall_pack

_get_circuit_breaker_registry = _inference_dependencies._get_circuit_breaker_registry
_get_local_preprocessor_cls = _inference_dependencies._get_local_preprocessor_cls
_PROMPT_EVOLVER_ENABLED = _inference_dependencies._PROMPT_EVOLVER_ENABLED
_LOCAL_ONLY_MODE = _inference_dependencies._LOCAL_ONLY_MODE
_lazy_get_adapter_manager = _inference_dependencies._lazy_get_adapter_manager
_lazy_get_prompt_evolver = _inference_dependencies._lazy_get_prompt_evolver
_lazy_get_prompt_assembler = _inference_dependencies._lazy_get_prompt_assembler
_lazy_get_inference_config = _inference_dependencies._lazy_get_inference_config
_lazy_get_token_optimizer = _inference_dependencies._lazy_get_token_optimizer
_lazy_get_batch_processor = _inference_dependencies._lazy_get_batch_processor
_lazy_get_thompson_strategy = _inference_dependencies._lazy_get_thompson_strategy
_lazy_get_semantic_cache = _inference_dependencies._lazy_get_semantic_cache


def _infer_response_cost_usd(response: Any, request: InferenceRequest) -> float:
    """Return best available USD cost for a successful inference response."""
    metadata = getattr(response, "metadata", None) or {}
    explicit = metadata.get("cost_usd") or metadata.get("total_cost_usd")
    if explicit is not None:
        try:
            return max(0.0, float(explicit))
        except (TypeError, ValueError):
            logger.warning("Invalid explicit response cost for %s", request.model_id, exc_info=True)
            return 0.0

    try:
        input_tokens = getattr(response, "input_tokens", None)
        output_tokens = getattr(response, "output_tokens", None)
        if input_tokens is None or output_tokens is None:
            logger.warning("Inference response omitted exact token split for %s; recording zero cost", request.model_id)
            return 0.0
        input_tokens = max(0, int(input_tokens))
        output_tokens = max(0, int(output_tokens))
        provider = str(metadata.get("provider") or request.metadata.get("provider") or "local")
        pricing = get_cost_tracker().get_pricing(provider, request.model_id)
        return max(0.0, float(pricing.compute(input_tokens, output_tokens)))
    except Exception:
        logger.warning("Failed to calculate BudgetTracker cost for %s", request.model_id, exc_info=True)
        return 0.0


class InferenceBehavior(_inference_json._JsonInferenceBehavior):
    """LLM inference capabilities mixed into BaseAgent.

    All methods rely on ``self._adapter_manager``, ``self.agent_type``,
    ``self.default_model``, ``self.get_system_prompt()``, and ``self._log()``
    being present on the BaseAgent instance at runtime.
    """

    def _infer(
        self,
        prompt: str,
        system_prompt: str | None = None,
        model_id: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        expect_json: bool = False,
        use_cascade: bool = True,
    ) -> str:
        """Call an LLM via the AdapterManager and return the text output.

        Args:
            prompt: The user/task prompt.
            system_prompt: Optional system prompt override.
            model_id: Optional model override.
            max_tokens: Maximum tokens to generate. ``None`` means resolve from
                task/model policy, then fall back to the runtime default.
            temperature: Sampling temperature. ``None`` means resolve from
                task/model policy, then fall back to the runtime default.
            expect_json: Whether JSON output is required.
            use_cascade: Whether to route through cascade inference.

        Returns:
            The generated text string.

        Raises:
            ModelUnavailableError: If no inference adapter is available.
            InferenceError: If preflight or inference fails.
        """
        return _infer_steps(
            self,
            prompt,
            system_prompt,
            model_id,
            max_tokens,
            temperature,
            expect_json,
            use_cascade,
            sys.modules[__name__],
        )

    def _infer_with_fallback(
        self,
        prompt: str,
        fallback_fn: Any | None = None,
        required_keys: list[str] | None = None,
    ) -> Any | None:
        """Infer from LLM with optional fallback and key validation.

        Args:
            prompt: The prompt to send to the LLM.
            fallback_fn: Optional callable to run if LLM fails.
            required_keys: Optional list of keys the JSON response must contain.

        Returns:
            Parsed response dict, fallback result, or None. Fallback dicts are
            tagged with ``_is_fallback=True`` so training collectors and quality
            gates can reject degraded output.
        """
        try:
            response = self._infer_json(prompt)
            if response and required_keys:
                if all(key in response for key in required_keys):
                    return response
            elif response:
                return response
        except (InferenceError, ModelUnavailableError) as exc:
            logger.warning("[%s] LLM inference failed: %s", self.agent_type, exc)

        if fallback_fn:
            result = fallback_fn()
            if isinstance(result, dict):
                result = dict(result)
                result["_is_fallback"] = True
            return result
        return None
