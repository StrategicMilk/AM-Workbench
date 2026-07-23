"""Event bus for inter-agent communication.

Provides a publish/subscribe event system that enables decoupled communication
between agents in the Vetinari orchestration pipeline. Events are published
synchronously or asynchronously, and a bounded history is maintained for
debugging and audit purposes.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import queue
import threading
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from enum import Enum

from vetinari.concurrency import submit_with_context
from vetinari.constants import (
    EVENTBUS_ASYNC_QUEUE_SIZE,
    EVENTBUS_HISTORY_MAX_LENGTH,
    QUEUE_TIMEOUT,
    THREAD_JOIN_TIMEOUT,
    THREAD_JOIN_TIMEOUT_SHORT,
)
from vetinari.events_payloads import (
    AnomalyDetected,
    ClarificationRequested,
    CpuTierRouteStatusChanged,
    CpuTierStatusChanged,
    Event,
    HumanApprovalNeeded,
    KaizenImprovementActive,
    KaizenImprovementConfirmed,
    KaizenImprovementProposed,
    KaizenImprovementReverted,
    KaizenLintFinding,
    QualityDriftDetected,
    QualityGateResult,
    ResourceRequest,
    RetrainingRecommended,
    TaskCompleted,
    TaskStarted,
    TaskTimingRecord,
    TelemetryAlertEvent,
    clarification_requested,
)

__all__ = [
    "AnomalyDetected",
    "ClarificationRequested",
    "CpuTierRouteStatusChanged",
    "CpuTierStatusChanged",
    "Event",
    "EventBus",
    "HumanApprovalNeeded",
    "KaizenImprovementActive",
    "KaizenImprovementConfirmed",
    "KaizenImprovementProposed",
    "KaizenImprovementReverted",
    "KaizenLintFinding",
    "QualityDriftDetected",
    "QualityGateResult",
    "ResourceRequest",
    "RetrainingRecommended",
    "TaskCompleted",
    "TaskStarted",
    "TaskTimingRecord",
    "TelemetryAlertEvent",
    "TimingEvent",
    "clarification_requested",
    "get_event_bus",
    "reset_event_bus",
]

logger = logging.getLogger(__name__)


_HISTORY_MAX_LENGTH = EVENTBUS_HISTORY_MAX_LENGTH  # Maximum number of events retained in the history ring buffer
_HANDLER_TIMEOUT_SECONDS = 5.0
_HANDLER_MAX_WORKERS = 4


# ---------------------------------------------------------------------------
# Event payloads live in vetinari.events_payloads.
# ---------------------------------------------------------------------------


class TimingEvent(Enum):
    """Timing event types for value stream mapping."""

    TASK_QUEUED = "task_queued"
    TASK_DISPATCHED = "task_dispatched"
    TASK_COMPLETED = "task_completed"
    TASK_REJECTED = "task_rejected"
    TASK_REWORK = "task_rework"
    TASK_SKIPPED = "task_skipped"


# ---------------------------------------------------------------------------
# Subscription record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Subscription:
    """Internal record for a single event subscription."""

    subscription_id: str
    event_type: type[Event]
    callback: Callable[[Event], None]


# ---------------------------------------------------------------------------
# EventBus
# ---------------------------------------------------------------------------


class EventBus:
    """Thread-safe publish/subscribe event bus.

    Subscribers register interest in a specific ``Event`` subclass and receive
    callbacks whenever a matching event is published. A bounded history deque
    retains recent events for diagnostic queries.

    This class should not be instantiated directly; use :func:`get_event_bus`
    to obtain the process-wide singleton.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscriptions: dict[str, _Subscription] = {}
        self._history: deque[Event] = deque(maxlen=_HISTORY_MAX_LENGTH)
        self._eviction_count: int = 0  # Total events evicted from history ring buffer
        self._async_queue: queue.Queue[tuple[Event, list[_Subscription]] | None] = queue.Queue(
            maxsize=EVENTBUS_ASYNC_QUEUE_SIZE
        )
        self._async_worker: threading.Thread | None = None
        self._handler_executor: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=_HANDLER_MAX_WORKERS,
            thread_name_prefix="eventbus-handler",
        )
        self._handler_futures: set[Future[None]] = set()
        self._handler_futures_lock = threading.Lock()
        self._shutdown = threading.Event()

    @property
    def eviction_count(self) -> int:
        """Total number of events evicted from the history ring buffer."""
        return self._eviction_count

    @property
    def pending_handler_count(self) -> int:
        """Return the number of subscriber handler callbacks still running.

        Returns:
            Count of accepted synchronous subscriber callbacks that have not
            completed yet.
        """
        with self._handler_futures_lock:
            self._prune_done_handler_futures_locked()
            return len(self._handler_futures)

    # -- public API --------------------------------------------------------

    def subscribe(
        self,
        event_type: type[Event],
        callback: Callable[[Event], None],
    ) -> str:
        """Register a callback for a specific event type.

        Args:
            event_type: The ``Event`` subclass to listen for.
            callback: Function invoked with the event instance when published.

        Returns:
            A unique subscription ID that can be passed to :meth:`unsubscribe`.
        """
        sub_id = uuid.uuid4().hex
        sub = _Subscription(
            subscription_id=sub_id,
            event_type=event_type,
            callback=callback,
        )
        with self._lock:
            self._subscriptions[sub_id] = sub
        logger.debug("Subscribed %s to %s", sub_id, event_type.__name__)
        return sub_id

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove a subscription by its ID.

        Args:
            subscription_id: The ID returned by :meth:`subscribe`.

        Raises:
            KeyError: If the subscription ID is not found.
        """
        with self._lock:
            if subscription_id not in self._subscriptions:
                raise KeyError(f"Subscription not found: {subscription_id}")
            del self._subscriptions[subscription_id]
        logger.debug("Unsubscribed %s", subscription_id)

    def publish(self, event: Event) -> None:
        """Publish an event and schedule matching subscribers for delivery.

        Matching callbacks run on the tracked handler executor so a slow
        subscriber does not block the publisher thread. Subscriber exceptions
        are caught and logged when the handler future completes.

        Args:
            event: The event instance to publish.

        Raises:
            RuntimeError: If the bus has already been shut down.
        """
        if self._shutdown.is_set():
            raise RuntimeError("EventBus is shut down")
        # Deep-copy the event before storing so that subscribers mutating
        # mutable payload fields (e.g. metadata dicts) cannot corrupt the
        # history ring buffer.
        stored_event = copy.deepcopy(event)
        with self._lock:
            if len(self._history) == _HISTORY_MAX_LENGTH:
                self._eviction_count += 1
                if self._eviction_count % 100 == 1:
                    logger.warning(
                        "EventBus history full (%d max) — evicting oldest event (total evictions: %d)",
                        _HISTORY_MAX_LENGTH,
                        self._eviction_count,
                    )
            self._history.append(stored_event)
            matching = [sub for sub in self._subscriptions.values() if isinstance(event, sub.event_type)]

        for sub in matching:
            self._invoke_handler(sub, event)

    def drain_handlers(self, timeout: float | None = None) -> int:
        """Wait for accepted synchronous subscriber callbacks to finish.

        Args:
            timeout: Maximum seconds to wait for the current pending handler
                set. ``None`` waits until every currently pending callback
                completes.

        Returns:
            Number of handler callbacks still pending after the wait.
        """
        with self._handler_futures_lock:
            self._prune_done_handler_futures_locked()
            pending = set(self._handler_futures)

        if pending:
            wait(pending, timeout=timeout)

        return self.pending_handler_count

    def _invoke_handler(self, sub: _Subscription, event: Event) -> None:
        """Run a subscriber callback through the tracked handler executor."""
        executor = self._ensure_handler_executor()
        future = submit_with_context(executor, sub.callback, event, require_correlation=False)
        with self._handler_futures_lock:
            self._handler_futures.add(future)
        future.add_done_callback(lambda done: self._handle_handler_done(done, sub, event))

    def _ensure_handler_executor(self) -> ThreadPoolExecutor:
        """Return a live handler executor, recreating it after shutdown."""
        with self._handler_futures_lock:
            if self._handler_executor is None:
                self._handler_executor = ThreadPoolExecutor(
                    max_workers=_HANDLER_MAX_WORKERS,
                    thread_name_prefix="eventbus-handler",
                )
            return self._handler_executor

    def _handle_handler_done(self, future: Future[None], sub: _Subscription, event: Event) -> None:
        """Remove a completed handler future and log callback failures."""
        with self._handler_futures_lock:
            self._handler_futures.discard(future)
        try:
            future.result()
        except CancelledError:
            logger.warning(
                "Subscriber %s handler cancelled for event %s during EventBus shutdown",
                sub.subscription_id,
                event.event_type,
            )
        except Exception:
            logger.exception(
                "Subscriber %s raised an exception for event %s",
                sub.subscription_id,
                event.event_type,
            )

    def _prune_done_handler_futures_locked(self) -> None:
        """Drop completed handler futures while the futures lock is held."""
        self._handler_futures = {future for future in self._handler_futures if not future.done()}

    def _ensure_async_worker(self) -> None:
        """Start the single async dispatch worker thread if not running.

        MUST be called while ``self._lock`` is held. The caller is responsible
        for holding the lock before invoking this method.
        """
        if self._async_worker is not None and self._async_worker.is_alive():
            return
        self._async_worker = threading.Thread(
            target=self._async_dispatch_loop,
            daemon=True,
            name="eventbus-async",
        )
        self._async_worker.start()

    def _async_dispatch_loop(self) -> None:
        """Single worker thread that drains the async event queue."""
        while True:
            timed_out = False
            item = None
            try:
                item = self._async_queue.get(timeout=QUEUE_TIMEOUT)
            except queue.Empty:
                # Normal poll timeout — no event to dispatch; loop and check shutdown flag.
                timed_out = True
            if timed_out:
                if self._shutdown.is_set():
                    return
                continue
            if item is None:
                with contextlib.suppress(ValueError):
                    self._async_queue.task_done()
                # Sentinel — shutdown() was called; exit immediately.
                return
            event, matching = item
            for sub in matching:
                try:
                    sub.callback(event)
                except Exception:
                    logger.exception(
                        "Async subscriber %s raised an exception for event %s",
                        sub.subscription_id,
                        event.event_type,
                    )
            with contextlib.suppress(ValueError):
                self._async_queue.task_done()

    def shutdown(self) -> None:
        """Signal the async worker thread to stop and wait for it to exit.

        Unlike :meth:`clear`, this does not remove subscriptions or history.
        Call this during application shutdown to release the background thread
        cleanly before the process exits.
        """
        self._shutdown.set()
        # Put a sentinel so the worker unblocks immediately instead of waiting
        # for the 1-second queue.get() timeout.
        with contextlib.suppress(queue.Full):
            self._async_queue.put(None, timeout=QUEUE_TIMEOUT)
        if self._async_worker is not None and self._async_worker.is_alive():
            self._async_worker.join(timeout=THREAD_JOIN_TIMEOUT)
        self._async_worker = None
        self.drain_handlers(timeout=THREAD_JOIN_TIMEOUT_SHORT)
        with self._handler_futures_lock:
            executor = self._handler_executor
            self._handler_executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)

    def publish_async(self, event: Event) -> None:
        """Publish an event, invoking all matching subscribers in a background thread.

        The history is updated immediately (under lock) before dispatching
        callbacks asynchronously via a single reusable worker thread. The
        async queue uses a bounded put with a short timeout so the caller
        is never blocked indefinitely when the worker falls behind.

        Args:
            event: The event instance to publish.

        Raises:
            RuntimeError: If the bus has already been shut down.
        """
        if self._shutdown.is_set():
            raise RuntimeError("EventBus is shut down")
        # Deep-copy before storing in history — same reason as publish().
        stored_event = copy.deepcopy(event)
        with self._lock:
            if len(self._history) == _HISTORY_MAX_LENGTH:
                self._eviction_count += 1
                if self._eviction_count % 100 == 1:
                    logger.warning(
                        "EventBus history full (%d max) — evicting oldest event (total evictions: %d)",
                        _HISTORY_MAX_LENGTH,
                        self._eviction_count,
                    )
            self._history.append(stored_event)
            matching = [sub for sub in self._subscriptions.values() if isinstance(event, sub.event_type)]

            if matching:
                try:
                    self._async_queue.put((event, matching), timeout=QUEUE_TIMEOUT)
                except queue.Full:
                    logger.warning(
                        "EventBus async queue full (size=%d) — dropping dispatch for event type '%s'",
                        EVENTBUS_ASYNC_QUEUE_SIZE,
                        event.event_type,
                    )
                    return
                self._ensure_async_worker()

    def clear(self) -> None:
        """Remove all subscriptions and history, stop async worker.

        Intended for use in test teardown to avoid cross-test interference.
        Resets the shutdown event so the bus can be reused after clearing.
        """
        self._shutdown.set()
        with contextlib.suppress(queue.Full):
            self._async_queue.put(None, timeout=QUEUE_TIMEOUT)
        if self._async_worker is not None and self._async_worker.is_alive():
            self._async_worker.join(timeout=THREAD_JOIN_TIMEOUT_SHORT)
        self._async_worker = None
        self.drain_handlers(timeout=THREAD_JOIN_TIMEOUT_SHORT)
        with self._handler_futures_lock:
            executor = self._handler_executor
            self._handler_executor = None
            self._handler_futures.clear()
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)  # noqa: leak-rule-3 -- handlers were drained above via drain_handlers(); cancel_futures signals stop. wait=False keeps EventBus shutdown from hanging on a stuck handler.
        # Drain any remaining items from the queue
        while not self._async_queue.empty():
            try:
                self._async_queue.get_nowait()
                with contextlib.suppress(ValueError):
                    self._async_queue.task_done()
            except queue.Empty:
                break
        with self._lock:
            self._subscriptions.clear()
            self._history.clear()
            self._eviction_count = 0
        # Reset the shutdown flag so the bus is usable again after clearing.
        # Without this, the next publish_async call would find _shutdown already
        # set and the worker thread would exit immediately on start.
        self._shutdown.clear()

    def get_history(
        self,
        event_type: type[Event] | None = None,
        limit: int = 100,
    ) -> list[Event]:
        """Return recent events, optionally filtered by type.

        Args:
            event_type: If provided, only events that are instances of this
                type are returned. ``None`` returns all event types.
            limit: Maximum number of events to return (must be positive).
                Newest events appear last (chronological order).

        Returns:
            A list of up to *limit* events in chronological order.

        Raises:
            ValueError: If *limit* is not a positive integer.
        """
        if limit <= 0:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")
        with self._lock:
            if event_type is None:
                items = list(self._history)
            else:
                items = [e for e in self._history if isinstance(e, event_type)]
        return items[-limit:]


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_event_bus: EventBus | None = None
_singleton_lock = threading.Lock()


def _log_event_for_observability(event: Event) -> None:
    """Log every published event at INFO level for operational observability.

    This is the mandatory subscriber that ensures the EventBus is not
    write-only.  All published events are captured here so operators can
    observe system activity through standard log aggregation tools.

    Args:
        event: The event that was published.
    """
    logger.info("[EventBus] %r", event)


def get_event_bus() -> EventBus:
    """Return the process-wide ``EventBus`` singleton.

    Thread-safe; the instance is created on first call.  The first call also
    registers an observability subscriber that logs every published event at
    INFO level, ensuring no event is published without a reader.

    Returns:
        The shared ``EventBus`` instance.
    """
    global _event_bus
    if _event_bus is None:
        with _singleton_lock:
            if _event_bus is None:
                _event_bus = EventBus()
                # Register the observability subscriber so every published event
                # is logged.  This satisfies the requirement that the EventBus
                # has at least one subscriber.
                _event_bus.subscribe(Event, _log_event_for_observability)
    return _event_bus


def reset_event_bus() -> None:
    """Destroy the current singleton so the next call to :func:`get_event_bus` creates a fresh instance.

    Intended for test isolation. Also calls :meth:`EventBus.clear` on the
    existing instance before discarding it.
    """
    global _event_bus
    with _singleton_lock:
        if _event_bus is not None:
            _event_bus.shutdown()
            _event_bus.clear()
        _event_bus = None
