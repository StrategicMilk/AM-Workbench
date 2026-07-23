"""Code execution sandbox — step 5 of the pipeline: Plan → Execute → Sandbox → Verify → Learn.

Subprocess-based isolated code execution. This is where agent-generated code is
actually run in an isolated environment before its output is passed to the Inspector
for verification.

Pipeline role:
    Intake → Planning → Execution → **Sandbox** → Verify → Learn
    Agent tasks that produce runnable code (Worker output) are executed here
    before the Inspector validates correctness.

Public API (backward-compatible re-exports):
    CodeSandbox         — subprocess sandbox class
    CodeExecutor        — simplified run/validate interface
    SandboxManager      — rate-limited orchestrator singleton
    get_sandbox_manager — module-level accessor for the singleton
    sandbox_manager     — pre-created singleton
    ExecutionResult     — per-execution result dataclass
    SandboxResult       — manager-level result dataclass
    SandboxType         — sandbox execution strategy enum
    SandboxStatus       — sandbox backend runtime status enum
    AuditEntry          — frozen audit log entry dataclass
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from vetinari.code_sandbox_lifecycle import ApplyChangesResult, CodeSandboxLifecycleMixin
from vetinari.code_sandbox_shell import prepare_shell_command
from vetinari.constants import SUBPROCESS_TIMEOUT
from vetinari.learning.atomic_writers import _write_text_atomic
from vetinari.sandbox_manager import SandboxManager, get_sandbox_manager
from vetinari.sandbox_policy import _SAFE_ENV_VARS
from vetinari.sandbox_subprocess import parse_sandbox_output, wrap_python_code
from vetinari.sandbox_types import ExecutionResult, SandboxAuditEntry, SandboxResult, SandboxStatus, SandboxType

logger = logging.getLogger(__name__)


_ALLOWED_LINTERS = {"ruff"}


class CodeSandbox(CodeSandboxLifecycleMixin):
    """Sandboxed code execution environment using subprocess isolation.

    Provides subprocess-isolated execution with module blocking, filesystem
    allowlists, network isolation, resource limits, and output capture.
    Both ``in_process`` and ``subprocess`` sandbox types route through here.
    ``SandboxManager`` adds rate limiting and audit logging on top.
    """

    def __init__(
        self,
        working_dir: str | None = None,
        max_execution_time: int = 60,
        max_memory_mb: int = 512,
        allow_network: bool = False,
        allowed_modules: list[str] | None = None,
        blocked_modules: list[str] | None = None,
        filesystem_allowlist: list[str] | None = None,
        network_isolation: bool = True,
    ):
        """Configure sandbox resource limits, module restrictions, and filesystem allowlists.

        Args:
            working_dir: Working directory for execution. A temp dir is created when None.
            max_execution_time: Maximum execution time in seconds.
            max_memory_mb: Maximum memory in MB. On Windows, resource.setrlimit is
                unavailable so this is stored for audit purposes only, not enforced.
            allow_network: Allow network access (deprecated — prefer network_isolation).
            allowed_modules: Whitelist of allowed modules.
            blocked_modules: Explicit blacklist of blocked modules. When provided,
                used as-is (network_isolation additions still apply).
            filesystem_allowlist: Path prefixes that sandboxed code may access.
                An empty list (default) permits all paths.
            network_isolation: When True, adds socket, urllib, requests, httpx, and
                aiohttp to the blocked module list.
        """
        self.working_dir = Path(working_dir or tempfile.mkdtemp(prefix="vetinari_sandbox_"))
        self.max_execution_time = max_execution_time
        self.max_memory_mb = max_memory_mb
        self.allow_network = allow_network
        self.filesystem_allowlist: list[str] = filesystem_allowlist or []
        self.network_isolation = network_isolation
        self.allowed_modules = allowed_modules or []

        _network_modules = {"socket", "urllib", "requests", "httpx", "aiohttp"}
        if blocked_modules is not None:
            self.blocked_modules = list(blocked_modules)
        else:
            _default_blocked = ["os", "sys", "subprocess", "socket", "requests", "urllib"]
            if network_isolation:
                _blocked_set = set(_default_blocked)
                _blocked_set.update(_network_modules)
                self.blocked_modules = list(_blocked_set)
            else:
                self.blocked_modules = list(_default_blocked)

        self._execution_count = 0
        self._lock = threading.Lock()

        logger.info("CodeSandbox initialized (working_dir=%s)", self.working_dir)

    def _working_dir_error(self) -> ExecutionResult | None:
        """Return a bounded failure result when the sandbox root is unusable."""
        if not self.working_dir.exists():
            return ExecutionResult(
                success=False,
                output="",
                error=f"Sandbox working_dir does not exist: {self.working_dir}",
                return_code=-1,
            )
        if not self.working_dir.is_dir():
            return ExecutionResult(
                success=False,
                output="",
                error=f"Sandbox working_dir is not a directory: {self.working_dir}",
                return_code=-1,
            )
        return None

    @staticmethod
    def _get_safe_env() -> dict[str, str]:
        """Build a minimal environment, passing only whitelisted variables to prevent secret leakage.

        Returns:
            Dictionary of safe environment variables.
        """
        return {k: v for k, v in os.environ.items() if k in _SAFE_ENV_VARS}

    def _wrap_python_code(
        self,
        code: str,
        input_data: dict[str, Any] | None = None,
    ) -> str:
        """Delegate to ``wrap_python_code`` forwarding this instance's module and network settings.

        Args:
            code: Raw Python source to execute inside the sandbox.
            input_data: Optional dict injected as ``INPUT_DATA`` in the subprocess environment.

        Returns:
            The full wrapped Python script as a string.
        """
        return wrap_python_code(
            code,
            input_data=input_data,
            blocked_modules=self.blocked_modules,
            allow_network=self.allow_network,
            filesystem_allowlist=self.filesystem_allowlist,
        )

    def execute_python(
        self,
        code: str,
        input_data: dict[str, Any] | None = None,
        env_vars: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> ExecutionResult:
        """Execute Python code in the sandbox subprocess.

        Args:
            code: Code value consumed by execute_python().
            input_data: Structured data consumed by the operation.
            env_vars: Env vars value consumed by execute_python().
            timeout: Timeout value controlling how long the operation may wait.

        Returns:
            Value produced for the caller.
        """
        timeout = timeout or self.max_execution_time
        execution_id = str(uuid.uuid4())[:8]
        script_file = self.working_dir / f"script_{execution_id}.py"

        working_dir_error = self._working_dir_error()
        if working_dir_error is not None:
            return working_dir_error

        _write_text_atomic(Path(script_file), self._wrap_python_code(code, input_data))
        env = self._python_execution_env(env_vars)

        start_time = time.time()

        try:
            result = self._run_python_script(script_file, timeout, env)
            execution_time = int((time.time() - start_time) * 1000)
            return self._python_execution_result(result, execution_time, execution_id, script_file)

        except subprocess.TimeoutExpired:
            execution_time = int((time.time() - start_time) * 1000)
            logger.warning("Sandbox execution timed out — returning timeout error")
            return ExecutionResult(
                success=False,
                output="",
                error=f"Execution timed out after {timeout} seconds",
                execution_time_ms=execution_time,
                return_code=-1,
                metadata={"execution_id": execution_id, "timeout": True},
            )

        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            logger.warning("Sandbox execution failed — returning error result: %s", e)
            return ExecutionResult(
                success=False,
                output="",
                error=f"Execution failed: {e!s}\n{traceback.format_exc()}",
                execution_time_ms=execution_time,
                return_code=-1,
                metadata={"execution_id": execution_id, "exception": str(e)},
            )

        finally:
            if script_file.exists():
                try:
                    script_file.unlink()
                except OSError:
                    logger.warning("Failed to clean up temporary script file %s", script_file, exc_info=True)

    def _python_execution_env(self, env_vars: dict[str, str] | None) -> dict[str, str]:
        env = self._get_safe_env()
        if env_vars:
            env.update(env_vars)
        env["PYTHONPATH"] = str(self.working_dir)
        return env

    def _run_python_script(
        self,
        script_file: Path,
        timeout: int,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(script_file)],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=str(self.working_dir),
        )

    @staticmethod
    def _python_execution_result(
        result: subprocess.CompletedProcess[str],
        execution_time: int,
        execution_id: str,
        script_file: Path,
    ) -> ExecutionResult:
        user_success, user_output, user_error = parse_sandbox_output(result.stdout)
        metadata = {"execution_id": execution_id, "script": str(script_file)}
        if user_success is not None:
            return ExecutionResult(
                success=user_success,
                output=user_output,
                error=user_error,
                execution_time_ms=execution_time,
                return_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                metadata=metadata,
            )
        return ExecutionResult(
            success=result.returncode == 0,
            output=result.stdout,
            error=result.stderr,
            execution_time_ms=execution_time,
            return_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            metadata=metadata,
        )

    def execute(self, code: str, **kwargs: Any) -> ExecutionResult:
        """Convenience alias for ``execute_python()``.

        Args:
            code: Python code to execute.
            **kwargs: Forwarded to execute_python().

        Returns:
            ExecutionResult with output and status.
        """
        return self.execute_python(code, **kwargs)

    def execute_shell(self, command: str, timeout: int | None = None) -> ExecutionResult:
        """Execute a shell command from the sandbox allowlist.

        Args:
            command: Shell command to run (must be in _ALLOWED_COMMANDS).
            timeout: Timeout in seconds; defaults to max_execution_time.

        Returns:
            ExecutionResult with output, error, and return code.
        """
        timeout = timeout or self.max_execution_time
        prepared = prepare_shell_command(command, self.working_dir, split_command=shlex.split)
        if prepared.rejection is not None:
            return prepared.rejection

        start_time = time.time()

        try:
            result = subprocess.run(
                prepared.parts,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._get_safe_env(),
                cwd=str(self.working_dir),
            )

            execution_time = int((time.time() - start_time) * 1000)
            return ExecutionResult(
                success=result.returncode == 0,
                output=result.stdout,
                error=result.stderr,
                execution_time_ms=execution_time,
                return_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        except subprocess.TimeoutExpired:
            execution_time = int((time.time() - start_time) * 1000)
            logger.warning("Sandbox execution timed out — returning timeout error")
            return ExecutionResult(
                success=False,
                output="",
                error=f"Command timed out after {timeout} seconds",
                execution_time_ms=execution_time,
                return_code=-1,
            )

        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            logger.warning("Sandbox execution failed — returning error result: %s", e)
            return ExecutionResult(
                success=False,
                output="",
                error=str(e),
                execution_time_ms=execution_time,
                return_code=-1,
            )

    def test_code(self, code: str, test_code: str, timeout: int = 60) -> ExecutionResult:
        """Execute code together with test code in the sandbox.

        Args:
            code: Source code to test.
            test_code: Test code to run against the source.
            timeout: Timeout in seconds.

        Returns:
            ExecutionResult with combined test output.
        """
        combined_code = f"# Code under test\n{code}\n\n# Tests\n{test_code}\n"
        return self.execute_python(combined_code, timeout=timeout)

    def run_tests(
        self,
        test_dir: str | None = None,
        test_pattern: str = "test_*.py",
        verbose: bool = True,
    ) -> ExecutionResult:
        """Run pytest tests in a directory.

        Args:
            test_dir: Directory to search for tests; defaults to working_dir.
            test_pattern: Glob pattern for test files (unused by pytest directly).
            verbose: Whether to pass -v to pytest.

        Returns:
            ExecutionResult with pytest output.
        """
        sandbox_root = self.working_dir.resolve()
        try:
            resolved_dir = (Path(test_dir) if test_dir else self.working_dir).resolve()
        except OSError as exc:
            logger.warning("Could not resolve sandbox test directory %r", test_dir, exc_info=True)
            return ExecutionResult(success=False, output="", error=str(exc), return_code=-1)
        if not resolved_dir.is_relative_to(sandbox_root):
            return ExecutionResult(
                success=False,
                output="",
                error="test_dir points outside the sandbox root — access denied",
                return_code=-1,
            )
        args = ["-m", "pytest"]
        if verbose:
            args.append("-v")
        args.append(str(resolved_dir))

        start_time = time.time()

        try:
            result = subprocess.run(
                [sys.executable, *args],
                capture_output=True,
                text=True,
                timeout=self.max_execution_time,
                env=self._get_safe_env(),
                cwd=str(resolved_dir),
            )

            execution_time = int((time.time() - start_time) * 1000)
            return ExecutionResult(
                success=result.returncode == 0,
                output=result.stdout,
                error=result.stderr,
                execution_time_ms=execution_time,
                return_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        except Exception as e:
            execution_time = int((time.time() - start_time) * 1000)
            logger.warning("Sandbox execution failed — returning error result: %s", e)
            return ExecutionResult(
                success=False,
                output="",
                error=str(e),
                execution_time_ms=execution_time,
                return_code=-1,
            )

    def lint_code(self, code: str, linter: str = "ruff") -> ExecutionResult:
        """Lint code with the specified linter.

        Args:
            code: Code value consumed by lint_code().
            linter: Linter value consumed by lint_code().

        Returns:
            Value produced for the caller.
        """
        cmd_name = Path(linter).name
        if cmd_name != linter or cmd_name not in _ALLOWED_LINTERS:
            logger.warning("Blocked linter not in sandbox allowlist: %s", linter)
            return ExecutionResult(
                success=False,
                output="",
                error=f"Linter '{linter}' not in sandbox allowlist",
                return_code=-1,
            )

        execution_id = str(uuid.uuid4())[:8]
        script_file = self.working_dir / f"lint_{execution_id}.py"
        _write_text_atomic(Path(script_file), code)
        sandbox_root = self.working_dir.resolve()
        try:
            resolved_script = script_file.resolve()
        except OSError as exc:
            logger.warning("Could not resolve sandbox lint script %s", script_file, exc_info=True)
            return ExecutionResult(success=False, output="", error=str(exc), return_code=-1)
        if not resolved_script.is_relative_to(sandbox_root):
            return ExecutionResult(success=False, output="", error="Lint script escaped sandbox root", return_code=-1)

        try:
            result = subprocess.run(
                [cmd_name, "check", str(resolved_script)],
                capture_output=True,
                text=True,
                timeout=SUBPROCESS_TIMEOUT,
                env=self._get_safe_env(),
                cwd=str(self.working_dir),
            )

            return ExecutionResult(
                success=result.returncode == 0,
                output=result.stdout,
                error=result.stderr,
                return_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )

        except FileNotFoundError:
            logger.warning("Linter '%s' not found on PATH - install it or check PATH configuration", linter)
            return ExecutionResult(
                success=False,
                output="",
                error=f"Linter '{linter}' not found",
                return_code=-1,
            )
        except Exception as e:
            logger.warning("Sandbox execution failed - returning error result: %s", e)
            return ExecutionResult(
                success=False,
                output="",
                error=str(e),
                return_code=-1,
            )
        finally:
            if script_file.exists():
                script_file.unlink()

    def __enter__(self) -> CodeSandbox:
        """Enter context manager — returns self for use in ``with`` blocks."""
        return self


# Module-level singleton retained as public compatibility state.
_code_executor: Any = None
_code_executor_lock = threading.Lock()


def get_subprocess_executor() -> Any:
    """Get or create the global subprocess-based code executor.

    Returns:
        Value produced for the caller.
    """
    global _code_executor
    if _code_executor is None:
        with _code_executor_lock:
            if _code_executor is None:
                from vetinari.sandbox_manager import CodeExecutor as _CodeExecutor

                _code_executor = _CodeExecutor()
    return _code_executor


def init_code_executor(**kwargs: Any) -> Any:
    """Create a fresh CodeExecutor backed by a new CodeSandbox.

    Returns:
        Value produced for the caller.
    """
    global _code_executor
    from vetinari.sandbox_manager import CodeExecutor as _CodeExecutor

    sandbox = CodeSandbox(**kwargs)
    _code_executor = _CodeExecutor(sandbox)
    return _code_executor


__all__ = [
    "ApplyChangesResult",
    "CodeSandbox",
    "ExecutionResult",
    "SandboxAuditEntry",
    "SandboxManager",
    "SandboxResult",
    "SandboxStatus",
    "SandboxType",
    "get_sandbox_manager",
    "get_subprocess_executor",
    "init_code_executor",
]
