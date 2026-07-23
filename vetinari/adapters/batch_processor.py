"""Batch Processor — vetinari.adapters.batch_processor.

Queues non-urgent inference requests and flushes them in batches to provider
batch endpoints (typically 50% cost discount vs. synchronous API calls).

Supported providers
-------------------
- Anthropic  — uses /v1/messages/batches  (up to 10 000 requests, 50% discount)
- OpenAI     — uses /v1/batches            (up to 50 000 requests, 50% discount)

For providers without batch endpoints the processor falls back to sequential
synchronous calls so callers always get a result.

Usage
-----
    from vetinari.adapters.batch_processor import BatchProcessor, get_batch_processor

    bp = get_batch_processor()

    # Enqueue (non-blocking)
    future = bp.enqueue(request, provider="anthropic")

    # Wait for result
    response = future.result(timeout=300)

    # Or flush immediately
    results = bp.flush()

Configuration
-------------
- ``BATCH_FLUSH_INTERVAL``  — seconds between automatic flushes (default 60)
- ``BATCH_MAX_SIZE``        — max requests per batch before forced flush (default 100)
- ``BATCH_ENABLED``         — set to "0" to disable batching (falls back to sync)
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any

from vetinari.adapters.base import InferenceResponse
from vetinari.adapters.batch_backends import (
    _AnthropicBatchBackend,
    _OpenAIBatchBackend,
)
from vetinari.boundary_guards import require_nonempty
from vetinari.constants import (
    THREAD_JOIN_TIMEOUT,
)

logger = logging.getLogger(__name__)


def _default_flush_interval() -> float:
    return float(os.environ.get("BATCH_FLUSH_INTERVAL", "60"))


def _default_max_batch_size() -> int:
    return int(os.environ.get("BATCH_MAX_SIZE", "100"))


def _default_batch_enabled() -> bool:
    return os.environ.get("BATCH_ENABLED", "1").lower() not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class BatchItem:
    """A single queued inference request."""

    item_id: str
    provider: str
    request: Any  # vetinari.adapters.base.InferenceRequest
    future: Future[Any]
    enqueued_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        model = getattr(self.request, "model_id", "unknown")
        return f"BatchItem(item_id={self.item_id!r}, model={model!r}, provider={self.provider!r})"


@dataclass(frozen=True, slots=True)
class BatchResult:
    """Result for a single batch item."""

    item_id: str
    response: Any | None  # vetinari.adapters.base.InferenceResponse
    error: str | None = None
    cached: bool = False

    def __repr__(self) -> str:
        return f"BatchResult(item_id={self.item_id!r}, success={self.success!r})"

    @property
    def success(self) -> bool:
        """Whether this batch item completed without error and has a response."""
        return self.error is None and self.response is not None


# ---------------------------------------------------------------------------
# BatchProcessor
# ---------------------------------------------------------------------------


class BatchProcessor:
    """Queues non-urgent inference requests and flushes them in batches.

    Batching provides ~50% cost discount on supported providers.
    For unsupported providers, falls back to sequential sync calls.

    Thread-safe. The auto-flush background thread is started lazily on first enqueue.
    """

    def __init__(
        self,
        flush_interval: float | None = None,
        max_batch_size: int | None = None,
        enabled: bool | None = None,
    ):
        self.flush_interval = _default_flush_interval() if flush_interval is None else flush_interval
        self.max_batch_size = _default_max_batch_size() if max_batch_size is None else max_batch_size
        self.enabled = _default_batch_enabled() if enabled is None else enabled

        self._queue: dict[str, list[BatchItem]] = {}  # provider -> items
        self._lock = threading.Lock()
        self._flush_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._backends: dict[str, Any] = {}

        logger.info(
            "BatchProcessor initialized (enabled=%s, flush_interval=%.0fs, max_batch=%d)",
            enabled,
            flush_interval,
            max_batch_size,
        )

    def register_backend(self, provider: str, backend: Any) -> None:
        """Register a batch backend for a provider.

        Args:
            provider: The provider.
            backend: The backend.
        """
        self._backends[provider] = backend
        logger.debug("Registered batch backend for provider: %s", provider)

    def register_anthropic(self, api_key: str, api_version: str = "2023-06-01") -> None:
        """Convenience method to register Anthropic batch backend.

        Args:
            api_key: The api key.
            api_version: The api version.
        """
        self.register_backend("anthropic", _AnthropicBatchBackend(api_key, api_version))

    def register_openai(self, api_key: str) -> None:
        """Convenience method to register OpenAI batch backend."""
        self.register_backend("openai", _OpenAIBatchBackend(api_key))

    def enqueue(self, request: Any, provider: str = "anthropic", metadata: dict[str, Any] | None = None) -> Future[Any]:
        """Enqueue a request for batch processing.

        Args:
            request: InferenceRequest to process.
            provider: Provider name ("anthropic", "openai", etc.).
            metadata: Optional extra metadata for the batch item.

        Returns:
            Future that resolves to an InferenceResponse.
        """
        future: Future[Any] = Future()

        if not self.enabled:
            # Fall through to sync immediately
            self._execute_sync(request, provider, future)
            return future

        item = BatchItem(
            item_id=str(uuid.uuid4()),
            provider=provider,
            request=request,
            future=future,
            metadata=metadata or {},
        )

        with self._lock:
            if provider not in self._queue:
                self._queue[provider] = []
            self._queue[provider].append(item)
            queue_len = len(self._queue[provider])

        logger.debug("Enqueued request %s for %s (queue_len=%d)", item.item_id, provider, queue_len)

        # Auto-flush if batch is full (reuse the background flush thread)
        if queue_len >= self.max_batch_size:
            self._flush_provider(provider)

        # Start background flush thread if not running
        self._ensure_flush_thread()

        return future

    def flush(self, provider: str | None = None) -> int:
        """Flush queued requests immediately.

        Args:
            provider: Specific provider to flush, or None for all.

        Returns:
            Number of items flushed.
        """
        if provider:
            return self._flush_provider(provider)
        total = 0
        with self._lock:
            providers = list(self._queue.keys())
        for p in providers:
            total += self._flush_provider(p)
        return total

    def _flush_provider(self, provider: str) -> int:
        """Flush all queued items for a provider. Returns count flushed."""
        with self._lock:
            items = self._queue.pop(provider, [])

        if not items:
            return 0

        logger.info("Flushing %d items for provider: %s", len(items), provider)

        backend = self._backends.get(provider)
        if backend is None:
            # No batch backend — fall back to sequential sync
            logger.debug("No batch backend for %s, running sync fallback", provider)
            for item in items:
                self._execute_sync(item.request, provider, item.future)
            return len(items)

        try:
            results = backend.submit(items)
            for item in items:
                result = results.get(item.item_id)
                if result and result.success:
                    item.future.set_result(_validated_batch_response(result))
                else:
                    err = result.error if result else "No result returned"
                    # Return error response rather than exception so callers can handle gracefully
                    item.future.set_result(
                        InferenceResponse(
                            model_id=item.request.model_id,
                            output="",
                            latency_ms=0,
                            tokens_used=0,
                            status="error",
                            error=err,
                        ),
                    )
        except Exception as exc:
            logger.error("Batch flush failed for %s: %s", provider, exc)

            for item in items:
                if not item.future.done():
                    item.future.set_result(
                        InferenceResponse(
                            model_id=item.request.model_id,
                            output="",
                            latency_ms=0,
                            tokens_used=0,
                            status="error",
                            error=str(exc),
                        ),
                    )

        return len(items)

    @staticmethod
    def _execute_sync(request: Any, provider: str, future: Future[Any]) -> None:
        """Execute a single request synchronously and resolve the future."""
        try:
            from vetinari.adapter_manager import get_adapter_manager

            manager = get_adapter_manager()
            adapter = manager.get_provider(provider)
            if adapter:
                response = adapter.infer(request)
            else:
                response = InferenceResponse(
                    model_id=request.model_id,
                    output="",
                    latency_ms=0,
                    tokens_used=0,
                    status="error",
                    error=f"No adapter for provider: {provider}",
                )
            future.set_result(response)
        except Exception as exc:
            future.set_result(
                InferenceResponse(
                    model_id=getattr(request, "model_id", "unknown"),
                    output="",
                    latency_ms=0,
                    tokens_used=0,
                    status="error",
                    error=str(exc),
                ),
            )

    def _ensure_flush_thread(self) -> None:
        """Start background flush thread if not already running."""
        if self._flush_thread is not None and self._flush_thread.is_alive():
            return

        self._stop_event.clear()

        def _auto_flush() -> None:
            logger.info("BatchProcessor auto-flush thread started (interval=%.0fs)", self.flush_interval)
            while not self._stop_event.wait(self.flush_interval):
                try:
                    self.flush()
                except Exception as exc:
                    logger.error("Auto-flush error: %s", exc)
            logger.info("BatchProcessor auto-flush thread stopped")

        self._flush_thread = threading.Thread(target=_auto_flush, daemon=True, name="batch-processor-flush")
        self._flush_thread.start()

    def stop(self) -> None:
        """Stop the background flush thread and flush remaining items."""
        self._stop_event.set()
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=THREAD_JOIN_TIMEOUT)
        # Final flush
        self.flush()

    def get_queue_stats(self) -> dict[str, Any]:
        """Return current queue statistics.

        Returns:
            Dictionary with keys ``enabled``, ``providers`` (per-provider
            queue depths), ``total_queued``, ``flush_interval``,
            ``max_batch_size``, ``backends_registered``, and
            ``flush_thread_active``.
        """
        with self._lock:
            return {
                "enabled": self.enabled,
                "providers": {p: len(items) for p, items in self._queue.items()},
                "total_queued": sum(len(items) for items in self._queue.values()),
                "flush_interval": self.flush_interval,
                "max_batch_size": self.max_batch_size,
                "backends_registered": list(self._backends.keys()),
                "flush_thread_active": self._flush_thread is not None and self._flush_thread.is_alive(),
            }


def _cached_response_provenance(response: Any) -> str | None:
    direct = getattr(response, "provenance", None)
    if direct:
        return str(direct)
    metadata = getattr(response, "metadata", None)
    if isinstance(metadata, dict):
        value = metadata.get("provenance") or metadata.get("provenance_ref") or metadata.get("provenance_id")
        if value:
            return str(value)
    return None


def _validated_batch_response(result: BatchResult) -> Any:
    response = result.response
    if result.cached:
        require_nonempty(_cached_response_provenance(response), field_name="cache_hit_provenance")
    return response


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_batch_processor: BatchProcessor | None = None
_bp_lock = threading.Lock()


def get_batch_processor() -> BatchProcessor:
    """Get or create the global BatchProcessor instance.

    Returns:
        The process-wide BatchProcessor singleton.  Created with default
        settings on first call; subsequent calls return the same instance.
    """
    global _batch_processor
    if _batch_processor is None:
        with _bp_lock:
            if _batch_processor is None:
                import os

                _batch_processor = BatchProcessor()
                anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
                if anthropic_key:
                    _batch_processor.register_anthropic(anthropic_key)
                openai_key = os.environ.get("OPENAI_API_KEY", "")
                if openai_key:
                    _batch_processor.register_openai(openai_key)
    return _batch_processor


def reset_batch_processor() -> None:
    """Reset the global BatchProcessor (useful for testing)."""
    global _batch_processor
    with _bp_lock:
        if _batch_processor is not None:
            _batch_processor.stop()
        _batch_processor = None
