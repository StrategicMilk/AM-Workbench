"""Prompt-shaping helpers extracted from inference_steps.py.

This module contains the eight helpers responsible for building and adjusting
the active system prompt and inference parameters before a request is dispatched
to an adapter. They are imported back into ``vetinari.agents.inference_steps``
so all existing call sites continue to resolve.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from vetinari.exceptions import InferenceError
from vetinari.guardrails.prompt_security import scan_prompt_security
from vetinari.safety.guardrails import redact_pii
from vetinari.safety.prompt_sanitizer import sanitize_task_description

if TYPE_CHECKING:
    from vetinari.agents.inference_steps import _InferenceCallState

logger = logging.getLogger(__name__)
_DEFAULT_MAX_TOKENS = 4096
_DEFAULT_TEMPERATURE = 0.3


def _sanitize_recalled_prompt_text(prompt_text: str) -> str:
    """Scrub recalled memory text before it is appended to an inference prompt."""
    redacted = redact_pii(prompt_text)
    if scan_prompt_security(redacted):
        return sanitize_task_description(redacted)
    return redacted


def _prepare_active_system_prompt(state: _InferenceCallState) -> None:
    """Build the model-size-aware active system prompt."""
    agent_system = state.system_prompt or state.agent.get_system_prompt()
    resolved_model_id = state.model_id or state.agent.default_model
    state.active_system_prompt = state.agent._get_prompt_tier(model_id=resolved_model_id) + "\n\n" + agent_system


def _apply_prompt_evolver(state: _InferenceCallState) -> None:
    """Apply optional prompt-evolver selection."""
    evolver = state.seams._lazy_get_prompt_evolver() if state.seams._PROMPT_EVOLVER_ENABLED else None
    if evolver is None:
        return
    try:
        evolved_prompt, state.variant_id = evolver.select_prompt(state.agent.agent_type.value)
        if evolved_prompt and evolved_prompt != state.active_system_prompt:
            state.active_system_prompt = evolved_prompt
    except Exception:
        logger.warning("Failed to select evolved prompt for agent %s", state.agent.agent_type.value, exc_info=True)


def _apply_prompt_assembler(state: _InferenceCallState) -> None:
    """Apply optional prompt assembler instructions and memory metadata."""
    assembler = state.seams._lazy_get_prompt_assembler()
    if assembler is None:
        return
    try:
        task_type = getattr(state.agent, "_current_task_type", None) or "general"
        assembled = assembler.build(
            agent_type=state.agent.agent_type.value,
            task_type=task_type,
            task_description=state.prompt,
            mode=getattr(state.agent, "_current_mode", None),
            context_budget=getattr(state.agent, "_context_budget", 28000),
        )
        if assembled.get("system"):
            state.active_system_prompt = assembled["system"]
            if isinstance(assembled.get("memory_recall"), dict):
                state.memory_recall_metadata = assembled["memory_recall"]
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "PromptAssembler built %d-char prompt for %s/%s (cache_hit=%s)",
                    assembled.get("total_chars", 0),
                    state.agent.agent_type.value,
                    task_type,
                    assembled.get("cache_hit", False),
                )
    except Exception:
        logger.warning("PromptAssembler unavailable for %s", state.agent.agent_type.value, exc_info=True)


def _inject_memory_recall(state: _InferenceCallState) -> None:
    """Append deterministic memory recall context to the prompt."""
    prior_memories = getattr(state.agent, "_current_task_memories", None)
    if not prior_memories:
        return
    try:
        memory_pack = state.seams.build_memory_recall_pack(
            agent_type=state.agent.agent_type.value,
            task_type=getattr(state.agent, "_current_task_type", None) or "general",
            query=state.prompt,
            prior_memories=prior_memories,
        )
        state.memory_recall_metadata = memory_pack.to_dict()
        if memory_pack.prompt_text:
            state.prompt = state.prompt + "\n\n" + _sanitize_recalled_prompt_text(memory_pack.prompt_text)
    except Exception as exc:
        logger.warning(
            "Memory recall packing failed for %s; memory content omitted: %s",
            state.agent.agent_type.value,
            exc,
            exc_info=True,
        )


def _apply_token_optimizer_profile(state: _InferenceCallState) -> None:
    """Apply token optimizer defaults when config parameters are unavailable."""
    optimizer = state.seams._lazy_get_token_optimizer()
    if optimizer is None:
        if state.max_tokens is None:
            state.max_tokens = _DEFAULT_MAX_TOKENS
        if state.temperature is None:
            state.temperature = _DEFAULT_TEMPERATURE
        return
    try:
        profile_max_tokens, profile_temp, _ = optimizer.get_task_profile(state.agent_task_key)
        if state.max_tokens is None:
            state.max_tokens = profile_max_tokens
        if state.temperature is None:
            state.temperature = profile_temp
    except (KeyError, ValueError):
        logger.warning("Token optimizer profile not available for %s - using defaults", state.agent.agent_type.value)
    finally:
        if state.max_tokens is None:
            state.max_tokens = _DEFAULT_MAX_TOKENS
        if state.temperature is None:
            state.temperature = _DEFAULT_TEMPERATURE


def _apply_inference_config(state: _InferenceCallState) -> None:
    """Apply task-specific inference parameters from external config."""
    config = state.seams._lazy_get_inference_config()
    model_for_config = state.model_id or state.agent.default_model
    if config is None:
        _apply_token_optimizer_profile(state)
        return
    try:
        effective = config.get_effective_params(state.agent_task_key, model_for_config)
        if state.max_tokens is None:
            state.max_tokens = effective.get("max_tokens", state.max_tokens)
        if state.temperature is None:
            state.temperature = effective.get("temperature", state.temperature)
    except (KeyError, ValueError):
        _apply_token_optimizer_profile(state)
    finally:
        if state.max_tokens is None:
            state.max_tokens = _DEFAULT_MAX_TOKENS
        if state.temperature is None:
            state.temperature = _DEFAULT_TEMPERATURE


def _apply_thompson_temperature(state: _InferenceCallState) -> None:
    """Prefer learned Thompson temperatures when available."""
    try:
        thompson_pair = state.seams._lazy_get_thompson_strategy()
    except Exception:
        logger.warning("Thompson strategy unavailable; keeping config-based temperature", exc_info=True)
        return
    if thompson_pair is None:
        return
    try:
        thompson_strategy, _select_strategy_fn = thompson_pair
        agent_key = (
            state.agent.agent_type.value if hasattr(state.agent.agent_type, "value") else str(state.agent.agent_type)
        )
        mode_key = getattr(state.agent, "mode", "default")
        thompson_temp = thompson_strategy.select_strategy(agent_key, mode_key, "temperature")
        if isinstance(thompson_temp, (int, float)):
            state.temperature = float(thompson_temp)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[Thompson] Selected temperature %.2f for %s/%s", state.temperature, agent_key, mode_key)
    except Exception:
        logger.warning("Thompson temperature selection failed - keeping config-based value", exc_info=True)


def _enforce_prompt_budget(state: _InferenceCallState) -> None:
    """Compress or reject prompts that exceed the context window."""
    try:
        context_length = getattr(state.agent, "_context_length", None) or 8192
        budget = state.seams.check_prompt_budget(
            state.active_system_prompt, state.prompt, state.max_tokens, context_length
        )
        if budget["fits"]:
            return
        preprocessor_cls = state.seams._get_local_preprocessor_cls()
        if preprocessor_cls is None:
            raise InferenceError(
                f"Prompt for {state.agent.agent_type.value} exceeds context window "
                f"({budget['total_tokens']} tokens > {context_length} n_ctx) - reduce prompt size"
            )
        compressed = preprocessor_cls().compress(state.prompt)
        budget2 = state.seams.check_prompt_budget(
            state.active_system_prompt, compressed, state.max_tokens, context_length
        )
        if budget2["fits"]:
            state.prompt = compressed
            logger.warning(
                "Prompt for %s compressed %d->%d tokens to fit n_ctx=%d",
                state.agent.agent_type.value,
                budget["task_tokens"],
                budget2["task_tokens"],
                context_length,
            )
            return
        raise InferenceError(
            f"Prompt for {state.agent.agent_type.value} exceeds context window "
            f"({budget['total_tokens']} tokens > {context_length} n_ctx) even after compression "
            f"({budget2['total_tokens']} tokens) - reduce prompt size"
        )
    except InferenceError:
        raise
    except Exception as exc:
        raise InferenceError(
            f"Prompt budget check failed for {state.agent.agent_type.value} - cannot verify context window fit, "
            "aborting inference to prevent silent truncation"
        ) from exc
