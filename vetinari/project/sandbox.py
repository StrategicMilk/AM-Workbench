"""Sandbox execution — test code changes safely before committing them.

This module is step 2 of Vetinari's autonomous improvement pipeline:
Scan → **Sandbox** → Fix → Verify.

Before any automated change is committed to the target project, it is first
applied to a temporary git worktree (the "sandbox"). The sandbox runs the
project's test suite and linter so a bad fix cannot reach the real branch.
If the checks pass, the caller can promote the change to a real branch via
git_integration. If they fail, the sandbox is discarded with no side effects.

Pipeline role: Safety gate. Every proposed change must pass sandbox checks
before it is eligible for a commit or pull request.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vetinari.learning.atomic_writers import _write_text_atomic
from vetinari.sandbox_policy import _SAFE_ENV_VARS
from vetinari.security.sandbox import RustSandboxAuthorityBridge

logger = logging.getLogger(__name__)


# Path to the sandbox policy relative to the Vetinari package root.
_POLICY_PATH_PARTS = ("config", "sandbox_policy.yaml")
_POLICY_PATH = Path(__file__).resolve().parent.parent.parent.joinpath(*_POLICY_PATH_PARTS)

# Default timeouts used when the policy file cannot be loaded.
_DEFAULT_TIMEOUT: int = 300  # seconds
_DEFAULT_MEMORY_MB: int = 2048
_PYTHON_EXE = shutil.which("python") or shutil.which("python3") or "python"
_RUFF_EXE = shutil.which("ruff") or "ruff"


# Resolved executable paths — resolved once at module load so hot-path code
# does not call shutil.which() on every subprocess call.
def _policy_path() -> Path:
    return _POLICY_PATH


def _git_exe() -> str:
    return shutil.which("git") or "git"


def _python_exe() -> str:
    return _PYTHON_EXE


def _ruff_exe() -> str:
    return _RUFF_EXE


def _safe_subprocess_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k in _SAFE_ENV_VARS}


# -- Data structures ----------------------------------------------------------


@dataclass
class ProjectSandboxResult:
    """Outcome of running checks inside a sandbox worktree.

    Attributes:
        success: True only when both tests and lint passed.
        test_passed: Whether the test suite exited with code 0.
        lint_passed: Whether the linter exited with code 0.
        errors: Human-readable error messages collected during the run.
        worktree_path: Path to the sandbox worktree, or None when the
            worktree was cleaned up before this result was returned.
    """

    success: bool
    test_passed: bool
    lint_passed: bool
    errors: list[str] = field(default_factory=list)
    worktree_path: Path | None = None

    def __repr__(self) -> str:
        return (
            f"SandboxResult(success={self.success!r},"
            f" test_passed={self.test_passed!r},"
            f" lint_passed={self.lint_passed!r})"
        )


# -- Public API ---------------------------------------------------------------


def create_sandbox(project_path: Path) -> Path:
    """Create an isolated git worktree for safely testing changes.

    The worktree is created in the system temporary directory.  The caller
    is responsible for calling :func:`cleanup_sandbox` when done.

    Args:
        project_path: Absolute path to the target project's git repository
            root. The repository must already be initialised with ``git init``.

    Returns:
        Path to the newly created worktree directory.

    Raises:
        RuntimeError: If git is not installed or the worktree cannot be
            created (e.g. the project path is not a git repository).
    """
    sandbox_dir = Path(tempfile.mkdtemp(prefix="vetinari_sandbox_"))
    branch_name = f"vetinari-sandbox-{sandbox_dir.name}"

    result = subprocess.run(
        [_git_exe(), "worktree", "add", "--detach", str(sandbox_dir)],
        capture_output=True,
        text=True,
        cwd=str(project_path),
        timeout=30,
    )

    if result.returncode != 0:
        # Clean up the temp dir we already created before raising.
        shutil.rmtree(sandbox_dir, ignore_errors=True)
        raise RuntimeError(f"Could not create git worktree for {project_path} — git reported: {result.stderr.strip()}")

    logger.debug("Created sandbox worktree at %s (branch %s)", sandbox_dir, branch_name)
    return sandbox_dir


def apply_changes(sandbox_path: Path, changes: dict[str, str]) -> bool:
    """Write a set of file changes into the sandbox worktree.

    Each key in *changes* is a path relative to the sandbox root; each value
    is the complete new file content. New parent directories are created
    automatically.

    Args:
        sandbox_path: Root of the sandbox worktree (from :func:`create_sandbox`).
        changes: Mapping of relative file path to new file content.

    Returns:
        True when all files were written successfully, False when any write
        failed (errors are logged with the filename that failed).
    """
    all_ok = True
    authority = RustSandboxAuthorityBridge(sandbox_path)
    resolved_sandbox = sandbox_path.resolve()
    for relative_path, content in changes.items():
        # Resolve after joining so that any `../` components in the key are
        # collapsed. Reject the path if it escapes the sandbox root — this
        # prevents path-traversal attacks where a change key like
        # `../../etc/passwd` would otherwise write outside the sandbox.
        try:
            resolved_target = authority.require_path("write", relative_path)
        except PermissionError as exc:
            logger.warning(
                "Path traversal rejected in apply_changes - key %r denied by Rust sandbox bridge: %s",
                relative_path,
                exc,
            )
            all_ok = False
            continue
        if not resolved_target.is_relative_to(resolved_sandbox):
            logger.warning(
                "Path traversal rejected in apply_changes — key %r resolves to %s which is outside sandbox root %s",
                relative_path,
                resolved_target,
                resolved_sandbox,
            )
            all_ok = False
            continue
        try:
            resolved_target.parent.mkdir(parents=True, exist_ok=True)
            _write_text_atomic(resolved_target, content)
        except OSError as exc:
            logger.warning(
                "Could not write %s to sandbox — change will be skipped: %s",
                relative_path,
                exc,
            )
            all_ok = False
    return all_ok


def run_checks(sandbox_path: Path) -> ProjectSandboxResult:
    """Run tests and lint inside the sandbox worktree.

    Reads timeout and memory limits from ``config/sandbox_policy.yaml``.
    If the policy file is missing the defaults are used so the pipeline does
    not abort on a missing config.

    Args:
        sandbox_path: Root of the sandbox worktree to check.

    Returns:
        A :class:`SandboxResult` describing what passed and what failed.
    """
    policy = _load_policy()
    timeout = policy.get("timeout_seconds", _DEFAULT_TIMEOUT)

    test_passed, test_errors = _run_tests(sandbox_path, timeout)
    lint_passed, lint_errors = _run_lint(sandbox_path, timeout)

    errors = test_errors + lint_errors
    success = test_passed and lint_passed

    return ProjectSandboxResult(
        success=success,
        test_passed=test_passed,
        lint_passed=lint_passed,
        errors=errors,
        worktree_path=sandbox_path,
    )


def cleanup_sandbox(sandbox_path: Path) -> None:
    """Remove a sandbox worktree and free its disk space.

    Uses ``git worktree remove`` to cleanly deregister the worktree, then
    falls back to a plain directory removal if git fails (e.g. when the
    project root is no longer available).

    Args:
        sandbox_path: Path to the sandbox worktree created by
            :func:`create_sandbox`.
    """
    try:
        subprocess.run(
            [_git_exe(), "worktree", "remove", "--force", str(sandbox_path)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        logger.debug("Removed sandbox worktree %s", sandbox_path)
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.warning(
            "git worktree remove failed for %s — falling back to rmtree: %s",
            sandbox_path,
            exc,
        )
    finally:
        shutil.rmtree(sandbox_path, ignore_errors=True)


def execute_in_sandbox(project_path: Path, changes: dict[str, str]) -> ProjectSandboxResult:
    """Create a sandbox, apply changes, run checks, then clean up.

    This is the all-in-one entry point for callers that do not need fine-
    grained control over the sandbox lifecycle.

    Args:
        project_path: Absolute path to the target project's git root.
        changes: Mapping of relative file path to new file content.

    Returns:
        A :class:`SandboxResult` with ``worktree_path=None`` because the
        worktree is cleaned up before returning.
    """
    sandbox_path: Path | None = None
    try:
        sandbox_path = create_sandbox(project_path)
        if not apply_changes(sandbox_path, changes):
            return ProjectSandboxResult(
                success=False,
                test_passed=False,
                lint_passed=False,
                errors=["apply_changes failed; sandbox checks were not run"],
                worktree_path=None,
            )
        result = run_checks(sandbox_path)
        result.worktree_path = None  # cleaned up below
        return result
    except RuntimeError as exc:
        logger.warning(
            "Sandbox creation failed for %s — cannot verify changes: %s",
            project_path,
            exc,
        )
        return ProjectSandboxResult(
            success=False,
            test_passed=False,
            lint_passed=False,
            errors=[str(exc)],
            worktree_path=None,
        )
    finally:
        if sandbox_path is not None:
            cleanup_sandbox(sandbox_path)


# -- Private helpers ----------------------------------------------------------


def _load_policy() -> dict:
    """Load sandbox policy settings from config/sandbox_policy.yaml.

    Returns:
        Dictionary of policy values from the ``sandbox.external`` section,
        or a default dict when the file is missing or unreadable.
    """
    try:
        policy_path = _policy_path()
        raw = policy_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        return data.get("sandbox", {}).get("external", {})
    except FileNotFoundError:
        logger.debug("sandbox_policy.yaml not found — using defaults")
        return {}
    except (yaml.YAMLError, OSError) as exc:
        logger.warning(
            "Could not read sandbox policy from %s — using defaults: %s",
            _policy_path(),
            exc,
        )
        return {}


def _run_tests(sandbox_path: Path, timeout: int) -> tuple[bool, list[str]]:
    """Execute the test suite inside the sandbox directory.

    Requires pytest to be present. If pytest is not installed the check fails
    closed — a missing test runner is not treated as passing evidence.

    Args:
        sandbox_path: Root of the sandbox worktree.
        timeout: Maximum seconds to allow the test run.

    Returns:
        A ``(passed, errors)`` tuple where *passed* is True when the suite
        exited with code 0 and *errors* contains any failure messages.
    """
    try:
        result = subprocess.run(
            [_python_exe(), "-m", "pytest", "--tb=short", "-q"],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(sandbox_path),
            env=_safe_subprocess_env(),
        )
        if result.returncode != 0:
            return False, [result.stdout[-2000:] or result.stderr[-2000:]]
        return True, []
    except FileNotFoundError:
        logger.warning("pytest not available in sandbox environment — cannot verify test suite, treating as failed")
        return False, ["pytest not available — cannot verify"]
    except subprocess.TimeoutExpired:
        logger.warning("Test suite timed out after %ds in sandbox — treating as failed", timeout)
        return False, [f"Test suite timed out after {timeout}s"]


def _run_lint(sandbox_path: Path, timeout: int) -> tuple[bool, list[str]]:
    """Run ruff linting inside the sandbox directory.

    Args:
        sandbox_path: Root of the sandbox worktree.
        timeout: Maximum seconds to allow the lint run.

    Returns:
        A ``(passed, errors)`` tuple where *passed* is True when ruff
        reported zero violations.
    """
    try:
        result = subprocess.run(
            [_ruff_exe(), "check", str(sandbox_path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(sandbox_path),
            env=_safe_subprocess_env(),
        )
        if result.returncode != 0:
            return False, [result.stdout[-2000:] or result.stderr[-2000:]]
        return True, []
    except FileNotFoundError:
        logger.warning("ruff not available in sandbox environment — cannot lint, treating as failed")
        return False, ["ruff not available — cannot lint"]
    except subprocess.TimeoutExpired:
        logger.warning("Lint check timed out after %ds in sandbox — treating as failed", timeout)
        return False, [f"Lint check timed out after {timeout}s"]
