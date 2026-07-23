"""Checkpoint callback helpers for the Workbench scheduler."""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections.abc import Callable

from vetinari.runtime.workbench_scheduler_types import Lane, WorkbenchSchedulerCapacityRetryExceeded

logger = logging.getLogger(__name__)
_checkpoint_context = threading.local()


def checkpoint_cancel_requested() -> bool:
    """Return whether the current checkpoint callback was asked to stop.

    Returns:
        True when the active checkpoint context has a cancellation request.
    """
    cancel_event = getattr(_checkpoint_context, "cancel_event", None)
    return bool(cancel_event is not None and cancel_event.is_set())


def run_checkpoint_with_timeout(
    *,
    checkpoint_fn: Callable[[], None] | None,
    timeout_s: float,
    lane: Lane | None,
    caller: str,
    checkpoint_threads: dict[int, tuple[threading.Thread, threading.Event]],
    checkpoint_threads_lock: threading.Lock,
) -> None:
    """Run a checkpoint callback and fail closed on timeout.

    Args:
        checkpoint_fn: Callback supplied by the training caller.
        timeout_s: Maximum seconds to wait.
        lane: Lane owning the checkpoint callback.
        caller: Caller subsystem for diagnostics.
        checkpoint_threads: In-flight checkpoint threads by thread id.
        checkpoint_threads_lock: Lock protecting checkpoint_threads.

    Raises:
        WorkbenchSchedulerCapacityRetryExceeded: If the checkpoint callback times out.
    """
    if checkpoint_fn is None:
        return

    cancel_event = threading.Event()
    errors: list[BaseException] = []

    def _target() -> None:
        _checkpoint_context.cancel_event = cancel_event
        try:
            invoke_checkpoint_callback(checkpoint_fn, cancel_event)
        except BaseException as exc:
            errors.append(exc)
        finally:
            if getattr(_checkpoint_context, "cancel_event", None) is cancel_event:
                del _checkpoint_context.cancel_event
            with checkpoint_threads_lock:
                checkpoint_threads.pop(id(thread), None)

    # Checkpoint callbacks are expected to honor ``cancel_event``, but a third-party
    # callback may block forever.  The scheduler rejects the preemption on timeout;
    # making this subordinate worker a daemon also prevents that broken callback
    # from holding interpreter shutdown hostage.
    thread = threading.Thread(target=_target, name="workbench-checkpoint", daemon=True)
    with checkpoint_threads_lock:
        checkpoint_threads[id(thread)] = (thread, cancel_event)
    thread.start()
    thread.join(timeout=max(0.0, timeout_s))
    if thread.is_alive():
        lane_name = lane.value if isinstance(lane, Lane) else "unknown"
        cancel_event.set()
        thread.join(timeout=min(1.0, max(0.0, timeout_s)))
        logger.warning(
            "checkpoint callback timed out for lane=%s caller=%s; refusing preempt checkpoint",
            lane_name,
            caller,
        )
        raise WorkbenchSchedulerCapacityRetryExceeded(
            f"checkpoint callback timed out for lane={lane_name} caller={caller}"
        )
    with checkpoint_threads_lock:
        checkpoint_threads.pop(id(thread), None)
    if errors:
        raise errors[0]
    logger.debug("checkpoint callback completed for caller=%s", caller)


def invoke_checkpoint_callback(checkpoint_fn: Callable[[], None], cancel_event: threading.Event) -> None:
    """Invoke a checkpoint callback with optional cancellation support.

    Args:
        checkpoint_fn: Callback to invoke.
        cancel_event: Cancellation event passed to callbacks that accept one required argument.
    """
    try:
        signature = inspect.signature(checkpoint_fn)
    except (TypeError, ValueError) as exc:
        logger.warning("Checkpoint callback signature unavailable; invoking without cancellation event: %s", exc)
        checkpoint_fn()
        return
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind
        in {
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
        and parameter.default is inspect.Parameter.empty
    ]
    if len(positional) == 1:
        checkpoint_fn(cancel_event)  # type: ignore[call-arg]
        return
    checkpoint_fn()


def cancel_and_drain_checkpoint_threads(
    *,
    checkpoint_threads: dict[int, tuple[threading.Thread, threading.Event]],
    checkpoint_threads_lock: threading.Lock,
    timeout_s: float,
) -> None:
    """Cancel tracked checkpoint threads and remove drained entries.

    Args:
        checkpoint_threads: Checkpoint threads value consumed by cancel_and_drain_checkpoint_threads().
        checkpoint_threads_lock: Checkpoint threads lock value consumed by cancel_and_drain_checkpoint_threads().
        timeout_s: Timeout value controlling how long the operation may wait.
    """
    with checkpoint_threads_lock:
        tracked = list(checkpoint_threads.items())
    deadline = time.monotonic() + max(0.0, timeout_s)
    for _thread_id, (thread, cancel_event) in tracked:
        cancel_event.set()
        remaining = max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)
    with checkpoint_threads_lock:
        checkpoint_threads.clear()
        checkpoint_threads.update({
            thread_id: (thread, cancel_event) for thread_id, (thread, cancel_event) in tracked if thread.is_alive()
        })


__all__ = [
    "cancel_and_drain_checkpoint_threads",
    "checkpoint_cancel_requested",
    "invoke_checkpoint_callback",
    "run_checkpoint_with_timeout",
]
