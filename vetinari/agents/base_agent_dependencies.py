"""Lazy dependency loaders for :mod:`vetinari.agents.base_agent`."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_cached_current_context = None
_cached_execution_context_mod = None
_cached_genai_tracer_fn = None
_cached_security_error_cls = None
_cached_guardrails_mod = None
_cached_skill_contract_mod = None
_cached_meta_adapter_mod = None
_cached_quality_scorer_fn = None
_cached_feedback_loop_fn = None
_cached_constraint_registry_fn = None
_cached_prompt_evolver_fn = None
_cached_training_collector_fn = None
_cached_episode_memory_fn = None
_cached_structured_logging_fn = None
_cached_execute_safely_fn = None
_cached_complete_task_fn = None
_cached_practices_fn = None
_cached_standards_loader_fn = None
_cached_rules_manager_fn = None
_cached_knowledge_base_fn = None


def _get_current_context():
    """Return the current_context callable, importing once on first call."""
    global _cached_current_context
    if _cached_current_context is None:
        from vetinari.execution_context import current_context

        _cached_current_context = current_context
    return _cached_current_context


def _get_execution_context_mod():
    """Return the execution-context module, importing once on first call."""
    global _cached_execution_context_mod
    if _cached_execution_context_mod is None:
        import vetinari.execution_context as _ec_mod

        _cached_execution_context_mod = _ec_mod
    return _cached_execution_context_mod


def _get_genai_tracer():
    """Return get_genai_tracer callable, importing once on first call."""
    global _cached_genai_tracer_fn
    if _cached_genai_tracer_fn is None:
        from vetinari.observability.otel_genai import get_genai_tracer

        _cached_genai_tracer_fn = get_genai_tracer
    return _cached_genai_tracer_fn


def _get_security_error_cls():
    """Return the SecurityError exception class, importing once on first call."""
    global _cached_security_error_cls
    if _cached_security_error_cls is None:
        from vetinari.exceptions import SecurityError

        _cached_security_error_cls = SecurityError
    return _cached_security_error_cls


def _get_guardrails_mod():
    """Return the guardrails module, importing once on first call."""
    global _cached_guardrails_mod
    if _cached_guardrails_mod is None:
        import vetinari.safety.guardrails as _gr_mod

        _cached_guardrails_mod = _gr_mod
    return _cached_guardrails_mod


def _get_skill_contract_mod():
    """Return the skill-contract module, importing once on first call."""
    global _cached_skill_contract_mod
    if _cached_skill_contract_mod is None:
        import vetinari.agents.skill_contract as _sc_mod

        _cached_skill_contract_mod = _sc_mod
    return _cached_skill_contract_mod


def _get_meta_adapter_mod():
    """Return the meta-adapter module, importing once on first call."""
    global _cached_meta_adapter_mod
    if _cached_meta_adapter_mod is None:
        import vetinari.learning.meta_adapter as _ma_mod

        _cached_meta_adapter_mod = _ma_mod
    return _cached_meta_adapter_mod


def _get_quality_scorer():
    """Return the get_quality_scorer callable, importing once on first call."""
    global _cached_quality_scorer_fn
    if _cached_quality_scorer_fn is None:
        from vetinari.learning.quality_scorer import get_quality_scorer

        _cached_quality_scorer_fn = get_quality_scorer
    return _cached_quality_scorer_fn


def _get_feedback_loop():
    """Return the get_feedback_loop callable, importing once on first call."""
    global _cached_feedback_loop_fn
    if _cached_feedback_loop_fn is None:
        from vetinari.learning.feedback_loop import get_feedback_loop

        _cached_feedback_loop_fn = get_feedback_loop
    return _cached_feedback_loop_fn


def _get_constraint_registry():
    """Return the get_constraint_registry callable, importing once on first call."""
    global _cached_constraint_registry_fn
    if _cached_constraint_registry_fn is None:
        from vetinari.constraints.registry import get_constraint_registry

        _cached_constraint_registry_fn = get_constraint_registry
    return _cached_constraint_registry_fn


def _get_prompt_evolver():
    """Return the get_prompt_evolver callable, importing once on first call."""
    global _cached_prompt_evolver_fn
    if _cached_prompt_evolver_fn is None:
        from vetinari.learning.prompt_evolver import get_prompt_evolver

        _cached_prompt_evolver_fn = get_prompt_evolver
    return _cached_prompt_evolver_fn


def _get_training_collector():
    """Return the get_training_collector callable, importing once on first call."""
    global _cached_training_collector_fn
    if _cached_training_collector_fn is None:
        from vetinari.learning.training_data import get_training_collector

        _cached_training_collector_fn = get_training_collector
    return _cached_training_collector_fn


def _get_episode_memory():
    """Return the get_episode_memory callable, importing once on first call."""
    global _cached_episode_memory_fn
    if _cached_episode_memory_fn is None:
        from vetinari.learning.episode_memory import get_episode_memory

        _cached_episode_memory_fn = get_episode_memory
    return _cached_episode_memory_fn


def _get_log_event():
    """Return the structured log_event callable, importing once on first call."""
    global _cached_structured_logging_fn
    if _cached_structured_logging_fn is None:
        from vetinari.structured_logging import log_event

        _cached_structured_logging_fn = log_event
    return _cached_structured_logging_fn


def _get_execute_safely_fn():
    """Return the execute_safely callable, importing once on first call."""
    global _cached_execute_safely_fn
    if _cached_execute_safely_fn is None:
        from vetinari.agents.base_agent_execution import execute_safely

        _cached_execute_safely_fn = execute_safely
    return _cached_execute_safely_fn


def _get_complete_task_fn():
    """Return the complete_task callable, importing once on first call."""
    global _cached_complete_task_fn
    if _cached_complete_task_fn is None:
        from vetinari.agents.base_agent_completion import complete_task

        _cached_complete_task_fn = complete_task
    return _cached_complete_task_fn


def _get_practices_for_mode():
    """Return the get_practices_for_mode callable, importing once on first call."""
    global _cached_practices_fn
    if _cached_practices_fn is None:
        from vetinari.agents.practices import get_practices_for_mode

        _cached_practices_fn = get_practices_for_mode
    return _cached_practices_fn


def _get_standards_loader():
    """Return the get_standards_loader callable, importing once on first call."""
    global _cached_standards_loader_fn
    if _cached_standards_loader_fn is None:
        from vetinari.config.standards_loader import get_standards_loader

        _cached_standards_loader_fn = get_standards_loader
    return _cached_standards_loader_fn


def _get_rules_manager():
    """Return the get_rules_manager callable, importing once on first call."""
    global _cached_rules_manager_fn
    if _cached_rules_manager_fn is None:
        from vetinari.rules_manager import get_rules_manager

        _cached_rules_manager_fn = get_rules_manager
    return _cached_rules_manager_fn


def _get_knowledge_base():
    """Return the get_knowledge_base callable, importing once on first call."""
    global _cached_knowledge_base_fn
    if _cached_knowledge_base_fn is None:
        from vetinari.rag import get_knowledge_base

        _cached_knowledge_base_fn = get_knowledge_base
    return _cached_knowledge_base_fn


def _get_agent_constraints(agent_type_value: str, mode: str | None = None):
    """Load constraints for an agent via the cached registry getter."""
    try:
        return _get_constraint_registry()().get_constraints_for_agent(
            agent_type_value,
            mode=mode,
        )
    except Exception as exc:
        logger.warning("Constraint registry unavailable for agent %s: %s", agent_type_value, exc)
        return None
