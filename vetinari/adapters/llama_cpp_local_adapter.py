"""LocalInferenceAdapter — high-level convenience wrapper for local GGUF inference.

Provides a simple chat/infer/stream interface that closely mirrors the removed
LMStudioAdapter so existing callers can switch with minimal code changes.

Pipeline role:
    API route → **LocalInferenceAdapter** → LlamaCppProviderAdapter → llama.cpp
    This is the entry point agents use; it hides ProviderConfig construction,
    model discovery, and continuous-batching bookkeeping.
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar, cast

from vetinari.constants import (
    DEFAULT_CONTEXT_LENGTH,
    DEFAULT_GPU_LAYERS,
    OPERATOR_MODELS_CACHE_DIR,
)

from .base import InferenceRequest, ProviderConfig, ProviderType
from .llama_cpp_adapter import LlamaCppProviderAdapter
from .llama_cpp_lazy_imports import (
    _get_current_config,
    _get_inference_batcher_fns,
    _get_inference_config_fn,
)
from .llama_cpp_model_info import DEFAULT_MEMORY_BUDGET_GB, DEFAULT_RAM_BUDGET_GB

logger = logging.getLogger(__name__)
T = TypeVar("T")


def _rough_token_count(text: str) -> int:
    """Approximate whitespace-separated token count without allocating a list.

    ``len(text.split())`` allocates a full list of substrings just to
    measure its length, which is wasted work on the per-inference
    accounting path.  This helper streams the characters once,
    counting transitions from whitespace into non-whitespace runs.

    Args:
        text: Source text to measure.

    Returns:
        Number of whitespace-delimited tokens.  Consecutive whitespace
        characters count as one separator.
    """
    count = 0
    in_token = False
    for ch in text:
        if ch.isspace():
            in_token = False
        elif not in_token:
            count += 1
            in_token = True
    return count


class _RuntimeTimeoutError(TimeoutError):
    """Raised when a local in-process operation exceeds its caller budget."""


def _run_with_timeout(operation: Callable[[], T], timeout_seconds: int, *, operation_name: str) -> T:
    """Run a blocking operation with a bounded caller wait.

    Args:
        operation: Callable to run in a background daemon thread.
        timeout_seconds: Maximum seconds the caller will wait.
        operation_name: Human-readable operation name used in timeout errors.

    Returns:
        The callable result.

    Raises:
        _RuntimeTimeoutError: If the operation exceeds ``timeout_seconds``.
        BaseException: Re-raises any exception from ``operation``.
    """
    if timeout_seconds <= 0:
        raise _RuntimeTimeoutError(f"{operation_name} timed out before start")

    result_queue: queue.Queue[tuple[bool, T | BaseException]] = queue.Queue(maxsize=1)

    def _target() -> None:
        try:
            result_queue.put((True, operation()))
        except BaseException as exc:
            result_queue.put((False, exc))

    worker = threading.Thread(target=_target, name=f"local-inference-timeout-{uuid.uuid4().hex[:8]}", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        raise _RuntimeTimeoutError(f"{operation_name} timed out after {timeout_seconds} seconds")

    try:
        ok, result = result_queue.get_nowait()
    except queue.Empty as exc:
        raise _RuntimeTimeoutError(f"{operation_name} ended without returning a result") from exc
    if ok:
        return cast(T, result)
    raise cast(BaseException, result)


def _timeout_response(model_id: str, timeout_seconds: int) -> dict[str, Any]:
    """Build the standard local inference timeout response payload.

    Args:
        model_id: Model identifier that timed out.
        timeout_seconds: Timeout budget that was exceeded.

    Returns:
        LocalInferenceAdapter-compatible error response.
    """
    return {
        "output": "",
        "latency_ms": float(timeout_seconds * 1000),
        "tokens_used": 0,
        "status": "error",
        "error": f"Local inference timed out after {timeout_seconds} seconds for {model_id}",
        "is_fallback": True,
    }


# ── Module-level lazy getters ─────────────────────────────────────────────────
# These callables live in sub-packages that would create circular imports at
# module load time. Each is imported once and cached under a threading.Lock.

# ── LocalInferenceAdapter ─────────────────────────────────────────────────────


class LocalInferenceAdapter:
    """High-level convenience wrapper for local GGUF inference.

    Provides a simple interface matching the removed LMStudioAdapter
    so that callers throughout the codebase can switch with minimal
    code changes.

    Usage::

        adapter = LocalInferenceAdapter()
        result  = adapter.chat(model_id, system_prompt, input_text)
        # result = {"output": "...", "latency_ms": 42, ...}
    """

    def __init__(
        self,
        models_dir: str | Path | None = None,
        gpu_layers: int | None = None,
        context_length: int | None = None,
        memory_budget_gb: int = DEFAULT_MEMORY_BUDGET_GB,
        ram_budget_gb: float = DEFAULT_RAM_BUDGET_GB,
        cpu_offload_enabled: bool = True,
    ):
        """Build a ProviderConfig and discover available GGUF models on disk.

        Reads live config from ``vetinari.web.shared.current_config`` when the
        caller does not supply explicit overrides, so ``config/models.yaml``
        and environment variables reach the inference engine automatically.

        Args:
            models_dir: Directory containing .gguf files.
            gpu_layers: Number of layers to offload to GPU (-1 = all).
            context_length: Default context window size.
            memory_budget_gb: Maximum VRAM budget in GB.
            ram_budget_gb: CPU RAM available for offloaded model layers in GB.
                Set to 0 to disable CPU offload.
            cpu_offload_enabled: When True, models too large for VRAM are
                split across GPU and CPU instead of triggering full eviction.
        """
        try:
            _cfg = _get_current_config()
            _models_dir = str(models_dir or _cfg.get("models_dir", OPERATOR_MODELS_CACHE_DIR))
            _gpu_layers = (
                gpu_layers
                if gpu_layers is not None
                else int(_cfg.get("gpu_layers", _cfg.get("local_gpu_layers", DEFAULT_GPU_LAYERS)))
            )
            _context_length = (
                context_length
                if context_length is not None
                else int(_cfg.get("local_context_length", DEFAULT_CONTEXT_LENGTH))
            )
            memory_budget_gb = (
                memory_budget_gb
                if memory_budget_gb != DEFAULT_MEMORY_BUDGET_GB
                else int(_cfg.get("memory_budget_gb", DEFAULT_MEMORY_BUDGET_GB))
            )
        except Exception:
            _models_dir = str(models_dir or OPERATOR_MODELS_CACHE_DIR)
            _gpu_layers = gpu_layers if gpu_layers is not None else DEFAULT_GPU_LAYERS
            _context_length = context_length if context_length is not None else DEFAULT_CONTEXT_LENGTH

        cfg = ProviderConfig(
            name="local-inference",
            provider_type=ProviderType.LOCAL,
            endpoint="local",
            memory_budget_gb=memory_budget_gb,
            extra_config={
                "models_dir": _models_dir,
                "gpu_layers": str(_gpu_layers),
                "context_length": str(_context_length),
                "ram_budget_gb": str(ram_budget_gb),
                "cpu_offload_enabled": str(cpu_offload_enabled),
            },
        )

        # Module-level ``LlamaCppProviderAdapter`` (no longer circular) so
        # tests can monkey-patch the symbol on this module without the
        # per-init import re-binding it back to the real class.
        self.provider = LlamaCppProviderAdapter(cfg)
        self.provider.discover_models()

    @staticmethod
    def _resolve_inference_params(task_type: str = "general", model_id: str = "") -> dict[str, Any]:
        """Resolve inference parameters from InferenceConfigManager.

        Falls back to conservative defaults when the config manager is not
        loaded or the profile is missing.

        Args:
            task_type: Task profile key (e.g. ``"coding"``, ``"general"``).
            model_id: Model identifier for model-specific adjustments.

        Returns:
            Dict with ``temperature``, ``max_tokens``, ``top_p``, ``top_k``,
            and other inference parameters from the active profile.
        """
        try:
            cfg = _get_inference_config_fn()()
            return cast(dict[str, Any], cfg.get_effective_params(task_type, model_id))
        except Exception:
            logger.warning("InferenceConfigManager unavailable; using defaults")
            return {"temperature": 0.3, "max_tokens": 2048, "top_p": 0.9, "top_k": 40}

    @staticmethod
    def _chat_via_batcher(
        *,
        model_id: str,
        system_prompt: str,
        input_text: str,
        timeout: int,
        temperature: float | None,
        task_type: str,
        profile: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Run the continuous-batching path when enabled.

        Receives the pre-resolved inference ``profile`` so the chat()
        wrapper resolves it once and both paths reuse the same dict
        (operability contract: a batcher miss must not pay a second
        ``_resolve_inference_params`` call).
        """
        try:
            get_inference_batcher, BatchRequest = _get_inference_batcher_fns()
            batcher = get_inference_batcher()
            if not batcher.enabled:
                return None

            effective_temp = temperature if temperature is not None else profile.get("temperature", 0.3)
            batch_request = BatchRequest(
                request_id=uuid.uuid4().hex[:12],
                model_id=model_id,
                prompt=input_text,
                system_prompt=system_prompt,
                max_tokens=profile.get("max_tokens", 2048),
                temperature=effective_temp,
                task_type=task_type,
                event=threading.Event(),
            )
            output = _run_with_timeout(
                lambda: batcher.submit(batch_request),
                timeout,
                operation_name="llama.cpp batch inference",
            )
            return {
                "output": output,
                "latency_ms": 0.0,
                "tokens_used": _rough_token_count(system_prompt)
                + _rough_token_count(input_text)
                + _rough_token_count(output),
                "status": "ok",
                "error": None,
                "is_fallback": False,
            }
        except _RuntimeTimeoutError:
            logger.warning("Local batch inference timed out for %s after %s seconds", model_id, timeout)
            return _timeout_response(model_id, timeout)
        except Exception as batch_err:
            logger.warning("Continuous batching unavailable: %s", batch_err)
            return None

    def _chat_via_provider(
        self,
        *,
        model_id: str,
        system_prompt: str,
        input_text: str,
        timeout: int,
        temperature: float | None,
        task_type: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Run the direct provider inference path.

        Receives the pre-resolved inference ``params`` so a batcher
        miss followed by a provider fallback does not pay a second
        ``_resolve_inference_params`` call.
        """
        effective_temp = temperature if temperature is not None else params.get("temperature", 0.3)
        req = InferenceRequest(
            model_id=model_id,
            prompt=input_text,
            system_prompt=system_prompt,
            max_tokens=params.get("max_tokens", 2048),
            temperature=effective_temp,
            top_p=params.get("top_p", 0.9),
            top_k=params.get("top_k", 40),
        )
        try:
            resp = _run_with_timeout(
                lambda: self.provider.infer(req),
                timeout,
                operation_name="llama.cpp inference",
            )
        except _RuntimeTimeoutError:
            logger.warning("Local inference timed out for %s after %s seconds", model_id, timeout)
            return _timeout_response(model_id, timeout)
        return {
            "output": resp.output,
            "latency_ms": resp.latency_ms,
            "tokens_used": resp.tokens_used,
            "status": resp.status,
            "error": resp.error,
            "is_fallback": resp.status != "ok" or resp.error is not None,
        }

    def chat(
        self,
        model_id: str,
        system_prompt: str,
        input_text: str,
        timeout: int = 120,
        temperature: float | None = None,
        max_tokens: int | None = None,
        task_type: str = "general",
    ) -> dict[str, Any]:
        """Run a chat completion and return a result dict.

        Attempts the continuous-batching path first; falls back to
        direct ``LlamaCppProviderAdapter.infer()`` when unavailable.

        Args:
            model_id: Model to use (or ``"default"`` for first available).
            system_prompt: System prompt text.
            input_text: User message text.
            timeout: Maximum seconds to wait for local in-process inference
                before returning a typed timeout error to the caller.
            temperature: Optional temperature override. When provided,
                overrides the value from the inference profile. Use this
                for generation tasks that require a specific temperature
                (e.g. Magpie instruction generation at 0.9).
            max_tokens: Optional output-token cap override.
            task_type: Inference profile key used to resolve generation
                parameters.

        Returns:
            Dict with keys: ``output``, ``latency_ms``, ``tokens_used``,
            ``status``, ``error``, ``is_fallback``.

        Raises:
            ValueError: If ``max_tokens`` is provided and is not positive.
        """
        # Resolve the inference profile once and reuse it across the
        # batcher and provider paths so a batcher miss does not pay a
        # second ``_resolve_inference_params`` call (operability
        # contract: resolved profile is reused across the fallback).
        profile = dict(self._resolve_inference_params(task_type, model_id))
        if max_tokens is not None:
            if max_tokens <= 0:
                raise ValueError("max_tokens must be positive")
            profile["max_tokens"] = max_tokens
        batch_result = self._chat_via_batcher(
            model_id=model_id,
            system_prompt=system_prompt,
            input_text=input_text,
            timeout=timeout,
            temperature=temperature,
            task_type=task_type,
            profile=profile,
        )
        if batch_result is not None:
            return batch_result
        return self._chat_via_provider(
            model_id=model_id,
            system_prompt=system_prompt,
            input_text=input_text,
            timeout=timeout,
            temperature=temperature,
            task_type=task_type,
            params=profile,
        )

    def infer(self, model_id: str, prompt: str, timeout: int = 120, task_type: str = "general") -> dict[str, Any]:
        """Run a simple prompt inference without a system prompt.

        Args:
            model_id: Model to use.
            prompt: The prompt text.
            timeout: Maximum seconds to wait; see ``chat()`` for details.
            task_type: Inference profile key used to resolve generation
                parameters.

        Returns:
            Dict with keys: ``output``, ``latency_ms``, ``tokens_used``,
            ``status``, ``error``, ``is_fallback``.
        """
        return self.chat(model_id, "", prompt, timeout=timeout, task_type=task_type)

    def chat_stream(
        self,
        model_id: str,
        system_prompt: str,
        input_text: str,
        timeout: int = 180,
        task_type: str = "general",
    ) -> Iterator[str]:
        """Stream chat completion tokens.

        Args:
            model_id: Model to use.
            system_prompt: System prompt text.
            input_text: User message text.
            timeout: Unused; see ``chat()`` for details.
            task_type: Inference profile key used to resolve generation
                parameters.

        Yields:
            Token strings as they are generated.
        """
        _params = self._resolve_inference_params(task_type, model_id)
        req = InferenceRequest(
            model_id=model_id,
            prompt=input_text,
            system_prompt=system_prompt,
            max_tokens=_params.get("max_tokens", 2048),
            temperature=_params.get("temperature", 0.3),
            top_p=_params.get("top_p", 0.9),
            top_k=_params.get("top_k", 40),
        )
        yield from self.provider.infer_stream(req)

    def list_loaded_models(self) -> list[dict[str, Any]]:
        """Return discovered model information as serialisable dicts.

        Returns:
            List of dicts with keys: ``id``, ``name``, ``capabilities``,
            ``memory_gb``, ``context_len``.
        """
        models = self.provider.discover_models()
        loaded = []
        for model in models:
            capabilities = [cap for cap in model.capabilities if isinstance(cap, str) and cap.strip()]
            if not capabilities:
                logger.warning("Ignoring local model %s because it has no declared capabilities", model.id)
                continue
            loaded.append({
                "id": model.id,
                "name": model.name,
                "capabilities": capabilities,
                "memory_gb": model.memory_gb,
                "context_len": model.context_len,
                "deployment_status": "reachable",
                "reachability": {
                    "status": "reachable",
                    "provider": getattr(model.provider, "value", str(model.provider)),
                },
            })
        return loaded

    def is_healthy(self) -> bool:
        """Return True if local inference is available and models exist.

        Returns:
            True if llama-cpp-python is installed and .gguf models are found.
        """
        health = self.provider.health_check()
        return bool(health.get("healthy", False))
