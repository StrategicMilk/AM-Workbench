"""MCP Server -- JSON-RPC based protocol handler."""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from vetinari.mcp.protocol import DEFAULT_PROTOCOL_VERSION, negotiate_protocol_version
from vetinari.mcp.tools import MCPToolRegistry

logger = logging.getLogger(__name__)


_JSONRPC_ID_TYPES = (str, int)
_SENSITIVE_BUILTIN_PERMISSIONS = {
    "vetinari_execute": "execute",
    "vetinari_memory": "memory",
}
MCP_PERMISSION_MODEL = {
    "permission_source": "server_granted_permissions",
    "client_advertised_capabilities_are_authorization": False,
    "fail_closed_error_code": -32003,
}
"""Machine-readable MCP authorization contract for security evidence."""


class MCPServer:
    """Model Context Protocol server for Vetinari.

    Handles JSON-RPC 2.0 messages for negotiated MCP protocol revisions.
    Delegates tool invocations to an internal ``MCPToolRegistry`` that maps
    tool names to Vetinari subsystem handlers.

    Attributes:
        PROTOCOL_VERSION: The MCP protocol version string.
        SERVER_NAME: The server identifier sent in the ``initialize`` response.
        SERVER_VERSION: The server version string.
    """

    PROTOCOL_VERSION = DEFAULT_PROTOCOL_VERSION
    SERVER_NAME = "vetinari"
    SERVER_VERSION = "1.0.0"

    def __init__(self) -> None:
        self.registry = MCPToolRegistry()
        self.registry.register_defaults()
        self._initialized = False
        self._client_capabilities: dict[str, Any] = {}
        self._server_granted_permissions: set[str] = set()
        self._protocol_version = self.PROTOCOL_VERSION

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Handle an incoming JSON-RPC 2.0 message and dispatch to the handler.

        Validates the JSON-RPC version field before dispatching. Returns ``None``
        for notification messages (no ``id``) that have been handled successfully,
        since the MCP spec forbids sending a response to a notification.

        Args:
            message: A JSON-RPC 2.0 request or notification dict.

        Returns:
            A JSON-RPC response dict with ``result`` on success, ``error`` on
            failure, or ``None`` for handled notifications.
        """
        if not isinstance(message, dict):
            return self._error_response(None, -32600, "Invalid Request: expected JSON object")

        has_id = "id" in message
        raw_id = message.get("id")
        msg_id = raw_id if self._is_supported_jsonrpc_id(raw_id) else None
        if has_id and not self._is_supported_jsonrpc_id(raw_id):
            return self._error_response(None, -32600, "Invalid Request: id must be a string, integer, or null")

        # MCP requires strict JSON-RPC 2.0 — reject anything that omits or
        # misstates the version field before we do any further work.
        if message.get("jsonrpc") != "2.0":
            return self._error_response(msg_id, -32600, "Invalid Request: missing or wrong jsonrpc version")

        method = message.get("method")
        params = message.get("params", {})

        if not isinstance(method, str) or not method:
            return self._error_response(msg_id, -32600, "Invalid Request: method must be a non-empty string")

        # params must be a dict (object) per JSON-RPC 2.0 — reject arrays,
        # strings, or other types that would cause KeyError later.
        if params is not None and not isinstance(params, dict):
            return self._maybe_notification_error(
                has_id,
                msg_id,
                -32602,
                "Invalid params: params must be an object",
            )

        # Treat missing params as an empty dict so handlers never receive None.
        if params is None:
            params = {}

        handlers = {
            "initialize": self._handle_initialize,
            "notifications/initialized": self._handle_notifications_initialized,
            "tools/list": self._handle_tools_list,
            "tools/call": self._handle_tools_call,
            "ping": self._handle_ping,
        }

        handler = handlers.get(method)
        if handler is None:
            return self._maybe_notification_error(has_id, msg_id, -32601, f"Method not found: {method}")

        try:
            result = handler(params)
            # Notifications (no id) must not receive a response per MCP spec.
            if not has_id:
                return None
            if result is None:
                return {"jsonrpc": "2.0", "id": msg_id, "result": None}
            # Handlers that detect protocol errors return a full error response
            # dict directly (contains "error" key) — fix up the id (handlers
            # use None as placeholder) and pass through unchanged so it is not
            # double-wrapped inside a "result" field.
            if "error" in result:
                result["id"] = msg_id
                return result
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except Exception:
            logger.exception("MCP handler failed for method %s — returning internal error", method)
            return self._maybe_notification_error(has_id, msg_id, -32603, "Internal server error")

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        client_version = params.get("protocolVersion", "")
        if not isinstance(client_version, str) or not client_version:
            return self._error_response(None, -32602, "Invalid params: protocolVersion must be a non-empty string")
        negotiated_version = negotiate_protocol_version(client_version)
        if negotiated_version != client_version:
            # Reject clients that speak a different protocol version — proceeding
            # would produce undefined behavior because message shapes differ.
            return self._error_response(
                None,
                -32002,
                f"Unsupported MCP protocol version: client={client_version}, server={negotiated_version}",
            )
        capabilities = params.get("capabilities", {})
        if capabilities is None:
            capabilities = {}
        if not isinstance(capabilities, dict):
            return self._error_response(None, -32602, "Invalid params: capabilities must be an object")
        self._client_capabilities = capabilities
        self._protocol_version = negotiated_version
        self._initialized = True
        return {
            "protocolVersion": negotiated_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.SERVER_NAME, "version": self.SERVER_VERSION},
        }

    def _handle_tools_list(self, params: dict[str, Any]) -> dict[str, Any]:
        # Guard against clients that skip the initialize handshake.
        if not self._initialized:
            return self._error_response(
                None,
                -32002,
                "Server not initialized — call initialize first",
            )
        return {"tools": self.registry.list_tools()}

    def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        # Guard against clients that skip the initialize handshake.
        if not self._initialized:
            return self._error_response(
                None,
                -32002,
                "Server not initialized — call initialize first",
            )
        name = params.get("name", "")
        if not isinstance(name, str) or not name:
            return self._error_response(None, -32602, "Invalid params: tools/call.name must be a non-empty string")
        arguments = params.get("arguments", {})
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return self._error_response(None, -32602, "Invalid params: tools/call.arguments must be an object")
        permission = _SENSITIVE_BUILTIN_PERMISSIONS.get(name)
        if permission is not None and not self._has_client_permission(permission):
            return self._error_response(None, -32003, f"Permission denied for tool: {name}")
        result = self.registry.invoke(name, arguments)
        if "error" in result:
            return {"content": [{"type": "text", "text": json.dumps(result)}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    @staticmethod
    def _handle_notifications_initialized(params: dict[str, Any]) -> None:
        # MCP clients send this after processing the initialize response; no reply is sent.
        logger.debug("MCP client sent notifications/initialized — session ready")
        return None

    @staticmethod
    def _handle_ping(params: dict[str, Any]) -> dict[str, Any]:
        return {}

    @staticmethod
    def _is_supported_jsonrpc_id(value: Any) -> bool:
        return value is None or (isinstance(value, _JSONRPC_ID_TYPES) and not isinstance(value, bool))

    def _has_client_permission(self, permission: str) -> bool:
        """Return True only for server-owned sensitive-tool permissions."""
        return permission in self._server_granted_permissions

    def grant_server_permission(self, permission: str) -> None:
        """Grant one sensitive-tool permission from trusted server code.

        Args:
            permission: Sensitive-tool permission name to add to the
                server-owned permission set.

        Raises:
            ValueError: If *permission* is not a non-empty string.
        """
        if not isinstance(permission, str) or not permission:
            raise ValueError("permission must be a non-empty string")
        self._server_granted_permissions.add(permission)

    def _maybe_notification_error(
        self,
        has_id: bool,
        msg_id: Any,
        code: int,
        message: str,
    ) -> dict[str, Any] | None:
        if not has_id:
            return None
        return self._error_response(msg_id, code, message)

    def _error_response(self, msg_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


_mcp_server: MCPServer | None = None
_mcp_server_lock = threading.Lock()


def get_mcp_server() -> MCPServer:
    """Return the global MCP server singleton, creating it on first call.

    Returns:
        The shared ``MCPServer`` instance with default tools registered.
    """
    global _mcp_server
    if _mcp_server is None:
        with _mcp_server_lock:
            if _mcp_server is None:
                _mcp_server = MCPServer()
    return _mcp_server
