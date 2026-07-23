"""Thread-pool lifecycle helpers with explicit drain and closed-state guards."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import TypeVar

T = TypeVar("T")


class ManagedThreadPool:
    """Thread pool wrapper with explicit drain and fail-closed shutdown."""

    def __init__(self, max_workers: int, *, thread_name_prefix: str, drain_timeout: float = 5.0) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be at least 1")
        if drain_timeout <= 0:
            raise ValueError("drain_timeout must be positive")
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        self._drain_timeout = drain_timeout
        self._lock = threading.Lock()
        self._futures: set[Future[object]] = set()
        self._closed = False

    def submit(self, fn: Callable[..., T], *args: object, **kwargs: object) -> Future[T]:
        """Submit work unless the pool is closed.

        Args:
            fn: Callable to run in the managed executor.
            *args: Positional arguments passed to ``fn``.
            **kwargs: Keyword arguments passed to ``fn``.

        Returns:
            Future for the submitted work.

        Raises:
            RuntimeError: If the pool has already been shut down.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("thread pool is closed")
            future = self._executor.submit(fn, *args, **kwargs)
            self._futures.add(future)  # type: ignore[arg-type]
        future.add_done_callback(self._discard_future)
        return future

    def drain(self, timeout: float | None = None) -> int:
        """Wait for submitted work and return the number still pending.

        Args:
            timeout: Optional timeout override in seconds.

        Returns:
            Number of tasks still pending after the wait completes.
        """
        with self._lock:
            pending = set(self._futures)
        if not pending:
            return 0
        _, not_done = wait(pending, timeout=self._drain_timeout if timeout is None else timeout)
        return len(not_done)

    def shutdown(self, *, cancel_futures: bool = False) -> None:
        """Close the pool only after pending work has drained.

        Args:
            cancel_futures: Whether pending futures should be cancelled.

        Raises:
            TimeoutError: If pending work did not drain and cancellation was not requested.
        """
        pending = self.drain()
        if pending and not cancel_futures:
            raise TimeoutError(f"{pending} thread-pool task(s) still pending")
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=cancel_futures)

    def _discard_future(self, future: Future[object]) -> None:
        with self._lock:
            self._futures.discard(future)
