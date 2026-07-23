"""Token compression — heuristic and LLM-based context reduction.

Contains ``LocalPreprocessor``, which compresses long context strings before
they are sent to inference. Three strategies are used in order of preference:

1. **Heuristic extraction** — AST-based code signature extraction and
   keyword-line filtering (no LLM required, near-instant).
2. **LLMLingua-2 compression** — BERT-level encoder (optional dependency,
   runs on CPU, no LLM server needed).  Installed via ``pip install llmlingua``.
3. **LLM compression** — a small local llama-cpp-python model rewrites the
   context to retain only technically relevant content (~70% token savings).
4. **Truncation fallback** — sliding-window head+tail truncation when no
   strategy is available.

``LocalPreprocessor`` is used by ``TokenOptimizer.prepare_prompt()`` and is
not intended to be called directly by most callers.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Any

from vetinari.constants import TRUNCATE_RESPONSE
from vetinari.token_compression_extraction import TokenCompressionExtractionMixin

logger = logging.getLogger(__name__)


# Maximum prompt character length before triggering local summarisation
_COMPRESS_THRESHOLD_CHARS = 24000  # ~6K tokens — modern models handle 32K-64K context natively

# Sliding window for context: keep latest N chars when truncating
_CONTEXT_WINDOW_CHARS = 16000  # ~4K tokens — enough for meaningful code context
_LLMLINGUA_COMPRESSORS: dict[str, Any] = {}
_LLMLINGUA_COMPRESSORS_LOCK = threading.Lock()


def LocalInferenceAdapter(*args: Any, **kwargs: Any) -> Any:
    """Lazily construct the local inference adapter.

    Kept as a module-level callable so tests can patch the historical
    ``vetinari.token_compression.LocalInferenceAdapter`` seam without forcing
    an eager llama.cpp import during module import.

    Returns:
        A lazily-imported ``LocalInferenceAdapter`` instance.
    """
    if not args and not kwargs:
        from vetinari.adapters.adapter_cache import get_local_inference_adapter

        return get_local_inference_adapter("token-compression")

    from vetinari.adapters.llama_cpp_local_adapter import LocalInferenceAdapter as _LocalInferenceAdapter

    return _LocalInferenceAdapter(*args, **kwargs)


def _get_token_compression_adapter() -> Any:
    """Return the cached token-compression adapter via the historical patch seam."""
    constructor = globals()["LocalInferenceAdapter"]
    return constructor()


def _get_llmlingua_compressor() -> Any:
    """Return the process-cached LLMLingua compressor for token compression."""
    model_name = "microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank"
    with _LLMLINGUA_COMPRESSORS_LOCK:
        cached = _LLMLINGUA_COMPRESSORS.get(model_name)
        if cached is not None:
            return cached
        from llmlingua import PromptCompressor

        compressor = PromptCompressor(
            model_name=model_name,
            use_llmlingua2=True,
            device_map="cpu",
        )
        _LLMLINGUA_COMPRESSORS[model_name] = compressor
        return compressor


class LocalPreprocessor(TokenCompressionExtractionMixin):
    """Uses a local LLM to compress and distil context before cloud API calls.

    This is the key cost-reduction feature:
    - Input: verbose context (code, docs, prior results)  ~3000 tokens
    - Output: compressed key points                        ~800 tokens
    - Savings: ~70% of cloud input tokens

    Only activates when:
    1. The target model is a cloud model (has cost > 0)
    2. The context length exceeds the threshold
    3. A local llama-cpp-python model is available
    """

    # Minimum context length (chars) to justify preprocessing overhead
    MIN_CONTEXT_CHARS = _COMPRESS_THRESHOLD_CHARS
    CONTEXT_WINDOW_CHARS = _CONTEXT_WINDOW_CHARS

    def __init__(self) -> None:
        self._local_model: str | None = None
        self._local_adapter: Any | None = None
        # Bounded cache: evict oldest entries when full to prevent memory leaks
        self._cache: OrderedDict[str, str] = OrderedDict()
        self._max_cache_size: int = 1024

    def _get_local_adapter(self) -> Any:
        """Return the cached local adapter used for model discovery and compression."""
        if self._local_adapter is None:
            self._local_adapter = _get_token_compression_adapter()
        return self._local_adapter

    def _get_local_model(self) -> str | None:
        """Discover the best available local model for preprocessing.

        Returns:
            Model identifier string, or None if no model is available.
        """
        if self._local_model:
            return self._local_model
        try:
            adapter = self._get_local_adapter()
            models = adapter.list_loaded_models()
            if models:
                # Prefer smaller/faster models for preprocessing
                for m in models:
                    mid = m.get("id", "")
                    if any(x in mid.lower() for x in ["7b", "8b", "3b", "1b"]):
                        self._local_model = mid
                        return mid
                # Fall back to first available model
                self._local_model = models[0].get("id", "")
                return self._local_model
        except Exception:
            logger.warning("Failed to discover local model for token optimization", exc_info=True)
        return None

    def _cache_put(self, key: str, value: str) -> None:
        """Insert into the bounded cache, evicting oldest entries if full."""
        if key in self._cache:
            self._cache.move_to_end(key)
        elif len(self._cache) >= self._max_cache_size:
            self._cache.popitem(last=False)
        self._cache[key] = value

    def compress_context(
        self,
        context: str,
        task_description: str = "",
        compression_goal: str = "key_facts",
    ) -> tuple[str, float]:
        """Compress verbose context with heuristics and local-model fallback.

        Args:
            context: Context value consumed by compress_context().
            task_description: Task description value consumed by compress_context().
            compression_goal: Compression goal value consumed by compress_context().

        Returns:
            Value produced for the caller.
        """
        if len(context) < self.MIN_CONTEXT_CHARS:
            return context, 1.0
        cache_key = hashlib.md5(
            f"{context}{task_description}{compression_goal}".encode(),
            usedforsecurity=False,
        ).hexdigest()
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            self._cache.move_to_end(cache_key)
            ratio = len(cached) / max(len(context), 1)
            return cached, ratio
        heuristic = self._heuristic_compress(context, compression_goal, cache_key)
        if heuristic is not None:
            return heuristic
        llmlingua_result = self._llmlingua_compress(context, task_description)
        if llmlingua_result and len(llmlingua_result) < len(context) * 0.8:
            ratio = len(llmlingua_result) / max(len(context), 1)
            self._cache_put(cache_key, llmlingua_result)
            logger.info(
                "[LocalPreprocessor] LLMLingua-2 compression: %s -> %s chars (%.0f%%)",
                len(context),
                len(llmlingua_result),
                ratio * 100,
            )
            return llmlingua_result, ratio
        local_model = self._get_local_model()
        if not local_model:
            return self._truncate(context), len(context[:_CONTEXT_WINDOW_CHARS]) / max(len(context), 1)
        try:
            goals = {
                "key_facts": "Extract ONLY the key facts, function signatures, API endpoints, and critical constraints. Remove examples, explanations, and repetition.",
                "summary": "Write a concise summary preserving all actionable information and technical specifics.",
                "code_only": "Extract ONLY the function/class definitions, signatures, and docstrings. Remove all prose.",
            }
            goal_instruction = goals.get(compression_goal, goals["key_facts"])
            prompt = (
                f"{goal_instruction}\n\n"
                f"Task context: {task_description[:200]}\n\n"
                f"Content to compress:\n{context[:TRUNCATE_RESPONSE]}\n\n"
                "Provide the compressed version. Be as concise as possible while preserving ALL technically relevant information."
            )
            adapter = self._get_local_adapter()
            result = adapter.chat(
                local_model,
                "You are a context compression specialist. Compress text while preserving all technically critical information.",
                prompt,
            )
            compressed = result.get("output", "").strip()
            if compressed:
                ratio = len(compressed) / max(len(context), 1)
                self._cache_put(cache_key, compressed)
                logger.info(
                    "[LocalPreprocessor] Compressed %s -> %s chars (%.0f%%) for task: %s",
                    len(context),
                    len(compressed),
                    ratio * 100,
                    task_description[:40],
                )
                return compressed, ratio
        except Exception as e:
            logger.warning("[LocalPreprocessor] Compression failed: %s", e)
        return self._truncate(context), len(context[:_CONTEXT_WINDOW_CHARS]) / max(len(context), 1)

    def _heuristic_compress(self, context: str, compression_goal: str, cache_key: str) -> tuple[str, float] | None:
        if compression_goal == "code_only":
            compressed = self._extract_code_signatures(context)
            limit = 0.8
            label = "code extraction"
        elif compression_goal == "key_facts":
            compressed = self._extract_key_lines(context)
            limit = 0.7
            label = "key-facts extraction"
        else:
            return None
        if compressed and len(compressed) < len(context) * limit:
            ratio = len(compressed) / max(len(context), 1)
            self._cache_put(cache_key, compressed)
            logger.info(
                "[LocalPreprocessor] Heuristic %s: %s -> %s chars (%.0f%%)",
                label,
                len(context),
                len(compressed),
                ratio * 100,
            )
            return compressed, ratio
        return None

    def compress(
        self,
        text: str,
        task_description: str = "",
        compression_goal: str = "summary",
    ) -> str:
        """Compress one prompt string for callers that do not split context.

        ``InferenceBehavior._infer`` performs a final context-window guard on the
        assembled task prompt. That boundary has only one text string, unlike
        ``TokenOptimizer.prepare_prompt()``, so it needs a single-string
        compression API rather than the prompt/context tuple returned by
        ``preprocess_for_cloud()``.

        Args:
            text: Prompt text to compress.
            task_description: Optional hint describing the inference task.
            compression_goal: Compression strategy passed through to
                ``compress_context``.

        Returns:
            Compressed text, or the original text when no compression is useful.
        """
        if not text:
            return text
        compressed, _ratio = self.compress_context(text, task_description, compression_goal)
        return compressed

    @staticmethod
    def _llmlingua_compress(context: str, task_description: str) -> str | None:
        """Compress context using LLMLingua-2 BERT-level encoder (optional dependency).

        Runs on CPU, no LLM server needed. Returns None if llmlingua not installed
        or if compression fails for any reason.

        Args:
            context: The context text to compress.
            task_description: Used as instruction hint for guided compression.

        Returns:
            Compressed text string, or None if llmlingua is unavailable or fails.
        """
        try:
            compressor = _get_llmlingua_compressor()
            result = compressor.compress_prompt(
                context,
                instruction=f"Task: {task_description[:200]}",
                rate=0.5,
            )
            return result.get("compressed_prompt")
        except ImportError:
            logger.warning("LLMLingua-2 not installed — token compression unavailable, returning None")
            return None
        except Exception:
            logger.warning("[LocalPreprocessor] LLMLingua-2 compression failed — falling back to LLM summarization")
            return None

    def preprocess_for_cloud(
        self,
        prompt: str,
        context: str = "",
        task_description: str = "",
    ) -> tuple[str, str, dict[str, Any]]:
        """Full preprocessing pipeline for cloud API calls.

        Compresses both context and prompt if they exceed size thresholds,
        reducing cloud token consumption by 30-70%.

        Args:
            prompt: The prompt text to preprocess.
            context: Additional context string to compress.
            task_description: Human-readable description forwarded to the
                compressor for guided key-fact extraction.

        Returns:
            Tuple of (processed_prompt, processed_context, metadata_dict).
        """
        meta: dict[str, Any] = {
            "original_prompt_chars": len(prompt),
            "original_context_chars": len(context),
            "compressed": False,
            "compression_ratio": 1.0,
        }

        if context and len(context) >= self.MIN_CONTEXT_CHARS:
            compressed_context, ratio = self.compress_context(context, task_description, "key_facts")
            meta["compressed"] = ratio < 0.9
            meta["compression_ratio"] = ratio
            context = compressed_context

        if len(prompt) > _COMPRESS_THRESHOLD_CHARS * 2:
            compressed_prompt, ratio = self.compress_context(prompt, task_description, "summary")
            meta["prompt_compressed"] = ratio < 0.9
            prompt = compressed_prompt

        meta["final_prompt_chars"] = len(prompt)
        meta["final_context_chars"] = len(context)
        return prompt, context, meta
