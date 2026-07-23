"""Process-exit signal handling for WorkbenchScheduler leases."""

from __future__ import annotations

import atexit
import logging
import signal
import threading
import weakref
from collections.abc import Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)
_install_lock = threading.Lock()
_signal_handlers_installed = False


class SignalLeaseScheduler(Protocol):
    """Scheduler surface required by the process-signal lifecycle."""

    _previous_sigint_handler: Any
    _previous_sigterm_handler: Any
    _sigint_handler: Callable[[int, Any], None] | None
    _sigterm_handler: Callable[[int, Any], None] | None

    def release_all_leases(self, *, outcome: str = "preempted") -> None:
        """Release all leases still owned by this scheduler."""


_registered_scheduler: SignalLeaseScheduler | None = None
_registered_schedulers: weakref.WeakSet[SignalLeaseScheduler] = weakref.WeakSet()


def release_registered_scheduler_leases(*, outcome: str = "preempted") -> bool:
    """Release leases held by the process-global WorkbenchScheduler, if any.

    Returns:
        True when a scheduler was registered and release was attempted.
    """
    schedulers = list(_registered_schedulers)
    if not schedulers and _registered_scheduler is not None:
        schedulers = [_registered_scheduler]
    if not schedulers:
        return False
    for scheduler in schedulers:
        scheduler.release_all_leases(outcome=outcome)
    return True


def install_signal_handlers(scheduler: SignalLeaseScheduler) -> None:
    """Install SIGINT, SIGTERM, and atexit release hooks once per process.

    Raises:
        RuntimeError: If validation cannot complete.
    """
    global _registered_scheduler, _signal_handlers_installed

    with _install_lock:
        _registered_scheduler = scheduler
        _registered_schedulers.add(scheduler)
        if _signal_handlers_installed:
            return
        if threading.current_thread() is not threading.main_thread():
            logger.debug("defer scheduler signal-handler installation outside the main thread")
            return
        scheduler._previous_sigint_handler = signal.getsignal(signal.SIGINT)
        scheduler._previous_sigterm_handler = signal.getsignal(signal.SIGTERM)

        def _release_registered() -> None:
            release_registered_scheduler_leases()

        def _sigint_handler(signum: int, frame: Any) -> None:
            _release_registered()
            previous = scheduler._previous_sigint_handler
            if callable(previous):
                previous(signum, frame)
                return
            raise KeyboardInterrupt

        def _sigterm_handler(signum: int, frame: Any) -> None:
            _release_registered()
            previous = scheduler._previous_sigterm_handler
            if callable(previous):
                previous(signum, frame)
                return
            raise SystemExit(0)

        scheduler._sigint_handler = _sigint_handler
        scheduler._sigterm_handler = _sigterm_handler
        signal.signal(signal.SIGINT, _sigint_handler)
        signal.signal(signal.SIGTERM, _sigterm_handler)
        atexit.register(_release_registered)
        _signal_handlers_installed = True


def signal_handlers_installed_state() -> bool:
    """Return whether scheduler process-exit handlers are installed.

    Returns:
        True after this module has registered SIGINT, SIGTERM, and process-exit
        lease-release hooks; otherwise False.
    """
    return _signal_handlers_installed


__all__ = ["install_signal_handlers", "release_registered_scheduler_leases", "signal_handlers_installed_state"]
