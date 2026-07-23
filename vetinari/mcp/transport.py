"""MCP Transport layer -- stdio JSON-RPC transport."""

from __future__ import annotations

import json
import logging
import sys
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.mcp.server import MCPServer


class StdioTransport:
    """stdio-based MCP transport (reads JSON-RPC from stdin, writes to stdout)."""

    def __init__(self, server: MCPServer) -> None:
        """Initialize the stdio transport.

        Args:
            server: The MCPServer instance to dispatch messages to.
        """
        self._server = server

    def run(self) -> None:
        """Read JSON-RPC messages from stdin, write responses to stdout."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
                # Guard against servers that send a JSON array, string, or number
                # instead of a JSON object — handle_message calls .get() which
                # would raise AttributeError on non-dict values.
                if not isinstance(message, dict):
                    error = {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Invalid Request: expected JSON object"},
                    }
                    sys.stdout.write(json.dumps(error) + "\n")
                    sys.stdout.flush()
                    continue
                has_id = "id" in message
                response = self._server.handle_message(message)
                # Suppress notification responses (id is None) — clients do not
                # expect a reply for one-way notification messages.
                if response is not None and (has_id or response.get("id") is not None):
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()
            except json.JSONDecodeError:
                error = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
                sys.stdout.write(json.dumps(error) + "\n")
                sys.stdout.flush()
