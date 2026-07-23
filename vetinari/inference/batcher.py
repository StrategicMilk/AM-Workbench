"""Continuous Batching (C17).

==========================
Thread-safe inference request batching for local in-process inference.

Collects inference requests into batches and dispatches them together
every ``max_wait_ms`` or when ``max_batch_size`` is reached.

Config keys: batching.enabled, batching.max_batch_size, batching.max_wait_ms
"""

from __future__ import annotations

import contextlib
import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from vetinari.adapters.adapter_cache import get_local_inference_adapter
from vetinari.constants import INFERENCE_BATCHER_QUEUE_SIZE, THREAD_JOIN_TIMEOUT
from vetinari.exceptions import InferenceError
from vetinari.safety.guardrails_manager import GuardrailsManager
from vetinari.safety.guardrails_types import RailContext
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BatchDispatchFailure:
    """Typed failure emitted when a batched request cannot complete."""

    reason: str
    request_id: str
    model_id: str


@dataclass(frozen=True, slots=True)
class BatchRequest:
    """A single inference request in the batch queue."""

    request_id: str
    model_id: str
    prompt: str
    system_prompt: str = ""
    max_tokens: int = 2048
    temperature: float = 0.3
    task_type: str = "general"
    callback: Callable | None = None
    result: str | None = None
    error: str | None = None
    failure: BatchDispatchFailure | None = None
    event: threading.Event = field(default_factory=threading.Event)

    def __repr__(self) -> str:
        return (
            f"BatchRequest(request_id={self.request_id!r}, model_id={self.model_id!r}, max_tokens={self.max_tokens!r})"
        )


@dataclass(slots=True)
class _BatchSubmission:
    """Dispatch outcome container for an immutable batch request."""

    request: BatchRequest
    result: str | None = None
    error: str | None = None
    failure: BatchDispatchFailure | None = None
    event: threading.Event = field(default_factory=threading.Event)

    def __repr__(self) -> str:
        return (
            "_BatchSubmission("
            f"request_id={self.request.request_id!r}, "
            f"model_id={self.request.model_id!r}, "
            f"has_result={self.result is not None!r}, "
            f"has_error={self.error is not None!r})"
        )

    def set_result(self, result: str) -> None:
        """Store a successful dispatch result from the worker thread."""
        self.result = result

    def set_failure(self, reason: str) -> None:
        """Store a dispatch failure from the worker thread."""
        self.failure = BatchDispatchFailure(
            reason=reason,
            request_id=self.request.request_id,
            model_id=self.request.model_id,
        )
        self.error = self.failure.reason


@dataclass(frozen=True, slots=True)
class BatchConfig:
    """Configuration for the inference batcher."""

    enabled: bool = False
    max_batch_size: int = 8
    max_wait_ms: float = 100.0  # dispatch every 100ms
    models_dir: str = ""

    def __repr__(self) -> str:
        return f"BatchConfig(enabled={self.enabled!r}, max_batch_size={self.max_batch_size!r}, max_wait_ms={self.max_wait_ms!r})"


class InferenceBatcher:
    """Collects and batches inference requests.

    When ``submit()`` is called, the request is queued. A background
    thread dispatches batches using the local in-process inference adapter
    either when the batch is full or the wait timer expires.
    """

    def __init__(self, config: BatchConfig | None = None):
        cfg = config or BatchConfig()
        if not cfg.models_dir:
            cfg = replace(cfg, models_dir=os.environ.get("VETINARI_MODELS_DIR", ""))
        self._config = cfg
        self._queue: queue.Queue[_BatchSubmission | None] = queue.Queue(maxsize=INFERENCE_BATCHER_QUEUE_SIZE)
        self._running = False
        self._thread: threading.Thread | None = None
        self._total_batches = 0
        self._total_requests = 0
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stopping = threading.Event()

    @property
    def enabled(self) -> bool:
        """Whether continuous batching is enabled in the current configuration."""
        return self._config.enabled

    def start(self) -> None:
        """Start the background dispatch thread."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            self._running = True
            self._thread = threading.Thread(target=self._dispatch_loop, daemon=True, name="InferenceBatcher")
            self._thread.start()
        logger.info(
            "InferenceBatcher started (batch_size=%d, wait_ms=%.0f)",
            self._config.max_batch_size,
            self._config.max_wait_ms,
        )

    def stop(self, timeout: float = THREAD_JOIN_TIMEOUT) -> None:
        """Drain queued requests and stop the dispatch thread.

        Accepted requests are processed before the sentinel is enqueued. If a
        dispatch call blocks past ``timeout``, shutdown is still attempted and a
        warning records the remaining unfinished queue count.
        """
        self._stopping.set()
        self._running = False
        if self._thread is None or not self._thread.is_alive():
            self._stopping.clear()
            return
        remaining = self.drain(timeout=timeout)
        if remaining:
            logger.warning("InferenceBatcher shutdown timed out with %d queued request(s)", remaining)
        # Sentinel wakes the worker so it exits without waiting for a real request.
        with contextlib.suppress(queue.Full):
            self._queue.put(None, timeout=timeout)
        if self._thread:
            self._thread.join(timeout=timeout)
        with self._lifecycle_lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None
        self._stopping.clear()

    def drain(self, timeout: float | None = THREAD_JOIN_TIMEOUT) -> int:
        """Wait until accepted batch requests have been dispatched.

        Args:
            timeout: Maximum seconds to wait. ``None`` waits indefinitely.

        Returns:
            Number of unfinished queue items remaining after the wait.
        """
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                if deadline is None:
                    self._queue.all_tasks_done.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._queue.unfinished_tasks
                self._queue.all_tasks_done.wait(timeout=min(0.1, remaining))
            return 0

    def submit(self, request: BatchRequest, timeout: float = 30.0) -> str:
        """Submit an inference request and wait for the result.

        If batching is disabled, dispatches immediately (synchronous).

        Args:
            request: The request.
            timeout: The timeout.

        Returns:
            The generated text output from the inference model.  Empty string
            if batching is enabled but the model produced no output.

        Raises:
            InferenceError: If the inference call fails or the batch marks the
                request as errored.
        """
        guarded_request = _guarded_batch_request(request)
        if not self._config.enabled:
            return self._dispatch_single(guarded_request)

        submission = _BatchSubmission(request=guarded_request, event=request.event)
        if self._stopping.is_set():
            submission.set_failure("shutting_down")
            raise InferenceError("Batch inference failed: shutting_down", failure=submission.failure)

        if not self._running:
            self.start()

        try:
            self._queue.put(submission, timeout=timeout)
        except queue.Full as exc:
            submission.set_failure("queue_full")
            raise InferenceError("Batch inference failed: queue_full", failure=submission.failure) from exc
        completed = submission.event.wait(timeout=timeout)

        if not completed:
            submission.set_failure("timeout")
            raise InferenceError("Batch inference failed: timeout", failure=submission.failure)
        if submission.error:
            raise InferenceError(f"Batch inference failed: {submission.error}", failure=submission.failure)
        return submission.result or ""

    def _dispatch_loop(self) -> None:
        """Background thread: collect and dispatch batches."""
        while True:
            batch: list[_BatchSubmission] = []
            deadline = time.monotonic() + self._config.max_wait_ms / 1000.0

            # Collect up to max_batch_size or until deadline
            while len(batch) < self._config.max_batch_size:
                remaining = max(0, deadline - time.monotonic())
                try:
                    req = self._queue.get(timeout=remaining)
                    if req is None:
                        # Sentinel placed by stop() — exit immediately.
                        with contextlib.suppress(ValueError):
                            self._queue.task_done()
                        return
                    batch.append(req)
                except queue.Empty:
                    break

            if batch:
                try:
                    self._dispatch_batch(batch)
                finally:
                    for _req in batch:
                        with contextlib.suppress(ValueError):
                            self._queue.task_done()

    def _dispatch_batch(self, batch: list[_BatchSubmission]) -> None:
        """Dispatch a batch of requests via local in-process inference."""
        with self._lock:
            self._total_batches += 1
            self._total_requests += len(batch)

        # Group by model for efficient batching
        by_model: dict[str, list[_BatchSubmission]] = {}
        for submission in batch:
            by_model.setdefault(submission.request.model_id, []).append(submission)

        for model_id, submissions in by_model.items():
            try:
                adapter = get_local_inference_adapter(model_id or None)
                for submission in submissions:
                    req = submission.request
                    try:
                        result = adapter.chat(
                            model_id=model_id or "default",
                            system_prompt=req.system_prompt,
                            input_text=req.prompt,
                            task_type=req.task_type,
                            max_tokens=req.max_tokens,
                            temperature=req.temperature,
                        )
                        submission.set_result(result.get("output", ""))
                    except Exception as e:
                        submission.set_failure(str(e))
                    finally:
                        submission.event.set()
            except Exception as e:
                for submission in submissions:
                    submission.set_failure(str(e))
                    submission.event.set()

    @staticmethod
    def _dispatch_single(request: BatchRequest) -> str:
        """Synchronous single-request dispatch (batching disabled)."""
        try:
            request = _guarded_batch_request(request)
            adapter = get_local_inference_adapter(request.model_id or None)
            result = adapter.chat(
                model_id=request.model_id or "default",
                system_prompt=request.system_prompt,
                input_text=request.prompt,
                task_type=request.task_type,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
            )
            return str(result.get("output", ""))
        except Exception as e:
            raise InferenceError(f"Inference failed: {e}") from e

    def get_stats(self) -> dict[str, Any]:
        """Return runtime statistics for the inference batcher.

        Returns:
            Dictionary containing enabled state, running status, total batches
            dispatched, total requests processed, average batch size, and
            current queue depth.
        """
        return {
            "enabled": self._config.enabled,
            StatusEnum.RUNNING.value: self._running,
            "total_batches": self._total_batches,
            "total_requests": self._total_requests,
            "avg_batch_size": (self._total_requests / max(self._total_batches, 1)),
            "queue_size": self._queue.qsize(),
        }


# ── Singleton ─────────────────────────────────────────────────────────

_batcher: InferenceBatcher | None = None
_batcher_lock = threading.Lock()


def get_inference_batcher(config: BatchConfig | None = None) -> InferenceBatcher:
    """Get inference batcher.

    Returns:
        The process-wide InferenceBatcher singleton.  If it does not yet
        exist, it is created with ``config`` (or default settings if None).
        Subsequent calls ignore ``config`` and return the existing instance.
    """
    global _batcher
    if _batcher is None:
        with _batcher_lock:
            if _batcher is None:
                _batcher = InferenceBatcher(config)
    return _batcher


def _guarded_batch_request(request: BatchRequest) -> BatchRequest:
    """Run input guardrails before local batch dispatch."""
    guardrails = GuardrailsManager()
    prompt_result = guardrails.check_input(request.prompt, context=RailContext.USER_FACING)
    if not prompt_result.allowed:
        reason = ", ".join(v.rail for v in prompt_result.violations) or "input_guardrail"
        raise InferenceError(f"Batch inference blocked by input guardrails: {reason}")

    system_prompt = request.system_prompt
    if system_prompt:
        system_result = guardrails.check_input(system_prompt, context=RailContext.USER_FACING)
        if not system_result.allowed:
            reason = ", ".join(v.rail for v in system_result.violations) or "system_prompt_guardrail"
            raise InferenceError(f"Batch inference blocked by system prompt guardrails: {reason}")
        system_prompt = system_result.content

    return replace(request, prompt=prompt_result.content, system_prompt=system_prompt)
