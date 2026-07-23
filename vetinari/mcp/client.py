"""MCP Client for consuming external MCP tools.

Connects to MCP-compatible servers via stdio subprocess transport,
enabling Vetinari to use tools exposed by external MCP servers.
"""

from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
from subprocess import PIPE
from types import TracebackType
from typing import Any

from vetinari.constants import MCP_PROCESS_TIMEOUT
from vetinari.exceptions import MCPError
from vetinari.mcp.config import build_mcp_subprocess_env, validate_mcp_launch_command
from vetinari.mcp.protocol import DEFAULT_PROTOCOL_VERSION, is_supported_protocol_version

logger = logging.getLogger(__name__)


# JSON-RPC protocol version used for all requests.
_JSONRPC_VERSION = "2.0"

# Latest MCP protocol version this client targets during initialization.
_MCP_PROTOCOL_VERSION = DEFAULT_PROTOCOL_VERSION

# Seconds to wait for the MCP server to respond before raising a timeout error.
_READLINE_TIMEOUT = 30.0
_REQUEST_TIMEOUT = 30.0
_MAX_FRAMES_PER_REQUEST = 100


class MCPClient:
    """Client that connects to an external MCP server via stdio subprocess transport.

    Launches an MCP-compatible server as a child process and communicates
    with it using JSON-RPC 2.0 messages over stdin/stdout.

    Example:
        >>> with MCPClient(["npx", "-y", "@modelcontextprotocol/server-filesystem"]) as client:
        ...     client.initialize()
        ...     tools = client.list_tools()
        ...     result = client.call_tool("read_file", {"path": "/tmp/example.txt"})
    """

    def __init__(self, command: list[str], env: dict[str, str] | None = None) -> None:
        """Initialise the client with a server launch command.

        Args:
            command: The command and arguments used to launch the MCP server
                subprocess (e.g. ``["npx", "-y", "@modelcontextprotocol/server-filesystem"]``).
            env: Optional environment variables to pass to the subprocess. If
                ``None``, a minimal MCP allowlist is used.
        """
        self._command = command
        self._env = env if env is not None else build_mcp_subprocess_env()
        self._process: subprocess.Popen[bytes] | None = None
        self._next_id: int = 0
        self._capabilities: dict[str, Any] = {}
        self._rpc_lock = threading.RLock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch the MCP server subprocess and prepare for communication.

        Raises:
            MCPError: If the subprocess cannot be started.
        """
        try:
            validate_mcp_launch_command(self._command)
        except ValueError as exc:
            raise MCPError(f"Unsafe MCP server command: {exc}") from exc

        try:
            self._process = subprocess.Popen(
                self._command,
                stdin=PIPE,
                stdout=PIPE,
                stderr=subprocess.DEVNULL,
                env=self._env,
            )
        except OSError as exc:
            raise MCPError(
                f"Failed to start MCP server process: {exc}",
            ) from exc
        self._next_id = 0
        logger.info("MCP server process started (pid=%s)", self._process.pid)

    def stop(self) -> None:
        """Terminate the MCP server subprocess gracefully.

        Closes stdin first so the server sees EOF, then sends SIGTERM and
        waits up to ``MCP_PROCESS_TIMEOUT`` seconds before force-killing.
        """
        if self._process is None:
            return
        logger.info("Stopping MCP server (pid=%s)", self._process.pid)
        try:
            # Close stdin first so the server receives EOF and can exit cleanly
            # before we escalate to SIGTERM/SIGKILL.
            if self._process.stdin is not None and not self._process.stdin.closed:
                self._process.stdin.close()
            self._process.terminate()
            try:
                self._process.wait(timeout=MCP_PROCESS_TIMEOUT)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "MCP server did not exit after SIGTERM; sending SIGKILL",
                )
                self._process.kill()
                self._process.wait()
        except OSError as exc:
            logger.exception("Error while stopping MCP server: %s", exc)
        finally:
            self._process = None

    def __enter__(self) -> MCPClient:
        """Start the server and return this client as the context value.

        Returns:
            This ``MCPClient`` instance after calling ``start()``.
        """
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Stop the server when leaving the context block."""
        self.stop()

    # ------------------------------------------------------------------
    # Internal JSON-RPC helpers
    # ------------------------------------------------------------------

    def _require_started(self) -> subprocess.Popen[bytes]:
        """Return the active subprocess or raise MCPError if not started.

        Returns:
            The active ``subprocess.Popen`` instance.

        Raises:
            MCPError: If ``start()`` has not been called.
        """
        if self._process is None:
            raise MCPError(
                "MCPClient is not started. Call start() or use as a context manager.",
            )
        return self._process

    def _send_request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a JSON-RPC 2.0 request with serialized stdio access."""
        with self._rpc_lock:
            return self._send_request_locked(method, params)

    def _send_request_locked(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a JSON-RPC 2.0 request and return the parsed response.

        Increments the internal request ID counter, serialises the request as
        a newline-delimited JSON line, writes it to the subprocess stdin, reads
        response lines from stdout, skipping server-initiated notifications,
        until the matching response arrives.

        Each read uses a bounded daemon reader thread so a hung server cannot
        stall the caller indefinitely. Notification frames (``method`` present,
        ``id`` absent) are logged and skipped; the loop continues reading until
        a response frame whose ``id`` matches ``request_id`` is received.

        Args:
            method: The JSON-RPC method name (e.g. ``"tools/list"``).
            params: Optional parameter mapping for the method.

        Returns:
            The ``result`` field from the JSON-RPC success response.

        Raises:
            MCPError: If the subprocess is not started, if communication fails,
                if the server does not respond within 30 seconds on any read, or
                if the server returns a JSON-RPC error object.
            RuntimeError: If the response is not a JSON object or the response
                ID does not match the request ID.
        """
        process = self._require_started()
        request_id, request = self._build_request(method, params)
        logger.debug("MCP -> %s (id=%s)", method, request_id)

        self._write_request_frame(process, request)

        # stdout is guaranteed non-None by Popen(stdout=PIPE); assert once
        # outside the loop so it is not repeated on every iteration.
        assert process.stdout is not None  # guarded by Popen(stdout=PIPE)

        # Read frames in a loop, skipping server-initiated notifications.
        # Each iteration uses a bounded daemon reader thread so the timeout
        # applies independently to every readline() call.
        # The total deadline and frame cap keep notification streams from
        # extending one request indefinitely.
        deadline = time.monotonic() + _REQUEST_TIMEOUT
        frames_seen = 0
        while frames_seen < _MAX_FRAMES_PER_REQUEST:
            response = self._read_response_frame(process, deadline, method, request_id)
            frames_seen += 1
            if self._is_ignorable_response_frame(response, method, request_id):
                continue
            if "error" in response:
                error = response["error"]
                code = error.get("code", "unknown")
                message = error.get("message", "unknown error")
                raise MCPError(
                    f"MCP server returned error (code={code}): {message}",
                )

            logger.debug("MCP <- %s (id=%s) OK", method, request_id)
            return response.get("result", {})

        self.stop()
        raise MCPError(
            f"MCP server exceeded {_MAX_FRAMES_PER_REQUEST} frames before responding (method={method}, id={request_id})"
        )

    def _build_request(self, method: str, params: dict[str, Any] | None) -> tuple[int, dict[str, Any]]:
        """Allocate a request id and return a JSON-RPC request object."""
        request_id = self._next_id
        self._next_id += 1
        request: dict[str, Any] = {
            "jsonrpc": _JSONRPC_VERSION,
            "id": request_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params
        return request_id, request

    @staticmethod
    def _write_request_frame(process: subprocess.Popen[bytes], request: dict[str, Any]) -> None:
        """Write one newline-delimited JSON-RPC request frame."""
        raw_request = json.dumps(request) + "\n"
        try:
            assert process.stdin is not None  # guarded by Popen(stdin=PIPE)
            process.stdin.write(raw_request.encode("utf-8"))
            process.stdin.flush()
        except OSError as exc:
            raise MCPError(f"Failed to write to MCP server stdin: {exc}") from exc

    def _read_response_frame(
        self,
        process: subprocess.Popen[bytes],
        deadline: float,
        method: str,
        request_id: int,
    ) -> dict[str, Any]:
        """Read and validate one JSON-RPC response object from stdout."""
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stop()
                raise MCPError(
                    f"MCP server did not respond within {_REQUEST_TIMEOUT:.0f} seconds"
                    f" (method={method}, id={request_id})"
                )
            raw_response = self._readline_with_timeout(process, min(_READLINE_TIMEOUT, remaining), method, request_id)
        except OSError as exc:
            raise MCPError(f"Failed to read from MCP server stdout: {exc}") from exc

        if not raw_response:
            raise MCPError("MCP server closed stdout unexpectedly (no response received)")
        try:
            response = json.loads(raw_response.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise MCPError(f"Invalid JSON received from MCP server: {exc}") from exc
        if not isinstance(response, dict):
            raise RuntimeError(
                f"Expected JSON object response from MCP server, got"
                f" {type(response).__name__} (method={method}, id={request_id})"
            )
        return response

    @staticmethod
    def _is_ignorable_response_frame(response: dict[str, Any], method: str, request_id: int) -> bool:
        """Return True for notifications or responses belonging to another id."""
        if "method" in response and "id" not in response:
            logger.debug(
                "MCP received notification method=%s while waiting for id=%s - skipping",
                response.get("method"),
                request_id,
            )
            return True
        if response.get("id") != request_id:
            logger.debug(
                "MCP skipped response id=%s while waiting for id=%s (method=%s)",
                response.get("id"),
                request_id,
                method,
            )
            return True
        return False

    def _readline_with_timeout(
        self,
        process: subprocess.Popen[bytes],
        timeout: float,
        method: str,
        request_id: int,
    ) -> bytes:
        """Read one stdout frame with a bounded daemon reader thread."""
        assert process.stdout is not None
        result_queue: queue.Queue[bytes | BaseException] = queue.Queue(maxsize=1)

        def _reader() -> None:
            try:
                result_queue.put(process.stdout.readline())
            except BaseException as exc:
                result_queue.put(exc)

        reader = threading.Thread(target=_reader, name=f"mcp-read-{request_id}", daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive():
            self.stop()
            reader.join(0.2)
            raise MCPError(
                f"MCP server did not respond within {timeout:.0f} seconds (method={method}, id={request_id})"
            )

        item = result_queue.get_nowait()
        if isinstance(item, BaseException):
            raise OSError(str(item)) from item
        return item

    def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC 2.0 notification with serialized stdio access."""
        with self._rpc_lock:
            self._send_notification_locked(method, params)

    def _send_notification_locked(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC 2.0 notification (no response expected).

        Args:
            method: The JSON-RPC method name (e.g. ``"notifications/initialized"``).
            params: Optional parameter mapping for the notification.

        Raises:
            MCPError: If the subprocess is not started or if writing fails.
        """
        process = self._require_started()
        notification: dict[str, Any] = {
            "jsonrpc": _JSONRPC_VERSION,
            "method": method,
        }
        if params is not None:
            notification["params"] = params

        raw_notification = json.dumps(notification) + "\n"
        logger.debug("MCP notification -> %s", method)

        try:
            assert process.stdin is not None
            process.stdin.write(raw_notification.encode("utf-8"))
            process.stdin.flush()
        except OSError as exc:
            raise MCPError(
                f"Failed to send MCP notification '{method}': {exc}",
            ) from exc

    # ------------------------------------------------------------------
    # MCP protocol methods
    # ------------------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        """Perform the MCP handshake with the server.

        Sends the ``initialize`` request with client metadata and the target
        protocol version, validates that the server speaks the same protocol
        version, stores the server-reported capabilities, then sends the
        required ``notifications/initialized`` notification.

        Returns:
            The full server response to the ``initialize`` request, which
            includes ``protocolVersion``, ``capabilities``, and ``serverInfo``.

        Raises:
            MCPError: If the server rejects the handshake or communication fails.
            RuntimeError: If the server reports an unsupported protocol version.
        """
        params: dict[str, Any] = {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": "vetinari",
                "version": "1.0.0",
            },
        }
        result = self._send_request("initialize", params)
        self._capabilities = result.get("capabilities", {})

        # Reject servers that advertise a different protocol version â€” proceeding
        # with a mismatched version risks silent incompatibilities in method
        # semantics or required fields.
        server_version = result.get("protocolVersion", "")
        if not isinstance(server_version, str) or not is_supported_protocol_version(server_version):
            raise RuntimeError(f"MCP protocol version unsupported: {server_version}")

        logger.info(
            "MCP server initialized; protocolVersion=%s",
            server_version,
        )
        self._send_notification("notifications/initialized")
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        """Return the list of tools exposed by the MCP server.

        Returns:
            A list of tool descriptor dictionaries. Each dict typically
            contains ``name``, ``description``, and ``inputSchema`` keys.

        Raises:
            MCPError: If the server is not started or the request fails.
        """
        result = self._send_request("tools/list")
        tools: list[dict[str, Any]] = result.get("tools", [])
        logger.info("MCP server advertises %d tool(s)", len(tools))
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Invoke a named tool on the MCP server.

        Args:
            name: The tool name as reported by ``list_tools()``.
            arguments: Keyword arguments to pass to the tool. Defaults to an
                empty dict when ``None`` is provided.

        Returns:
            Concatenated text from all ``text``-type content blocks in the
            server response, joined by newlines. Returns an empty string if
            the server returns content blocks but none are of type ``text``.

        Raises:
            MCPError: If the server is not started, the request fails, or the
                response contains no content blocks.
        """
        params: dict[str, Any] = {
            "name": name,
            "arguments": arguments or {},
        }
        result = self._send_request("tools/call", params)
        if result.get("isError") is True:
            content_blocks = result.get("content", [])
            text = "\n".join(block.get("text", "") for block in content_blocks if block.get("type") == "text")
            raise MCPError(f"Tool '{name}' returned an MCP error response: {text or 'no error details'}")
        content_blocks: list[dict[str, Any]] = result.get("content", [])
        if not content_blocks:
            raise MCPError(
                f"Tool '{name}' returned no content blocks in its response",
            )
        # Concatenate all text-type blocks; non-text blocks (images, resources)
        # are skipped because their content is not representable as plain text.
        text = "\n".join(block.get("text", "") for block in content_blocks if block.get("type") == "text")
        logger.debug("Tool '%s' returned %d character(s)", name, len(text))
        return text

    def ping(self) -> bool:
        """Send a ping to verify the server is alive.

        Returns:
            ``True`` if the server responds successfully, ``False`` if the
            ping fails for any reason.
        """
        try:
            self._send_request("ping")
            return True
        except MCPError as exc:
            logger.warning("MCP ping failed: %s", exc)
            return False
