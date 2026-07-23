"""Bounded local SSE tee for AM Engine agent streams.

The adapter remains the single inference owner. This module only mirrors its
already-authorized stream to the Rust kernel and forwards an explicit user
cancel to the active engine stream.
"""

from __future__ import annotations

import atexit
import json
import logging
import threading
from collections import deque
from collections.abc import Iterator, Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Cross-side pin consumed by AMW_KERNEL_ENGINE_STREAM_TEE_URL in amw-kernel.
_STREAM_TEE_PORT = 8639
# Cross-side pin consumed by AMW_KERNEL_ENGINE_STREAM_TEE_URL in amw-kernel.
_STREAM_TEE_HOST = "127.0.0.1"
_STREAM_TEE_DEFAULT_URL = f"http://{_STREAM_TEE_HOST}:{_STREAM_TEE_PORT}"
_SCROLLBACK_LIMIT = 512
logger = logging.getLogger(__name__)

_lock = threading.Lock()
_condition = threading.Condition(_lock)
_server: ThreadingHTTPServer | None = None
_server_thread: threading.Thread | None = None
_active_stream: Any | None = None
_events: deque[tuple[int, str]] = deque(maxlen=_SCROLLBACK_LIMIT)
_next_sequence = 1
_terminal = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        """Serve the bounded SSE mirror to an observing kernel client."""
        if self.path != "/agent-stream":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        cursor = 0
        try:
            while True:
                with _condition:
                    _condition.wait_for(
                        lambda current=cursor: any(item[0] > current for item in _events) or _terminal,
                        timeout=15,
                    )
                    pending = [item for item in _events if item[0] > cursor]
                    terminal = _terminal
                for event_sequence, payload in pending:
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
                    cursor = event_sequence
                if terminal and not pending:
                    return
                if not pending:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Observer disconnect is intentionally isolated from generation cancel.
            logger.warning("AM Engine stream observer disconnected")
            return

    def do_POST(self) -> None:
        """Forward one explicit user cancellation to the active stream."""
        if self.path != "/agent-stream/cancel":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        with _lock:
            stream = _active_stream
        if stream is None:
            self._json(HTTPStatus.CONFLICT, {"cancelled": False, "reason": "no active generation"})
            return
        try:
            acknowledgement = stream.cancel()
        except Exception as exc:
            logger.warning("AM Engine stream cancellation failed: %s", exc)
            self._json(HTTPStatus.BAD_GATEWAY, {"cancelled": False, "reason": str(exc)})
            return
        self._json(HTTPStatus.OK, {"cancelled": True, "acknowledgement": acknowledgement})

    def _json(self, status: HTTPStatus, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def ensure_server() -> str:
    """Lazily start the single daemonized loopback tee server.

    Returns:
        The loopback origin consumed by the kernel proxy.
    """
    global _server, _server_thread
    with _lock:
        if _server is None:
            server = ThreadingHTTPServer((_STREAM_TEE_HOST, _STREAM_TEE_PORT), _Handler)
            server.daemon_threads = True
            thread = threading.Thread(target=server.serve_forever, name="am-engine-stream-tee", daemon=True)
            _server = server
            _server_thread = thread
            thread.start()
    return _STREAM_TEE_DEFAULT_URL


def mirror(stream: Any) -> Iterator[Mapping[str, Any]]:
    """Mirror a typed engine stream while yielding its payloads to the caller.

    Yields:
        Versioned event payloads emitted by the shared engine stream.

    Raises:
        RuntimeError: If another generation already owns the tee.
    """
    global _active_stream, _next_sequence, _terminal
    ensure_server()
    with _condition:
        if _active_stream is not None:
            raise RuntimeError("an AM Engine stream is already active")
        _active_stream = stream
        _events.clear()
        _next_sequence = 1
        _terminal = False
        _condition.notify_all()
    try:
        with stream:
            for event in stream:
                payload = dict(event.payload)
                encoded = json.dumps(payload, separators=(",", ":"))
                with _condition:
                    _events.append((_next_sequence, encoded))
                    _next_sequence += 1
                    _condition.notify_all()
                yield payload
    finally:
        with _condition:
            _active_stream = None
            _terminal = True
            _condition.notify_all()


def shutdown() -> None:
    """Stop the tee server without waiting while holding the lifecycle lock."""
    global _server, _server_thread
    with _lock:
        server, thread = _server, _server_thread
        _server = None
        _server_thread = None
    if server is not None:
        server.shutdown()
        server.server_close()
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)


atexit.register(shutdown)
