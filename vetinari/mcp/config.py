"""MCP server configuration loader.

Reads ``config/mcp_servers.yaml`` and returns typed ``MCPServerConfig``
dataclasses for each configured external MCP server.  Results are cached
after the first load so route handlers do not perform repeated disk I/O.

The config file schema:

.. code-block:: yaml

    mcp_servers:
      - name: filesystem
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
        env: {}
        enabled: true
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vetinari.config_paths import resolve_config_path

logger = logging.getLogger(__name__)


# Default path uses the source-tree config when present, then the packaged fallback.
_DEFAULT_CONFIG_PATH = resolve_config_path("mcp_servers.yaml")

# Module-level cache so config is only parsed once per process.
# Protected by _cache_lock for thread safety.
_config_cache: dict[Path, list[MCPServerConfig]] = {}
_cache_lock = threading.Lock()
_MCP_BASE_ENV_ALLOWLIST: frozenset[str] = frozenset({
    "PATH",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "TEMP",
    "TMP",
    "TMPDIR",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
})
_MCP_SHELL_EXECUTABLES: frozenset[str] = frozenset({
    "bash",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "pwsh.exe",
    "sh",
    "zsh",
})
_MCP_EXECUTABLE_METACHARS = frozenset(";&|<>`$\r\n")


def build_mcp_subprocess_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Build an allowlisted MCP subprocess environment with explicit overrides.

    Returns:
        Environment variables safe to pass to an MCP subprocess.
    """
    env = {key: value for key, value in os.environ.items() if key.upper() in _MCP_BASE_ENV_ALLOWLIST}
    if overrides:
        env.update({str(key): str(value) for key, value in overrides.items()})
    return env


def _is_unpinned_remote_runner(command: str, args: list[str]) -> bool:
    if Path(command).name.lower() == "npx" and "-y" in args:
        return True
    return any("@latest" in arg for arg in args)


def validate_mcp_launch_command(command: list[str]) -> None:
    """Validate an MCP server launch command before subprocess execution.

    Args:
        command: Full command vector, including executable and arguments.

    Raises:
        ValueError: If the command is empty, shell-mediated, or uses an
            unpinned remote package runner.
    """
    if not command:
        raise ValueError("MCP server command must not be empty")
    if any(not isinstance(part, str) or not part for part in command):
        raise ValueError("MCP server command entries must be non-empty strings")

    executable = command[0]
    executable_name = Path(executable).name.lower()
    if any(char in executable for char in _MCP_EXECUTABLE_METACHARS):
        raise ValueError("MCP server executable contains shell metacharacters")
    if executable_name in _MCP_SHELL_EXECUTABLES:
        raise ValueError("MCP servers must be launched directly, not through a shell")
    if _is_unpinned_remote_runner(executable, command[1:]):
        raise ValueError("MCP server command uses unpinned remote package execution")


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """Configuration for a single external MCP server.

    Attributes:
        name: Logical identifier used as the tool namespace prefix
            (e.g. ``"filesystem"`` → tools registered as ``mcp__filesystem__*``).
        command: Executable to launch the server subprocess (e.g. ``"npx"``).
        args: Arguments passed after the command when launching the subprocess.
        env: Environment variables added to the subprocess allowlist; empty
            dict means no extra variables beyond the MCP base allowlist.
        enabled: When ``False`` this server is skipped during discovery.
    """

    name: str  # Namespace prefix for all tools from this server
    command: str  # Subprocess executable
    args: list[str] = field(default_factory=list)  # Subprocess arguments
    env: dict[str, str] = field(default_factory=dict)  # Subprocess environment overrides
    enabled: bool = True  # Whether to connect to this server on startup

    def __repr__(self) -> str:
        return f"MCPServerConfig(name={self.name!r}, command={self.command!r}, enabled={self.enabled!r})"

    def to_command(self) -> list[str]:
        """Return the full subprocess command list (command + args).

        Returns:
            A list of strings suitable for passing to ``subprocess.Popen``.
        """
        return [self.command, *self.args]

    def to_env(self) -> dict[str, str]:
        """Return the scrubbed subprocess environment for this server."""
        return build_mcp_subprocess_env(self.env)


def _parse_server_entry(entry: dict[str, Any]) -> MCPServerConfig | None:
    """Parse a single YAML entry dict into an MCPServerConfig.

    Args:
        entry: Raw dict from the YAML ``mcp_servers`` list.

    Returns:
        Parsed ``MCPServerConfig``, or ``None`` if the entry is missing
        required fields (``name`` or ``command``).
    """
    name: str = entry.get("name", "").strip()
    command: str = entry.get("command", "").strip()

    if not name:
        logger.warning("Skipping MCP server entry with missing 'name' field: %r", entry)
        return None
    if not command:
        logger.warning("Skipping MCP server entry %r — missing 'command' field", name)
        return None

    args = [str(a) for a in entry.get("args") or []]
    enabled = bool(entry.get("enabled", True))
    if enabled:
        try:
            validate_mcp_launch_command([command, *args])
        except ValueError as exc:
            logger.warning("Skipping enabled MCP server %r because its launch command is unsafe: %s", name, exc)
            return None

    raw_env = entry.get("env") or {}
    env: dict[str, str] = {str(k): str(v) for k, v in raw_env.items()}

    return MCPServerConfig(
        name=name,
        command=command,
        args=args,
        env=env,
        enabled=enabled,
    )


def load_mcp_server_configs(config_path: Path | None = None) -> list[MCPServerConfig]:
    """Load and cache external MCP server configs from YAML.

    Reads each YAML path once and caches the result for the lifetime of the
    process.  Subsequent calls for the same resolved path return the cached
    list without re-reading disk.

    The ``pyyaml`` package must be installed (listed under ``[mcp]`` in
    ``pyproject.toml``).  If the file is missing or malformed, an empty list
    is returned and a warning is logged so the server starts without external
    MCP tools rather than crashing.

    Args:
        config_path: Path to the YAML config file.  Defaults to
            ``config/mcp_servers.yaml`` relative to the project root.

    Returns:
        List of ``MCPServerConfig`` instances for each enabled server entry.
        Returns an empty list when the config file is absent or unreadable.
    """
    resolved_path = (config_path or _DEFAULT_CONFIG_PATH).resolve()

    # Fast path — already cached
    if resolved_path in _config_cache:
        return _config_cache[resolved_path]

    with _cache_lock:
        # Re-check inside the lock (double-checked locking)
        if resolved_path in _config_cache:
            return _config_cache[resolved_path]

        _config_cache[resolved_path] = _load_from_path(resolved_path)

    return _config_cache[resolved_path]


def _load_from_path(path: Path) -> list[MCPServerConfig]:
    """Read and parse the YAML file at *path* without caching.

    Args:
        path: Absolute or relative path to the YAML config file.

    Returns:
        Parsed list of ``MCPServerConfig`` instances (may be empty).
    """
    if not path.exists():
        logger.debug("MCP server config not found at %s — no external servers configured", path)
        return []

    try:
        import yaml  # pyyaml — optional dep in [mcp] extras
    except ImportError:
        logger.warning(
            "pyyaml is not installed — cannot load MCP server config from %s; install with: pip install pyyaml",
            path,
        )
        return []

    try:
        raw_text = path.read_text(encoding="utf-8")
        data: dict[str, Any] = yaml.safe_load(raw_text) or {}
    except Exception as exc:
        logger.warning(
            "Failed to read MCP server config from %s — no external servers will be loaded: %s",
            path,
            exc,
        )
        return []

    raw_servers: list[Any] = data.get("mcp_servers") or []
    configs: list[MCPServerConfig] = []

    for entry in raw_servers:
        if not isinstance(entry, dict):
            logger.warning("Skipping non-dict MCP server entry: %r", entry)
            continue
        cfg = _parse_server_entry(entry)
        if cfg is not None:
            configs.append(cfg)

    logger.info(
        "Loaded %d MCP server config(s) from %s (%d enabled)",
        len(configs),
        path,
        sum(1 for c in configs if c.enabled),
    )
    return configs


def reset_mcp_config_cache() -> None:
    """Clear the cached config so the next call to ``load_mcp_server_configs`` re-reads disk.

    Intended for use in tests and hot-reload scenarios.
    """
    with _cache_lock:
        _config_cache.clear()
