"""Lazy optional dependency loaders for agent inference."""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)


_circuit_breaker_registry: Any | None = None
_circuit_breaker_available = True

_local_preprocessor_cls: Any | None = None
_local_preprocessor_available = True

_PROMPT_EVOLVER_ENABLED = os.environ.get("PROMPT_EVOLVER_ENABLED", "true").lower() not in ("false", "0", "no")
_LOCAL_ONLY_MODE = os.environ.get("LOCAL_ONLY_MODE", "false").lower() in ("true", "1", "yes")

_adapter_manager_fn: Any | None = None
_prompt_evolver_fn: Any | None = None
_prompt_evolver_available = True
_prompt_assembler_fn: Any | None = None
_prompt_assembler_available = True
_inference_config_fn: Any | None = None
_inference_config_available = True
_token_optimizer_fn: Any | None = None
_token_optimizer_available = True
_batch_processor_fn: Any | None = None
_batch_processor_available = True
_semantic_cache_fn: Any | None = None
_semantic_cache_available = True
_thompson_strategy_fn: Any | None = None
_thompson_strategy_available = True


def _module_is_available(module_name: str) -> bool:
    """Return True when a lazy dependency can be discovered without importing it."""
    if module_name in sys.modules:
        return sys.modules[module_name] is not None
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError):
        logger.warning("Exception handled by  module is available fallback", exc_info=True)
        return False


def _get_circuit_breaker_registry() -> Any | None:
    """Return the circuit breaker registry, or None if unavailable."""
    global _circuit_breaker_registry, _circuit_breaker_available
    if not _circuit_breaker_available:
        return None
    if _circuit_breaker_registry is not None:
        return _circuit_breaker_registry
    if not _module_is_available("vetinari.resilience"):
        logger.warning("vetinari.resilience not available - circuit breaker protection disabled")
        _circuit_breaker_available = False
        return None
    try:
        from vetinari.resilience import get_circuit_breaker_registry

        _circuit_breaker_registry = get_circuit_breaker_registry()
        return _circuit_breaker_registry
    except ModuleNotFoundError:
        logger.warning("vetinari.resilience not available - circuit breaker protection disabled")
        _circuit_breaker_available = False
        return None
    except Exception as exc:
        logger.warning("Circuit breaker registry init failed: %s", exc)
        return None


def _get_local_preprocessor_cls() -> Any | None:
    """Return LocalPreprocessor class, or None when unavailable."""
    global _local_preprocessor_cls, _local_preprocessor_available
    if not _local_preprocessor_available:
        return None
    if _local_preprocessor_cls is not None:
        return _local_preprocessor_cls
    if not _module_is_available("vetinari.token_compression"):
        logger.warning("vetinari.token_compression not available - prompt compression disabled")
        _local_preprocessor_available = False
        return None
    try:
        from vetinari.token_compression import LocalPreprocessor

        _local_preprocessor_cls = LocalPreprocessor
        return _local_preprocessor_cls
    except ModuleNotFoundError:
        logger.warning("vetinari.token_compression not available - prompt compression disabled")
        _local_preprocessor_available = False
        return None


def _lazy_get_adapter_manager() -> Any | None:
    """Cached lazy loader for get_adapter_manager."""
    global _adapter_manager_fn
    if _adapter_manager_fn is not None:
        return _adapter_manager_fn()
    try:
        from vetinari.adapter_manager import get_adapter_manager

        _adapter_manager_fn = get_adapter_manager
        return get_adapter_manager()
    except (ImportError, AttributeError):
        logger.warning("vetinari.adapter_manager not available - inference will have no adapter backend")
        return None


def _lazy_get_prompt_evolver() -> Any | None:
    """Cached lazy loader for PromptEvolver."""
    global _prompt_evolver_fn, _prompt_evolver_available
    if not _prompt_evolver_available:
        return None
    if _prompt_evolver_fn is not None:
        return _prompt_evolver_fn()
    if not _module_is_available("vetinari.learning.prompt_evolver"):
        logger.warning("vetinari.learning.prompt_evolver not available - prompt evolution disabled")
        _prompt_evolver_available = False
        return None
    try:
        from vetinari.learning.prompt_evolver import get_prompt_evolver

        _prompt_evolver_fn = get_prompt_evolver
        return get_prompt_evolver()
    except ModuleNotFoundError:
        logger.warning("vetinari.learning.prompt_evolver not available - prompt evolution disabled")
        _prompt_evolver_available = False
        return None


def _lazy_get_prompt_assembler() -> Any | None:
    """Cached lazy loader for PromptAssembler."""
    global _prompt_assembler_fn, _prompt_assembler_available
    if not _prompt_assembler_available:
        return None
    if _prompt_assembler_fn is not None:
        return _prompt_assembler_fn()
    if not _module_is_available("vetinari.prompts"):
        logger.warning("vetinari.prompts not available - prompt assembly disabled, using raw prompts")
        _prompt_assembler_available = False
        return None
    try:
        from vetinari.prompts import get_prompt_assembler

        _prompt_assembler_fn = get_prompt_assembler
        return get_prompt_assembler()
    except ModuleNotFoundError:
        logger.warning("vetinari.prompts not available - prompt assembly disabled, using raw prompts")
        _prompt_assembler_available = False
        return None


def _lazy_get_inference_config() -> Any | None:
    """Cached lazy loader for InferenceConfigManager."""
    global _inference_config_fn, _inference_config_available
    if not _inference_config_available:
        return None
    if _inference_config_fn is not None:
        return _inference_config_fn()
    if not _module_is_available("vetinari.config.inference_config"):
        logger.warning("vetinari.config.inference_config not available - using hardcoded inference defaults")
        _inference_config_available = False
        return None
    try:
        from vetinari.config.inference_config import get_inference_config

        _inference_config_fn = get_inference_config
        return get_inference_config()
    except ModuleNotFoundError:
        logger.warning("vetinari.config.inference_config not available - using hardcoded inference defaults")
        _inference_config_available = False
        return None


def _lazy_get_token_optimizer() -> Any | None:
    """Cached lazy loader for TokenOptimizer."""
    global _token_optimizer_fn, _token_optimizer_available
    if not _token_optimizer_available:
        return None
    if _token_optimizer_fn is not None:
        return _token_optimizer_fn()
    if not _module_is_available("vetinari.token_optimizer"):
        logger.warning("vetinari.token_optimizer not available - token budget optimization disabled")
        _token_optimizer_available = False
        return None
    try:
        from vetinari.token_optimizer import get_token_optimizer

        _token_optimizer_fn = get_token_optimizer
        return get_token_optimizer()
    except ModuleNotFoundError:
        logger.warning("vetinari.token_optimizer not available - token budget optimization disabled")
        _token_optimizer_available = False
        return None


def _lazy_get_batch_processor() -> Any | None:
    """Cached lazy loader for BatchProcessor."""
    global _batch_processor_fn, _batch_processor_available
    if not _batch_processor_available:
        return None
    if _batch_processor_fn is not None:
        return _batch_processor_fn()
    if not _module_is_available("vetinari.adapters.batch_processor"):
        logger.warning("vetinari.adapters.batch_processor not available - batch inference disabled")
        _batch_processor_available = False
        return None
    try:
        from vetinari.adapters.batch_processor import get_batch_processor

        _batch_processor_fn = get_batch_processor
        return get_batch_processor()
    except ModuleNotFoundError:
        logger.warning("vetinari.adapters.batch_processor not available - batch inference disabled")
        _batch_processor_available = False
        return None


def _lazy_get_thompson_strategy() -> Any | None:
    """Cached lazy loader for Thompson temperature strategy selection."""
    global _thompson_strategy_fn, _thompson_strategy_available
    if not _thompson_strategy_available:
        return None
    if _thompson_strategy_fn is not None:
        return _thompson_strategy_fn()
    if not _module_is_available("vetinari.learning.model_selector") or not _module_is_available(
        "vetinari.learning.thompson_selectors"
    ):
        logger.warning("Thompson strategy selection is unavailable - using configured temperatures only")
        _thompson_strategy_available = False
        return None
    try:
        from vetinari.learning.model_selector import get_thompson_selector
        from vetinari.learning.thompson_selectors import select_strategy

        def _get_pair() -> tuple[Any, Any]:
            return get_thompson_selector(), select_strategy

        _thompson_strategy_fn = _get_pair
        return _get_pair()
    except ModuleNotFoundError:
        logger.warning("Thompson strategy selection is unavailable - using configured temperatures only")
        _thompson_strategy_available = False
        return None


def _lazy_get_semantic_cache() -> Any | None:
    """Cached lazy loader for SemanticCache singleton."""
    global _semantic_cache_fn, _semantic_cache_available
    if not _semantic_cache_available:
        return None
    if _semantic_cache_fn is not None:
        return _semantic_cache_fn()
    if not _module_is_available("vetinari.optimization.semantic_cache"):
        logger.warning("vetinari.optimization.semantic_cache not available - semantic caching disabled")
        _semantic_cache_available = False
        return None
    try:
        from vetinari.optimization.semantic_cache import get_semantic_cache

        _semantic_cache_fn = get_semantic_cache
        return get_semantic_cache()
    except ModuleNotFoundError:
        logger.warning("vetinari.optimization.semantic_cache not available - semantic caching disabled")
        _semantic_cache_available = False
        return None
    except Exception:
        logger.warning("SemanticCache init failed - semantic caching disabled, proceeding with direct inference")
        _semantic_cache_available = False
        return None
