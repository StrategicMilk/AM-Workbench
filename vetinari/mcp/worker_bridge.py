"""Worker-to-MCP bridge — connects the Worker agent to external MCP servers.

Loads external MCP server configs from ``config/mcp_servers.yaml``, starts
client connections, discovers tools, and registers them in the MCP tool
registry under the ``mcp__{server}__{tool}`` namespace.

Provides two functions used by the Worker agent:

- ``get_available_mcp_tools()`` — returns tool schemas for prompt injection
- ``invoke_mcp_tool(namespaced_name, arguments)`` — dispatches a tool call
  through the registry to the right external server

The bridge is instantiated lazily via the ``get_worker_mcp_bridge()`` singleton.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from vetinari.mcp.config import MCPServerConfig, load_mcp_server_configs
from vetinari.mcp.tools import MCPToolRegistry
from vetinari.security.fail_closed import UntrustedInputError, sanitize_untrusted_text

logger = logging.getLogger(__name__)


# Namespace prefix format for external MCP tools.
_NAMESPACE_PREFIX = "mcp__"
_NAMESPACED_TOOL_RE = re.compile(r"^[A-Za-z0-9_.:-]+(?:__[A-Za-z0-9_.:-]+){2,}$")


class WorkerMCPBridge:
    """Manages external MCP server connections and exposes tools to the Worker agent.

    On construction the bridge is empty.  Call ``load_servers()`` to read config
    and register tools.  This separation keeps ``__init__`` side-effect-free so
    the bridge can be unit-tested without touching the filesystem or launching
    subprocesses.

    Attributes:
        _registry: Shared MCPToolRegistry that holds all registered tools.
        _active_clients: Mapping from server name to its started MCPClient.
    """

    def __init__(self, registry: MCPToolRegistry | None = None) -> None:
        """Initialise the bridge with an optional shared registry.

        Args:
            registry: Registry to register external tools into.  Defaults to
                a fresh ``MCPToolRegistry`` when ``None``.
        """
        # Use a provided registry so tests can inject a pre-seeded one.
        # None-check rather than truthiness so an empty (but valid) registry is not discarded.
        self._registry: MCPToolRegistry = registry if registry is not None else MCPToolRegistry()
        # server_name -> MCPClient (started subprocesses)
        self._active_clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load_servers(self, configs: list[MCPServerConfig] | None = None) -> int:
        """Start enabled external MCP servers and register their tools.

        For each enabled server config, an ``MCPClient`` is started, the MCP
        handshake is performed, tools are discovered, and registered into
        ``self._registry`` under the ``mcp__{name}__*`` namespace.

        Servers that fail to start or whose tool discovery errors are skipped
        with a warning — partial failures do not abort the whole load.

        Args:
            configs: Explicit list of configs to load.  Defaults to reading
                ``config/mcp_servers.yaml`` via ``load_mcp_server_configs()``.

        Returns:
            Number of external tools successfully registered across all servers.
        """
        from vetinari.mcp.client import MCPClient

        if configs is None:
            configs = load_mcp_server_configs()

        total_registered = 0
        seen_names: set[str] = set()

        for cfg in configs:
            if not cfg.enabled:
                logger.debug("Skipping disabled MCP server %r", cfg.name)
                continue
            if cfg.name in seen_names:
                logger.warning("Skipping duplicate MCP server config name %r", cfg.name)
                continue
            seen_names.add(cfg.name)

            client = None
            try:
                with self._lock:
                    previous = self._active_clients.pop(cfg.name, None)
                if previous is not None:
                    try:
                        previous.stop()
                    finally:
                        self._registry.unregister_external_server(cfg.name)

                client = MCPClient(cfg.to_command(), env=cfg.to_env())
                client.start()
                client.initialize()
                registered = self._registry.register_external_server(cfg.name, client)
                total_registered += len(registered)
                with self._lock:
                    self._active_clients[cfg.name] = client
                logger.info(
                    "Connected to external MCP server %r — %d tool(s) registered",
                    cfg.name,
                    len(registered),
                )
            except Exception as exc:
                try:
                    if client is not None:
                        client.stop()
                except Exception:
                    logger.warning(
                        "Could not stop MCP client for %r during error cleanup — resources may leak",
                        cfg.name,
                    )
                logger.warning(
                    "Failed to load external MCP server %r — its tools will not be available: %s",
                    cfg.name,
                    exc,
                )

        return total_registered

    def shutdown(self) -> None:
        """Stop all active external MCP server subprocesses.

        Safe to call multiple times.  Errors during individual client stops
        are logged but do not prevent other clients from being stopped.
        """
        with self._lock:
            clients = dict(self._active_clients)
            self._active_clients.clear()

        for name, client in clients.items():
            removed = 0
            try:
                client.stop()
                logger.info("Stopped external MCP server %r", name)
            except Exception as exc:
                logger.warning("Error while stopping MCP server %r: %s", name, exc)
            finally:
                try:
                    removed = self._registry.unregister_external_server(name)
                except Exception as exc:
                    logger.warning("Error while unregistering MCP tools for %r: %s", name, exc)
                else:
                    logger.debug("Removed %d MCP tool(s) for stopped server %r", removed, name)

    # ------------------------------------------------------------------
    # Worker-facing API
    # ------------------------------------------------------------------

    def get_available_mcp_tools(self) -> list[dict[str, Any]]:
        """Return tool schemas for all registered external MCP tools.

        Returns only tools whose names begin with ``mcp__`` so built-in
        Vetinari tools (``vetinari_plan``, etc.) are excluded from the list
        returned to external callers.

        Returns:
            List of MCP tool schema dicts with ``name``, ``description``, and
            ``inputSchema`` keys.  Empty list when no external servers are
            configured or all failed to connect.
        """
        all_tools = self._registry.list_tools()
        return [t for t in all_tools if t.get("name", "").startswith(_NAMESPACE_PREFIX)]

    def invoke_mcp_tool(self, namespaced_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke an external MCP tool by its namespaced name.

        Routes the call through the tool registry's ``invoke()`` method which
        delegates to the client handler registered during ``load_servers()``.
        The result is treated as untrusted text from an external system.

        Args:
            namespaced_name: Full namespaced tool name, e.g.
                ``"mcp__filesystem__read_file"``.
            arguments: Keyword arguments forwarded to the tool handler.

        Returns:
            On success: ``{"result": <handler return value>}``.
            On failure: ``{"error": <message>}``.

        Raises:
            UntrustedInputError: If the namespaced tool name contains
                unsupported characters.
        """
        namespaced_name = sanitize_untrusted_text(namespaced_name, max_length=384)
        if not namespaced_name.startswith(_NAMESPACE_PREFIX):
            return {"error": f"Not a namespaced external tool: {namespaced_name!r}"}
        if not _NAMESPACED_TOOL_RE.fullmatch(namespaced_name):
            raise UntrustedInputError("namespaced MCP tool name contains unsupported characters")
        result = self._registry.invoke(namespaced_name, arguments)
        logger.debug(
            "MCP tool %r invoked; success=%s",
            namespaced_name,
            "error" not in result,
        )
        return result

    @property
    def registry(self) -> MCPToolRegistry:
        """Return the underlying tool registry (read-only access for tests).

        Returns:
            The ``MCPToolRegistry`` instance holding all registered tools.
        """
        return self._registry


# ------------------------------------------------------------------
# Module-level singleton (lazy, double-checked locking)
# ------------------------------------------------------------------

_bridge: WorkerMCPBridge | None = None
_bridge_lock = threading.Lock()


def get_worker_mcp_bridge() -> WorkerMCPBridge:
    """Return the global WorkerMCPBridge singleton, creating it on first call.

    The bridge is initialised with the default ``MCPToolRegistry`` and
    ``load_servers()`` is called automatically so external MCP tools are
    available as soon as the first caller requests the bridge.

    Returns:
        The shared ``WorkerMCPBridge`` instance.
    """
    global _bridge
    if _bridge is None:
        with _bridge_lock:
            if _bridge is None:
                _bridge = WorkerMCPBridge()
                _bridge.load_servers()
    return _bridge


def get_available_mcp_tools() -> list[dict[str, Any]]:
    """Return tool schemas for all external MCP tools available to the Worker.

    Convenience wrapper around ``get_worker_mcp_bridge().get_available_mcp_tools()``
    for use in Worker prompt construction.

    Returns:
        List of tool schema dicts.  Empty list when no servers are configured.
    """
    return get_worker_mcp_bridge().get_available_mcp_tools()


def invoke_mcp_tool(namespaced_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Invoke an external MCP tool by namespaced name, routing through the bridge.

    Convenience wrapper around ``get_worker_mcp_bridge().invoke_mcp_tool()``
    for use in Worker tool execution paths.

    Args:
        namespaced_name: Full namespaced tool name, e.g.
            ``"mcp__filesystem__read_file"``.
        arguments: Keyword arguments forwarded to the tool handler.

    Returns:
        On success: ``{"result": <handler return value>}``.
        On failure: ``{"error": <message>}``.
    """
    return get_worker_mcp_bridge().invoke_mcp_tool(namespaced_name, arguments)


def reset_worker_mcp_bridge() -> None:
    """Reset the global bridge singleton (for tests and hot-reload).

    Shuts down any active clients before clearing the singleton so
    subprocesses are not left running.
    """
    global _bridge
    with _bridge_lock:
        if _bridge is not None:
            _bridge.shutdown()
            _bridge = None
