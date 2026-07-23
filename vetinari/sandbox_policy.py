"""Sandbox policy and security infrastructure.

Contains security constants, the external-plugin sandbox, and the three
helper classes used by SandboxManager: policy loader, rate limiter, and
audit logger.

These components are separated from the execution core (CodeSandbox /
CodeExecutor) so they can be imported independently without pulling in
the subprocess execution machinery.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import logging
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import _PROJECT_ROOT, SANDBOX_MAX_MEMORY_MB, SANDBOX_TIMEOUT
from vetinari.sandbox_policy_helpers import _SandboxAuditLogger, _SandboxPolicyLoader, _SandboxRateLimiter
from vetinari.sandbox_types import SandboxAuditEntry
from vetinari.security.redaction import redact_value

logger = logging.getLogger(__name__)

__all__ = [
    "_ALLOWED_COMMANDS",
    "_DANGEROUS_ATTRS",
    "_DANGEROUS_NAMES",
    "_SAFE_ENV_VARS",
    "ExternalPluginSandbox",
    "_SandboxAuditLogger",
    "_SandboxPolicyLoader",
    "_SandboxRateLimiter",
    "_execute_plugin_hook_entry",
]


# ── Environment and command allowlists ───────────────────────────────────────
# Only these environment variables are forwarded to sandboxed subprocesses.
# Keeping the list minimal prevents leaking API keys and other secrets.

_SAFE_ENV_VARS: frozenset[str] = frozenset({
    "PATH",
    "HOME",
    "TEMP",
    "TMP",
    "TMPDIR",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONDONTWRITEBYTECODE",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SYSTEMROOT",
    "COMSPEC",  # Required on Windows
})

# Only these command names (without path) may be executed via execute_shell().
_ALLOWED_COMMANDS: frozenset[str] = frozenset({
    "python",
    "python3",
    "pip",
    "pip3",
    "node",
    "npm",
    "npx",
    "git",
    "ls",
    "dir",
    "cat",
    "head",
    "tail",
    "wc",
    "echo",
    "grep",
    "find",
    "which",
    "where",
    "pytest",
    "ruff",
    "mypy",
    "black",
})

# ── Dangerous-pattern constants used by SandboxManager pre-execution scan ────
# These are checked by SandboxManager.execute() before dispatching to any
# backend. A single AST walk is performed when any of these strings appear
# in the submitted code (fast pre-filter avoids parsing clean code).

_DANGEROUS_NAMES: frozenset[str] = frozenset({
    "compile",
    "eval",
    "exec",
    "__import__",
    "open",
    "input",
    "getattr",
    "setattr",
    "delattr",
    "type",
    "vars",
    "dir",
    "globals",
    "locals",
    "breakpoint",
})
"""Function/call names that indicate potentially unsafe code patterns."""

_DANGEROUS_ATTRS: frozenset[str] = frozenset({
    "__subclasses__",
    "__bases__",
    "__mro__",
    "__globals__",
    "__code__",
    "__builtins__",
    "__class__",
    "__dict__",
    "__init__",
    "__new__",
    "__del__",
    "__reduce__",
    "__reduce_ex__",
    "__getattr__",
    "__setattr__",
    "__delattr__",
    "__module__",
    "__qualname__",
    "__func__",
    "__self__",
    "__wrapped__",
    "__loader__",
    "__spec__",
    "__path__",
    "__file__",
    "__cached__",
    "__import__",
})
"""Attribute names used in class-hierarchy escape chains."""

_INJECTION_MARKERS: frozenset[str] = frozenset({
    "```python",
    "```bash",
    "```shell",
    "<script>",
    "</script>",
    "os.system(",
    "subprocess.run(",
    "subprocess.call(",
    "subprocess.Popen(",
    "__import__('os')",
    "__import__('subprocess')",
    "import os;",
    "import subprocess;",
})
"""Patterns that indicate LLM prompt injection attempting to escape into code execution."""

MAX_CODE_LENGTH = 10_000  # Maximum allowed code length in characters (US-041)


def _execute_plugin_hook_payload(
    target_file: str,
    plugin_name: str,
    hook_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Load and execute one plugin hook, returning a serialized-safe envelope."""
    try:
        spec = importlib.util.spec_from_file_location(f"vetinari_plugin_{plugin_name}", target_file)
        if spec is None or spec.loader is None:
            logger.warning("Plugin %r could not be loaded from %s", plugin_name, target_file)
            return {"ok": False, "error": f"Plugin {plugin_name} could not be loaded"}

        mod = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            spec.loader.exec_module(mod)

            hook_fn = getattr(mod, hook_name, None)
            if hook_fn is None or not callable(hook_fn):
                logger.warning("Plugin %r has no callable hook %r", plugin_name, hook_name)
                return {"ok": False, "error": f"Hook {hook_name} not found in plugin {plugin_name}"}

            return {"ok": True, "result": hook_fn(params)}
    except BaseException as exc:
        logger.warning("Plugin %r hook %r failed in child process: %s", plugin_name, hook_name, exc)
        return {"ok": False, "error": str(exc)}


def _execute_plugin_hook_entry() -> int:
    """Subprocess entrypoint for executing one plugin hook."""
    try:
        payload = json.loads(sys.stdin.read())
        envelope = _execute_plugin_hook_payload(
            payload["target_file"],
            payload["plugin_name"],
            payload["hook_name"],
            payload["params"],
        )
    except BaseException as exc:
        logger.warning("Plugin hook subprocess entry failed: %s", exc)
        envelope = {"ok": False, "error": str(exc)}

    try:
        sys.stdout.write(json.dumps(envelope, default=str))
    except BaseException as exc:
        logger.warning("Plugin hook subprocess could not serialize result: %s", exc)
        fallback = {"ok": False, "error": f"Plugin hook result could not be serialized: {exc}"}
        sys.stdout.write(json.dumps(fallback, default=str))
    return 0


# ── External plugin sandbox ──────────────────────────────────────────────────


class ExternalPluginSandbox:
    """External plugin sandbox using importlib-based module loading.

    Loads plugin modules from a directory and executes named hooks within
    a killable child-process timeout.
    """

    ALLOWED_HOOKS = ["read_file", "write_file", "search_code"]

    def __init__(
        self,
        plugin_dir: str = "./plugins",
        timeout: int = SANDBOX_TIMEOUT,
        max_memory_mb: int = SANDBOX_MAX_MEMORY_MB,
    ):
        """Configure the plugin directory and execution limits.

        Args:
            plugin_dir: Path to the directory containing plugin sub-directories.
            timeout: Maximum hook execution time in seconds.
            max_memory_mb: Maximum memory per hook execution in MB (informational).
        """
        self.plugin_dir = Path(plugin_dir)
        self.timeout = timeout
        self.max_memory_mb = max_memory_mb
        self.loaded_plugins: dict[str, Any] = {}
        self.audit_log: list[SandboxAuditEntry] = []

    def discover_plugins(self) -> list[dict]:
        """Scan the plugin directory for valid plugin manifests.

        Returns:
            List of parsed manifest dictionaries.
        """
        manifests = []
        if not self.plugin_dir.exists():
            return manifests

        for plugin_path in self.plugin_dir.iterdir():
            if plugin_path.is_dir():
                manifest_file = plugin_path / "manifest.yaml"
                if manifest_file.exists():
                    try:
                        import yaml

                        with Path(manifest_file).open(encoding="utf-8") as f:
                            data = yaml.safe_load(f)
                            manifests.append(data)
                    except Exception:  # yaml.YAMLError or OSError
                        logger.warning("Failed to load plugin manifest %s", manifest_file)
        return manifests

    def _resolve_plugin_path(self, plugin_name: str) -> Path | dict[str, str]:
        plugin_dir_resolved = self.plugin_dir.resolve()
        try:
            plugin_path_resolved = (self.plugin_dir / plugin_name).resolve()
        except Exception as e:
            logger.warning("Failed to resolve plugin path for %r - rejecting: %s", plugin_name, e)
            return {"error": f"Plugin name {plugin_name!r} is invalid"}

        if plugin_path_resolved != plugin_dir_resolved and plugin_dir_resolved not in plugin_path_resolved.parents:
            logger.warning(
                "Path traversal attempt blocked for plugin %r - resolved path %s escapes plugin dir %s",
                plugin_name,
                plugin_path_resolved,
                plugin_dir_resolved,
            )
            return {"error": f"Plugin name {plugin_name!r} is not allowed (path traversal detected)"}
        return plugin_path_resolved

    def _record_hook_audit(
        self,
        execution_id: str,
        hook_name: str,
        status: str,
        duration_ms: int,
        details: dict[str, Any],
    ) -> None:
        self._log_audit(
            SandboxAuditEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                execution_id=execution_id,
                operation=hook_name,
                sandbox_type="external",
                status=status,
                duration_ms=duration_ms,
                details=details,
            )
        )

    @staticmethod
    def _plugin_target(plugin_path: Path) -> Path:
        init_file = plugin_path / "__init__.py"
        main_file = plugin_path / "main.py"
        return main_file if main_file.exists() else init_file

    def _execute_validated_hook(
        self,
        plugin_path: Path,
        plugin_name: str,
        hook_name: str,
        params: dict,
    ) -> Any:
        target = self._plugin_target(plugin_path)
        if not target.exists():
            logger.error("Plugin %r has no loadable module at %s", plugin_name, target)
            return {"error": f"Plugin {plugin_name} not found"}
        outcome = self._run_hook_in_subprocess(target, plugin_name, hook_name, params)
        if not outcome.get("ok"):
            raise RuntimeError(str(outcome.get("error", "plugin hook failed")))
        return outcome.get("result")

    def execute_hook(self, plugin_name: str, hook_name: str, params: dict) -> Any:
        """Execute a named hook in the specified plugin.

        Args:
            plugin_name: The plugin to invoke.
            hook_name: The hook function name.
            params: Parameters passed to the hook function.

        Returns:
            The hook's return value, or a dict with an ``error`` key on failure.

        Timeouts and plugin exceptions are returned as ``{"error": "..."}``.

        Raises:
            No exceptions are raised for plugin failures; errors are converted
            to structured responses and audit entries.
        """
        if hook_name not in self.ALLOWED_HOOKS:
            return {"error": f"Hook {hook_name} not allowed"}

        plugin_path = self._resolve_plugin_path(plugin_name)
        if isinstance(plugin_path, dict):
            return plugin_path

        execution_id = f"plugin_{uuid.uuid4().hex[:8]}"
        start_time = time.time()

        self._record_hook_audit(
            execution_id,
            hook_name,
            "executing",
            0,
            {"plugin": plugin_name, "params": redact_value(params)},
        )

        try:
            result = self._execute_validated_hook(plugin_path, plugin_name, hook_name, params)
            self._record_hook_audit(
                execution_id,
                hook_name,
                "success",
                int((time.time() - start_time) * 1000),
                {"plugin": plugin_name},
            )
            return result

        except Exception as e:
            self._record_hook_audit(
                execution_id,
                hook_name,
                "error",
                int((time.time() - start_time) * 1000),
                {"plugin": plugin_name, "error": str(e)},
            )
            logger.warning("Plugin hook execution failed — returning error result: %s", e)
            return {"error": str(e)}

    def _run_hook_in_subprocess(
        self,
        target_file: Path,
        plugin_name: str,
        hook_name: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute a plugin hook in a separate process with a hard timeout."""
        payload = json.dumps(
            {
                "target_file": str(target_file),
                "plugin_name": plugin_name,
                "hook_name": hook_name,
                "params": params,
            },
            default=str,
        ).encode("utf-8")
        command = [
            sys.executable,
            "-c",
            "from vetinari.sandbox_policy import _execute_plugin_hook_entry; "
            "raise SystemExit(_execute_plugin_hook_entry())",
        ]
        try:
            completed = subprocess.run(
                command,
                input=payload,
                capture_output=True,
                cwd=str(_PROJECT_ROOT),
                timeout=float(self.timeout),
                check=False,
            )
        except subprocess.TimeoutExpired:
            logger.warning("Plugin hook %s timed out after %s seconds", hook_name, self.timeout)
            return {
                "ok": False,
                "error": f"Plugin hook {hook_name} timed out after {self.timeout} seconds",
            }

        if completed.returncode != 0 and not completed.stdout:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            return {
                "ok": False,
                "error": f"Plugin hook {hook_name} subprocess failed: {stderr or completed.returncode}",
            }

        try:
            return json.loads(completed.stdout.decode("utf-8", errors="replace").strip())
        except Exception as exc:
            logger.warning("Plugin hook %s returned invalid subprocess output: %s", hook_name, exc)
            return {
                "ok": False,
                "error": f"Plugin hook {hook_name} returned an invalid result: {exc}",
            }

    def _log_audit(self, entry: SandboxAuditEntry) -> None:
        self.audit_log.append(entry)

    def get_audit_log(self, limit: int = 100) -> list[dict]:
        """Return recent audit log entries as plain dictionaries.

        Args:
            limit: Maximum number of entries to return (most recent first).

        Returns:
            List of audit entry dictionaries.
        """
        from dataclasses import asdict

        return [asdict(e) for e in self.audit_log[-limit:]]


# ── Policy loader ────────────────────────────────────────────────────────────
