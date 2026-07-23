"""Graceful shutdown handler for the Vetinari process.

Registers signal handlers (SIGTERM, SIGINT) and atexit callbacks to
cleanly drain in-flight work, close HTTP sessions, shut down schedulers,
and flush caches before the process exits.

Usage::

    from vetinari.shutdown import register_shutdown_handlers
    register_shutdown_handlers()  # call once at startup
"""

from __future__ import annotations

import atexit
import collections.abc
import contextlib
import logging
import signal
import sys
import threading

logger = logging.getLogger(__name__)


# Guards against running shutdown logic more than once
_shutdown_started = threading.Event()
_handlers_registered = threading.Event()
_signals_registered = threading.Event()
_callbacks: list[tuple[str, collections.abc.Callable[[], None]]] = []
_callbacks_lock = threading.Lock()
_logging_raise_exceptions_before_shutdown: bool | None = None


def _ignore_logging_error(_record: logging.LogRecord) -> None:
    """Suppress handler errors during interpreter shutdown."""


def register_callback(name: str, fn: collections.abc.Callable[[], None]) -> None:
    """Register a named cleanup callback to run on shutdown.

    Callbacks are executed in LIFO order (last registered runs first).

    Args:
        name: Human-readable label for logging (e.g. "APScheduler").
        fn: Zero-argument callable to invoke during shutdown.
    """
    with _callbacks_lock:
        for index, (registered_name, _registered_fn) in enumerate(_callbacks):
            if registered_name == name:
                _callbacks[index] = (name, fn)
                logger.debug("Shutdown callback updated: %s", name)
                return
        _callbacks.append((name, fn))
    logger.debug("Shutdown callback registered: %s", name)


def unregister_callback(name: str, fn: collections.abc.Callable[[], None] | None = None) -> None:
    """Remove a previously registered shutdown callback.

    Args:
        name: Human-readable callback label.
        fn: Optional callable identity. When omitted, all callbacks with
            ``name`` are removed.
    """
    with _callbacks_lock:
        _callbacks[:] = [
            (registered_name, registered_fn)
            for registered_name, registered_fn in _callbacks
            if registered_name != name or (fn is not None and registered_fn != fn)
        ]


def _safe_log(level: int, msg: str, *args: object) -> None:
    """Log a message, silently ignoring errors from closed streams.

    During process exit, logging handlers may have already been torn down
    by the interpreter.  This avoids noisy ``ValueError: I/O operation on
    closed file`` tracebacks that would otherwise pollute test output.

    Args:
        level: Logging level constant (e.g. ``logging.INFO``).
        msg: Log message with %-style placeholders.
        *args: Substitution arguments for *msg*.
    """
    handlers = [*logger.handlers, *logging.root.handlers]
    for handler in handlers:
        stream = getattr(handler, "stream", None)
        if stream is not None and getattr(stream, "closed", False):
            return
    with contextlib.suppress(ValueError, OSError):
        logger.log(level, msg, *args)


def _install_safe_handlers() -> None:
    """Silence logging errors during interpreter shutdown.

    ``StreamHandler.emit()`` catches write errors internally and calls
    ``handleError(record)`` which prints tracebacks to stderr.  During
    interpreter teardown the underlying streams are closed, producing
    noisy ``ValueError: I/O operation on closed file`` output.

    This replaces ``handleError`` on every root handler with a no-op so
    those tracebacks are silently swallowed.
    """
    global _logging_raise_exceptions_before_shutdown
    if _logging_raise_exceptions_before_shutdown is None:
        _logging_raise_exceptions_before_shutdown = logging.raiseExceptions
    logging.raiseExceptions = False

    loggers: list[logging.Logger] = [logging.root]
    loggers.extend(
        candidate for candidate in logging.root.manager.loggerDict.values() if isinstance(candidate, logging.Logger)
    )
    for current_logger in loggers:
        for handler in current_logger.handlers:
            handler.__dict__["handleError"] = _ignore_logging_error


def shutdown() -> None:
    """Execute all registered shutdown callbacks.

    Safe to call multiple times — only the first invocation runs the
    callbacks.  Subsequent calls are no-ops.
    """
    if _shutdown_started.is_set():
        return
    _shutdown_started.set()

    # Protect against closed-stream errors from callbacks that log directly
    _install_safe_handlers()

    _safe_log(logging.INFO, "Graceful shutdown initiated")

    with _callbacks_lock:
        callbacks = list(reversed(_callbacks))

    for name, fn in callbacks:
        try:
            _safe_log(logging.INFO, "Shutting down: %s", name)
            fn()
        except Exception:
            _safe_log(logging.WARNING, "Error during shutdown of %s", name)

    _safe_log(logging.INFO, "Graceful shutdown complete")


def _signal_handler(signum: int, _frame: object) -> None:
    """Handle SIGTERM/SIGINT by running shutdown and exiting."""
    sig_name = signal.Signals(signum).name
    logger.info("Received %s — initiating shutdown", sig_name)
    shutdown()
    sys.exit(0)


def _register_http_cleanup_callback() -> None:
    """Register HTTP session cleanup with duplicate-name replacement."""
    from vetinari.http import close_all as _close_http

    register_callback("HTTP sessions", _close_http)


def _register_signal_handlers_once() -> None:
    """Install process signal handlers when called from the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return
    if _signals_registered.is_set():
        return
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)
        _signals_registered.set()
        logger.debug("Signal handlers registered (SIGTERM, SIGINT)")
    except (OSError, ValueError):
        # Some environments (e.g. Windows services) don't support signals
        logger.warning("Signal handler registration skipped (unsupported)")


def register_shutdown_handlers() -> None:
    """Register signal handlers and atexit callback.

    Call this once during application startup (e.g. in ``cli.py`` or
    ``web_ui.py`` before ``app.run()``).

    Registers:
        - SIGTERM handler (container/systemd stop)
        - SIGINT handler (Ctrl+C)
        - atexit callback (normal Python exit)
        - HTTP session cleanup (from ``vetinari.http``)
    """
    with _callbacks_lock:
        if _handlers_registered.is_set():
            logger.debug("Shutdown handlers already registered")
            already_registered = True
        else:
            _handlers_registered.set()
            already_registered = False

    if already_registered:
        _register_signal_handlers_once()
        _register_http_cleanup_callback()
        return

    # atexit always works — signals may not on all platforms
    atexit.register(shutdown)

    _register_signal_handlers_once()

    _register_http_cleanup_callback()

    logger.info("Shutdown handlers registered")


def reset() -> None:
    """Reset shutdown state.  For testing only."""
    global _logging_raise_exceptions_before_shutdown
    if _logging_raise_exceptions_before_shutdown is not None:
        logging.raiseExceptions = _logging_raise_exceptions_before_shutdown
        _logging_raise_exceptions_before_shutdown = None
    _shutdown_started.clear()
    _handlers_registered.clear()
    _signals_registered.clear()
    with _callbacks_lock:
        _callbacks.clear()
