"""Sandbox manager — SandboxManager orchestrator, CodeExecutor, and singleton accessors.

Contains the high-level sandbox management layer:
- ``CodeExecutor`` — simplified code-run interface wrapping a ``CodeSandbox``
- ``SandboxManager`` — rate-limited, policy-enforced execution coordinator
  using double-checked locking singleton
- ``sandbox_manager`` module-level singleton
- ``get_sandbox_manager()`` accessor
- ``get_subprocess_executor()`` / ``init_code_executor()`` executor helpers

Separated from ``code_sandbox.py`` so the manager can be imported without
pulling in the full subprocess execution machinery.
"""

from __future__ import annotations

import ast
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from vetinari.boundary_guards import account_evidence_drop
from vetinari.exceptions import SandboxError
from vetinari.sandbox_policy import (
    _DANGEROUS_ATTRS,
    _DANGEROUS_NAMES,
    _INJECTION_MARKERS,
    MAX_CODE_LENGTH,
    ExternalPluginSandbox,
    _SandboxAuditLogger,
    _SandboxPolicyLoader,
    _SandboxRateLimiter,
)
from vetinari.sandbox_types import SandboxResult
from vetinari.security.fail_closed import sanitize_untrusted_text

logger = logging.getLogger(__name__)


# ── CodeExecutor ──────────────────────────────────────────────────────────────


class CodeExecutor:
    """Simplified code execution interface backed by a CodeSandbox.

    Wraps a ``CodeSandbox`` instance with convenience helpers — run/validate,
    multi-input testing, and automatic cleanup. Callers that only want "run
    code and get a dict back" should use this class rather than CodeSandbox.
    """

    def __init__(self, sandbox: object | None = None) -> None:
        """Configure the executor with an optional pre-constructed sandbox.

        Args:
            sandbox: An existing ``CodeSandbox`` instance. When ``None``,
                a new ``CodeSandbox`` with default settings is created lazily.
        """
        if sandbox is not None:
            self.sandbox = sandbox
        else:
            # Lazy import to avoid circular: code_sandbox imports sandbox_manager
            from vetinari.code_sandbox import CodeSandbox

            self.sandbox = CodeSandbox()

    def run(self, code: str, language: str = "python", **kwargs: Any) -> dict[str, Any]:
        """Run code and return a serialisable result dictionary.

        Args:
            code: Source code to execute.
            language: Execution language — ``"python"``, ``"shell"``,
                ``"bash"``, or ``"sh"``.
            **kwargs: Additional keyword arguments forwarded to the sandbox.

        Returns:
            A ``dict`` produced by ``ExecutionResult.to_dict()``, or a
            failure dict when the language is not supported.
        """
        if language.lower() == "python":
            result = self.sandbox.execute_python(code, **kwargs)
        elif language.lower() in ("bash", "shell", "sh"):
            result = self.sandbox.execute_shell(code, **kwargs)
        else:
            return {
                "success": False,
                "error": f"Unsupported language: {language}",
                "output": "",
            }

        return result.to_dict()

    def run_and_validate(
        self,
        code: str,
        expected_output: str | None = None,
        error_pattern: str | None = None,
    ) -> dict[str, Any]:
        """Run code and validate the output against expected patterns.

        Args:
            code: Source code to execute.
            expected_output: Substring that must appear in the output.
            error_pattern: Substring that must NOT appear in output or error.

        Returns:
            Validation result dict with ``success``, ``output``, ``error``,
            ``validations``, and ``all_validations_passed`` keys.
        """
        result = self.run(code)

        validation: dict[str, Any] = {
            "success": result["success"],
            "output": result["output"],
            "error": result["error"],
            "validations": {
                "execution_succeeded": bool(result["success"]),
            },
        }

        if expected_output:
            validation["validations"]["expected_output"] = expected_output in result["output"]

        if error_pattern:
            validation["validations"]["no_errors"] = (
                error_pattern not in result["output"] and error_pattern not in result["error"]
            )

        if expected_output is None and error_pattern is None:
            validation["validations"]["validation_checks_configured"] = False

        validation["all_validations_passed"] = (
            bool(result["success"]) and bool(validation["validations"]) and all(validation["validations"].values())
        )

        return validation

    def test_with_input(self, code: str, inputs: list[Any]) -> list[dict[str, Any]]:
        """Run code with multiple inputs and collect per-input results.

        Args:
            code: Source code to execute for each input.
            inputs: List of input values. Each is passed as
                ``input_data={"value": item}`` to the sandbox.

        Returns:
            List of dicts, each with ``input`` and ``result`` keys.
        """
        return [{"input": item, "result": self.run(code, input_data={"value": item})} for item in inputs]


# ── Global CodeExecutor singleton ─────────────────────────────────────────────

_code_executor: CodeExecutor | None = None
_code_executor_lock = threading.Lock()


def get_subprocess_executor() -> CodeExecutor:
    """Return the module-level CodeExecutor singleton, creating it on first call.

    Uses double-checked locking to guarantee thread-safe lazy initialisation.

    Returns:
        The shared ``CodeExecutor`` instance.
    """
    global _code_executor
    if _code_executor is None:
        with _code_executor_lock:
            if _code_executor is None:
                _code_executor = CodeExecutor()
    return _code_executor


def init_code_executor(**kwargs: Any) -> CodeExecutor:
    """Create a fresh CodeExecutor backed by a new CodeSandbox, replacing the singleton.

    Useful in tests that need a sandbox configured with non-default parameters
    (e.g. a specific ``working_dir`` or ``max_execution_time``).

    Args:
        **kwargs: Forwarded to ``CodeSandbox.__init__()`` (working_dir,
            max_execution_time, max_memory_mb, etc.).

    Returns:
        The newly created ``CodeExecutor`` instance (also stored as the singleton).
    """
    global _code_executor
    from vetinari.code_sandbox import CodeSandbox

    sandbox = CodeSandbox(**kwargs)
    _code_executor = CodeExecutor(sandbox)
    return _code_executor


# ── SandboxManager ────────────────────────────────────────────────────────────


class SandboxManager:
    """Thin orchestrator that delegates to policy, rate-limiting, and audit helpers.

    Coordinates four concerns, each owned by a dedicated internal class:
      - ``_SandboxPolicyLoader``  — reads sandbox_policy.yaml
      - ``_SandboxRateLimiter``   — per-client sliding-window rate limiting
      - ``_SandboxAuditLogger``   — best-effort audit record persistence
      - ``SandboxManager`` itself — security scanning, dispatch, status

    External callers interact only with ``SandboxManager.execute()`` and
    ``SandboxManager.get_status()``.
    """

    _instance: SandboxManager | None = None
    _lock: threading.Lock = threading.Lock()

    _RATE_LIMIT_MAX = 10  # max calls per client per window
    _RATE_LIMIT_WINDOW = 60.0  # seconds

    @classmethod
    def get_instance(cls) -> SandboxManager:
        """Return the singleton SandboxManager, creating it on first call.

        Uses double-checked locking to guarantee thread safety without holding
        the lock on every read after initialisation.

        Returns:
            The shared ``SandboxManager`` instance, constructed lazily on first access.
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def __init__(self, policy_path: Path | None = None) -> None:
        """Initialise SandboxManager by loading policy and wiring helper objects.

        Args:
            policy_path: Override path to ``sandbox_policy.yaml``. Defaults
                to ``<project_root>/config/sandbox_policy.yaml``.
        """
        self._policy_loader = _SandboxPolicyLoader()
        self._rate_limiter = _SandboxRateLimiter(
            max_calls=self._RATE_LIMIT_MAX,
            window_seconds=self._RATE_LIMIT_WINDOW,
        )
        self._audit_logger = _SandboxAuditLogger()

        self.policy = self._policy_loader.load(policy_path)
        ext_cfg = self.policy.sandbox.external
        self.external = ExternalPluginSandbox(
            plugin_dir=ext_cfg.plugin_dir,
            timeout=ext_cfg.timeout_seconds,
            max_memory_mb=ext_cfg.max_memory_mb,
        )
        self.current_load = 0.0
        self.max_concurrent = 5

    @staticmethod
    def _scan_for_injection_markers(code: str) -> str | None:
        """Check code for LLM prompt injection markers.

        Scans the code string for known patterns used in prompt injection
        attacks that attempt to smuggle shell or script execution payloads
        into sandboxed code.

        Args:
            code: Source code string to scan.

        Returns:
            The first matching injection marker string, or ``None`` when clean.
        """
        for marker in _INJECTION_MARKERS:
            if marker in code:
                return marker
        return None

    def execute(
        self,
        code: str,
        sandbox_type: str = "in_process",
        timeout: int = 30,
        context: dict | None = None,
        client_id: str = "default",
    ) -> SandboxResult:
        """Execute code in a sandboxed environment with rate limiting and security checks.

        Performs injection marker scanning, AST-based dangerous-pattern detection,
        per-client rate limiting, and then dispatches to a ``CodeSandbox`` subprocess.
        Audit records are persisted after each execution (best-effort).

        Args:
            code: Source code string to execute.
            sandbox_type: Execution strategy — ``"in_process"`` or ``"subprocess"``.
            timeout: Maximum execution time in seconds.
            context: Optional namespace dict injected into the execution scope.
            client_id: Caller identifier for per-client rate limiting.

        Returns:
            ``SandboxResult`` with execution output, errors, and timing metadata.

        Raises:
            SandboxError: If ``sandbox_type`` is unknown or code exceeds
                ``MAX_CODE_LENGTH`` characters.
        """
        client_id = sanitize_untrusted_text(client_id, max_length=160)
        sandbox_type = sanitize_untrusted_text(sandbox_type, max_length=80)
        if sandbox_type not in {"in_process", "subprocess"}:
            rejected = self._rejected_result(f"Unsupported sandbox_type: {sandbox_type}")
            self._record_audit(
                sandbox_type=sandbox_type,
                execution_id=rejected.execution_id,
                status="denied",
                duration_ms=0,
                code_length=len(code),
            )
            raise SandboxError(f"Unsupported sandbox_type: {sandbox_type}")
        code = sanitize_untrusted_text(code, max_length=MAX_CODE_LENGTH)
        if len(code) > MAX_CODE_LENGTH:
            raise SandboxError(f"Code length {len(code)} exceeds maximum {MAX_CODE_LENGTH} characters")

        rejected = self._reject_unsafe_code(code)
        if rejected is not None:
            self._record_audit(
                sandbox_type=sandbox_type,
                execution_id=rejected.execution_id,
                status="denied",
                duration_ms=0,
                code_length=len(code),
            )
            return rejected

        if not self._rate_limiter.check(client_id):
            rejected = SandboxResult(
                execution_id=f"exec_{uuid.uuid4().hex[:8]}",
                success=False,
                error=(
                    f"Rate limit exceeded: max {self._RATE_LIMIT_MAX} executions "
                    f"per {int(self._RATE_LIMIT_WINDOW)}s per client"
                ),
                execution_time_ms=0,
            )
            self._record_audit(
                sandbox_type=sandbox_type,
                execution_id=rejected.execution_id,
                status="denied",
                duration_ms=0,
                code_length=len(code),
            )
            return rejected

        execution_id = f"exec_{uuid.uuid4().hex[:8]}"
        _exec_start = time.time()

        _result = self._execute_python_subprocess(execution_id, code, timeout, context)

        _duration_ms = int((time.time() - _exec_start) * 1000)
        _status = "success" if _result.success else "failure"
        logger.info(
            "Sandbox execution: sandbox_type=%s execution_id=%s duration_ms=%d status=%s code_length=%d",
            sandbox_type,
            execution_id,
            _duration_ms,
            _status,
            len(code),
        )

        self._record_audit(
            sandbox_type=sandbox_type,
            execution_id=_result.execution_id,
            status=_status,
            duration_ms=_duration_ms,
            code_length=len(code),
        )

        return _result

    def _record_audit(
        self,
        *,
        sandbox_type: str,
        execution_id: str,
        status: str,
        duration_ms: int,
        code_length: int,
    ) -> None:
        """Write a sandbox audit record and account for failed audit writes."""
        try:
            audit_logger = getattr(self, "_audit_logger", None)
            if audit_logger is None:
                audit_logger = _SandboxAuditLogger()
                self._audit_logger = audit_logger
            audit_logger.record(
                sandbox_type=sandbox_type,
                execution_id=execution_id,
                status=status,
                duration_ms=duration_ms,
                code_length=code_length,
            )
        except Exception:
            account_evidence_drop(execution_id, "sandbox_audit_record", logger=logger)
            raise

    def _reject_unsafe_code(self, code: str) -> SandboxResult | None:
        """Return a rejection result when code trips injection or dangerous patterns."""
        marker = self._scan_for_injection_markers(code)
        if marker:
            logger.warning("Injection marker detected in sandbox code: %s", marker)
            return self._rejected_result(f"Rejected: code contains injection marker '{marker}'")

        needs_scan = any(p in code for p in _DANGEROUS_NAMES) or any(a in code for a in _DANGEROUS_ATTRS)
        if not needs_scan:
            return None
        try:
            return self._reject_dangerous_ast(code)
        except SyntaxError:
            logger.warning("Exception handled by  reject unsafe code fallback", exc_info=True)
            return self._reject_dangerous_text(code)

    def _reject_dangerous_ast(self, code: str) -> SandboxResult | None:
        """Parse code and reject dangerous call names or attribute access."""
        for node in ast.walk(ast.parse(code)):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in _DANGEROUS_NAMES:
                    return self._rejected_result(f"Dangerous pattern '{node.func.id}' detected in code")
                if isinstance(node.func, ast.Attribute) and node.func.attr in _DANGEROUS_NAMES:
                    return self._rejected_result(f"Dangerous pattern '{node.func.attr}' detected in code")
            if isinstance(node, ast.Attribute) and node.attr in _DANGEROUS_ATTRS:
                return self._rejected_result(f"Dangerous pattern '{node.attr}' detected in code")
        return None

    def _reject_dangerous_text(self, code: str) -> SandboxResult | None:
        """Fallback string scan when code cannot be parsed."""
        for pattern in (*_DANGEROUS_NAMES, *_DANGEROUS_ATTRS):
            if pattern in code:
                return self._rejected_result(f"Dangerous pattern '{pattern}' detected in code")
        return None

    @staticmethod
    def _rejected_result(error: str) -> SandboxResult:
        """Build a standard sandbox rejection result."""
        return SandboxResult(
            execution_id=f"exec_{uuid.uuid4().hex[:8]}", success=False, error=error, execution_time_ms=0
        )

    @staticmethod
    def _execute_python_subprocess(
        execution_id: str,
        code: str,
        timeout: int,
        context: dict | None,
    ) -> SandboxResult:
        """Execute Python through CodeSandbox and convert to SandboxResult."""
        from vetinari.code_sandbox import CodeSandbox

        code = sanitize_untrusted_text(code, max_length=MAX_CODE_LENGTH)
        exec_result = CodeSandbox(max_execution_time=timeout, allow_network=False).execute_python(
            code, input_data=context or {}
        )
        combined_output = exec_result.output or ""
        if exec_result.stdout:
            combined_output = exec_result.stdout + ("\n" + combined_output if combined_output else "")
        return SandboxResult(
            execution_id=execution_id,
            success=exec_result.success,
            result=combined_output,
            error=exec_result.error or (exec_result.stderr or ""),
            execution_time_ms=exec_result.execution_time_ms,
        )

    def get_status(self) -> dict[str, Any]:
        """Return the current status of all sandbox backends.

        Performs a lightweight availability check for each backend rather than
        hardcoding ``True``.  Subprocess availability is inferred from whether
        the current load is below the concurrency ceiling; external plugin
        availability reflects whether the plugin directory is accessible.

        Returns:
            Dictionary with status information for each sandbox type.
        """
        # Subprocess backend is available when it has headroom below max concurrency.
        subprocess_available = self.current_load < self.max_concurrent

        # External plugin backend is available when the plugin directory exists and is readable.
        try:
            plugin_dir = self.external.plugin_dir
            external_available = plugin_dir.exists() and plugin_dir.is_dir()
        except Exception:
            external_available = False

        return {
            "subprocess": {
                "available": subprocess_available,
                "current_load": self.current_load,
                "max_concurrent": self.max_concurrent,
                "queue_length": 0,
            },
            "external": {
                "available": external_available,
                "plugins_loaded": len(self.external.loaded_plugins),
                "isolation": "process",
            },
        }

    def get_audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return recent audit log entries from all sandbox execution sinks.

        Merges records from two sources:
        - ``_audit_logger`` — subprocess/in-process executions via SandboxManager.execute()
        - ``external.get_audit_log()`` — plugin hook executions via ExternalPluginSandbox

        Args:
            limit: Maximum number of entries to return (most recent across both sources).

        Returns:
            List of audit entry dictionaries, most recent first.
        """
        # Collect from both sinks and merge by timestamp (most recent last in each list).
        manager_records = self._audit_logger.get_records(limit)
        plugin_records = self.external.get_audit_log(limit)
        merged = manager_records + plugin_records
        # Sort by timestamp descending so the caller gets the most recent entries.
        merged.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
        return merged[:limit]


# ── Module-level singleton ────────────────────────────────────────────────────

sandbox_manager: SandboxManager | None = None


def get_sandbox_manager() -> SandboxManager:
    """Return the module-level ``SandboxManager`` singleton.

    Returns:
        The shared ``SandboxManager`` instance.
    """
    global sandbox_manager
    if sandbox_manager is None:
        sandbox_manager = SandboxManager.get_instance()
    return sandbox_manager
