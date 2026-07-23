"""Sandbox-based code verification for the Inspector pipeline.

Runs code artifacts through the CodeSandbox before the Inspector accepts them.
Checks syntax (via ast.parse), import validity, and optional test execution.
Returns structured verification results that the Inspector uses for accept/reject.

This is a sub-stage of Inspector verification (step 5 of the pipeline):
    Intake → Planning → Execution → Sandbox → **Verify** → Learn
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)


_SANDBOX_DENIED_IMPORT_ROOTS = {
    "aiohttp",
    "httpx",
    "os",
    "requests",
    "socket",
    "subprocess",
    "sys",
    "urllib",
}


def denied_import_roots() -> frozenset[str]:
    """Return base sandbox denied roots plus operator-configured extras.

    Returns:
        Immutable set of denied top-level import roots.
    """
    raw_extras = os.environ.get("VETINARI_EXTRA_DENIED_ROOTS", "")
    extras = frozenset(root.strip() for root in raw_extras.split(",") if root.strip())
    return frozenset(_SANDBOX_DENIED_IMPORT_ROOTS) | extras


# ── Result dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SandboxVerification:
    """Result of sandbox verification on a code artifact.

    All three checks (syntax, imports, execution) must pass for ``passed``
    to be ``True``. Individual flags allow callers to surface which check
    failed and why.

    Attributes:
        passed: True only if ALL checks that were run passed.
        syntax_valid: ast.parse succeeded without SyntaxError.
        imports_valid: Every import statement resolved via importlib.
        execution_result: Captured stdout from the sandbox run, or None if
            test execution was skipped.
        error_message: Human-readable description of the first failure, or
            None when all checks passed.
        checks_run: Tuple of check names that were actually performed.
    """

    passed: bool
    syntax_valid: bool
    imports_valid: bool
    execution_result: str | None
    error_message: str | None
    checks_run: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            f"SandboxVerification(passed={self.passed!r}, "
            f"syntax_valid={self.syntax_valid!r}, "
            f"imports_valid={self.imports_valid!r})"
        )


@dataclass(frozen=True, slots=True)
class SandboxFailure:
    """Bounded failure object replacing raw PermissionError/OSError.

    Use this instead of letting OS-level exceptions propagate when the
    sandbox machinery itself fails (e.g. temp-dir creation, file write).

    Attributes:
        error_type: Machine-readable category — ``"permission_error"``,
            ``"timeout"``, ``"syntax_error"``, ``"import_error"``,
            ``"execution_error"``, or ``"unknown"``.
        message: Human-readable description of what went wrong.
        recoverable: Whether a retry without code changes might succeed
            (e.g. True for transient permission errors, False for syntax errors).
    """

    error_type: str
    message: str
    recoverable: bool


# ── Internal helpers ─────────────────────────────────────────────────────────


def _check_syntax(code: str) -> tuple[bool, str | None]:
    """Parse code with ast.parse and return (valid, error_message).

    Args:
        code: Python source code to check.

    Returns:
        Tuple of (is_valid, error_message). error_message is None when valid.
    """
    try:
        ast.parse(code)
        return True, None
    except SyntaxError as exc:
        logger.warning("Syntax error in submitted code at line %s: %s", exc.lineno, exc.msg)
        return False, f"SyntaxError at line {exc.lineno}: {exc.msg}"


def _extract_top_level_imports(code: str) -> list[str]:
    """Walk the AST to collect all top-level module names imported.

    Only collects the root module name (e.g. ``os`` from ``import os.path``
    or ``from os.path import join``). Non-parseable code returns an empty list.

    Args:
        code: Python source code to inspect.

    Returns:
        List of root module name strings (may contain duplicates).
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        logger.warning("Could not extract imports — code has syntax errors, returning empty list")
        return []

    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module.split(".")[0])
    return names


def _check_imports(code: str) -> tuple[bool, str | None]:
    """Verify all imports in code are resolvable via importlib.find_spec.

    Args:
        code: Python source code whose imports should be checked.

    Returns:
        Tuple of (all_valid, error_message). error_message names the first
        unresolvable module, or None when all imports resolve.
    """
    module_names = _extract_top_level_imports(code)
    # Deduplicate while preserving first-seen order
    seen: set[str] = set()
    unique_names: list[str] = []
    for name in module_names:
        if name not in seen:
            seen.add(name)
            unique_names.append(name)

    for name in unique_names:
        if name in denied_import_roots():
            return False, f"Import '{name}' is blocked by sandbox policy"

        try:
            spec = importlib.util.find_spec(name)
        except (ModuleNotFoundError, ValueError):
            # find_spec raises ValueError for some edge cases (e.g. relative
            # imports without a package context). Treat as unresolvable.
            spec = None

        if spec is None:
            return False, f"Cannot resolve import: '{name}'"

    return True, None


def _run_in_sandbox(code: str, timeout: int) -> tuple[bool, str | None, str | None]:
    """Execute code in a CodeSandbox subprocess and return (success, output, error).

    Creates a fresh CodeSandbox in a known-writable temp directory to avoid
    Windows ``%TEMP%`` permission issues. The temp directory is cleaned up in
    a ``finally`` block regardless of outcome.

    Args:
        code: Python source code to execute.
        timeout: Maximum execution time in seconds.

    Returns:
        Tuple of (success, stdout_output, error_message). On success,
        error_message is None. On failure, stdout_output may be None.
    """
    from vetinari.code_sandbox import CodeSandbox

    # Use mkdtemp in a known-writable location to avoid Windows %TEMP% issues.
    tmp_dir = Path(tempfile.mkdtemp(prefix="vetinari_verifier_"))
    try:
        sandbox = CodeSandbox(working_dir=str(tmp_dir), max_execution_time=timeout)
        result = sandbox.execute_python(code)
        output = result.output or result.stdout or ""
        if result.success:
            return True, output or None, None
        error = result.error or result.stderr or "Execution failed with no error message"
        return False, output or None, error
    finally:
        # Clean up temp directory; best-effort — log but don't raise on failure
        import shutil

        try:
            shutil.rmtree(str(tmp_dir), ignore_errors=True)
        except Exception as exc:
            logger.warning(
                "Failed to remove verifier temp directory %s — manual cleanup may be needed: %s",
                tmp_dir,
                exc,
            )


# ── Public API ───────────────────────────────────────────────────────────────


class SandboxVerifier:
    """Compatibility facade for sandbox verification helpers."""

    def denied_import_roots(self) -> frozenset[str]:
        """Return the active denied import roots."""
        return denied_import_roots()


def verify_code(
    code: str,
    filename: str = "artifact.py",
    run_tests: bool = False,
    timeout: int = 30,
) -> SandboxVerification:
    """Verify Python code through syntax, import, and optional execution checks.

    Args:
        code: Code value consumed by verify_code().
        filename: File path or file-like value consumed by the operation.
        run_tests: Run tests value consumed by verify_code().
        timeout: Timeout value controlling how long the operation may wait.

    Returns:
        Value produced for the caller.
    """
    checks_run: list[str] = []
    checks_run.append("syntax")
    syntax_valid, syntax_error = _check_syntax(code)
    if not syntax_valid:
        logger.debug("Syntax check failed for %s: %s", filename, syntax_error)
        return SandboxVerification(
            passed=False,
            syntax_valid=False,
            imports_valid=False,  # Cannot check imports on unparseable code
            execution_result=None,
            error_message=syntax_error,
            checks_run=tuple(checks_run),
        )
    checks_run.append("imports")
    imports_valid, import_error = _check_imports(code)
    if not imports_valid:
        logger.debug("Import check failed for %s: %s", filename, import_error)
        return SandboxVerification(
            passed=False,
            syntax_valid=True,
            imports_valid=False,
            execution_result=None,
            error_message=import_error,
            checks_run=tuple(checks_run),
        )
    execution_result: str | None = None
    if run_tests:
        checks_run.append("execution")
        exec_success, exec_output, exec_error = _run_in_sandbox(code, timeout)
        execution_result = exec_output
        if not exec_success:
            logger.debug("Execution check failed for %s: %s", filename, exec_error)
            return SandboxVerification(
                passed=False,
                syntax_valid=True,
                imports_valid=True,
                execution_result=execution_result,
                error_message=exec_error,
                checks_run=tuple(checks_run),
            )
    logger.debug("All checks passed for %s (checks: %s)", filename, checks_run)
    return SandboxVerification(
        passed=True,
        syntax_valid=True,
        imports_valid=True,
        execution_result=execution_result,
        error_message=None,
        checks_run=tuple(checks_run),
    )


def verify_code_safe(
    code: str,
    **kwargs: object,
) -> SandboxVerification:
    """Verify a code artifact, guaranteed to never raise an exception.

    Wraps ``verify_code`` in a broad try/except. Any unhandled error — including
    ``PermissionError``, ``OSError``, or unexpected runtime failures — is caught
    and returned as a ``passed=False`` result. Fails closed: an error in the
    verifier itself is treated as a failed verification.

    Args:
        code: Python source code to verify.
        **kwargs: Forwarded to ``verify_code`` (filename, run_tests, timeout).

    Returns:
        SandboxVerification with ``passed=False`` and a descriptive
        ``error_message`` on any unhandled error.
    """
    try:
        unsupported_kwargs = sorted(set(kwargs) - {"filename", "run_tests", "timeout"})
        if unsupported_kwargs:
            raise TypeError(f"verify_code() got unsupported keyword argument(s): {unsupported_kwargs}")
        return verify_code(
            code,
            filename=cast(str, kwargs.get("filename", "artifact.py")),
            run_tests=cast(bool, kwargs.get("run_tests", False)),
            timeout=cast(int, kwargs.get("timeout", 30)),
        )
    except PermissionError as exc:
        logger.warning(
            "Verifier encountered a permission error — treating as verification failure: %s",
            exc,
        )
        return SandboxVerification(
            passed=False,
            syntax_valid=False,
            imports_valid=False,
            execution_result=None,
            error_message=f"Permission error during verification: {exc}",
            checks_run=("safe_wrapper",),
        )
    except Exception as exc:
        logger.warning(
            "Verifier encountered an unexpected error — treating as verification failure: %s",
            exc,
        )
        return SandboxVerification(
            passed=False,
            syntax_valid=False,
            imports_valid=False,
            execution_result=None,
            error_message=f"Unexpected verification error: {exc}",
            checks_run=("safe_wrapper",),
        )


def cleanup_sandbox_artifacts(directory: Path) -> int:
    """Remove leftover ``script_*.py`` files from a sandbox working directory.

    Orphaned script files are left behind when the sandbox subprocess was
    killed mid-execution or a ``finally`` cleanup block failed. This function
    removes them.

    Args:
        directory: Path to the directory to scan for leftover script files.

    Returns:
        Count of files successfully removed.
    """
    if not directory.is_dir():
        logger.debug("cleanup_sandbox_artifacts: %s is not a directory — skipping", directory)
        return 0

    removed = 0
    for script_file in directory.glob("script_*.py"):
        try:
            script_file.unlink()
            removed += 1
            logger.debug("Removed orphaned sandbox artifact: %s", script_file)
        except OSError as exc:
            logger.warning(
                "Could not remove sandbox artifact %s — file may be locked: %s",
                script_file,
                exc,
            )

    if removed:
        logger.info("Cleaned up %d orphaned sandbox artifact(s) from %s", removed, directory)

    return removed


__all__ = [
    "SandboxFailure",
    "SandboxVerification",
    "SandboxVerifier",
    "cleanup_sandbox_artifacts",
    "denied_import_roots",
    "verify_code",
    "verify_code_safe",
]
