"""Provider adapters for multi-LLM orchestration.

This module provides pluggable adapters for different LLM providers:
- LiteLLM (unified adapter for all cloud providers — ADR-0062)
- Local (llama-cpp-python, in-process GGUF inference)
- OpenAI-compatible servers: vLLM, NVIDIA NIMs (ADR-0084)

Grammar-constrained generation is available via ``grammar_library``:
- ``GRAMMAR_LIBRARY`` — pre-built GBNF grammars by name
- ``TASK_TYPE_TO_GRAMMAR`` — task type -> grammar name mapping
- ``get_grammar`` / ``get_grammar_for_task_type`` — lookup helpers
- ``validate_grammar`` — structural BNF validation
- ``truncate_at_grammar_boundary`` — BudgetForcing-inspired truncation
"""

from __future__ import annotations

from .adapter_cache import clear_adapter_cache, get_local_inference_adapter
from .base import InferenceRequest, InferenceResponse, ModelInfo, ProviderAdapter, ProviderConfig
from .cloud_adapter import CloudAdapter
from .grammar_library import (
    GRAMMAR_LIBRARY,
    TASK_TYPE_TO_GRAMMAR,
    get_grammar,
    get_grammar_for_task_type,
    truncate_at_grammar_boundary,
    validate_grammar,
)
from .llama_cpp_adapter import LlamaCppProviderAdapter
from .llama_cpp_local_adapter import LocalInferenceAdapter
from .manager import AdapterManager
from .openai_server_adapter import OpenAIServerAdapter
from .pool import ModelPool
from .registry import AdapterRegistry

__all__ = [
    "GRAMMAR_LIBRARY",
    "TASK_TYPE_TO_GRAMMAR",
    "AdapterManager",
    "AdapterRegistry",
    "CloudAdapter",
    "InferenceRequest",
    "InferenceResponse",
    "LlamaCppProviderAdapter",
    "LocalInferenceAdapter",
    "ModelInfo",
    "ModelPool",
    "OpenAIServerAdapter",
    "ProviderAdapter",
    "ProviderConfig",
    "clear_adapter_cache",
    "get_grammar",
    "get_grammar_for_task_type",
    "get_local_inference_adapter",
    "truncate_at_grammar_boundary",
    "validate_grammar",
]
