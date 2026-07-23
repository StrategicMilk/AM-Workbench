"""Git Checkpoint — stash-based rollback for maker-checker quality loops.

Provides lightweight savepoint/rollback semantics using ``git stash``.
Called by the AgentGraph maker-checker loop to:
  - Save working state before an Inspector review
  - Commit accepted changes permanently
  - Roll back rejected changes to the last checkpoint

All operations use subprocess with a 30-second timeout to avoid blocking
the orchestration thread indefinitely.  Git availability is checked at
construction time; if git is unavailable the instance operates in no-op
mode so orchestration continues without checkpointing.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from vetinari.boundary_guards import require_nonempty

logger = logging.getLogger(__name__)


GIT_TIMEOUT = 30  # seconds for any single git command
_GIT_UNAVAILABLE_MSG = "[GitCheckpoint] git not available — operating in no-op mode"


@dataclass(frozen=True, slots=True)
class CheckpointResult:
    """Result from a git checkpoint operation.

    Args:
        success: True if the operation completed without error.
        message: Human-readable outcome description.
        stash_ref: Git stash ref created (e.g. ``stash@{0}``), or empty.
        error: Error message if success is False.
    """

    success: bool
    message: str = ""
    stash_ref: str = ""
    error: str = ""

    def __repr__(self) -> str:
        return f"CheckpointResult(success={self.success!r}, stash_ref={self.stash_ref!r}, error={self.error!r})"


def _run_git(args: list[str], cwd: Path) -> tuple[bool, str, str]:
    """Run a git command in the given directory.

    Args:
        args: List of arguments after ``git`` (e.g. ``["stash", "push"]``).
        cwd: Working directory for the git command.

    Returns:
        Tuple of (success, stdout, stderr).
    """
    try:
        git_exe = shutil.which("git") or "git"
        result = subprocess.run(
            [git_exe, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            encoding="utf-8",
        )
        return result.returncode == 0, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        logger.warning("git executable not found — checkpoint operations will fail")
        return False, "", "git executable not found"
    except subprocess.TimeoutExpired:
        logger.warning("git command timed out after %ds — checkpoint operation aborted", GIT_TIMEOUT)
        return False, "", f"git command timed out after {GIT_TIMEOUT}s"
    except OSError as exc:
        logger.warning("git command failed with OS error: %s — checkpoint operation aborted", exc)
        return False, "", str(exc)


class GitCheckpoint:
    """Stash-based savepoint/rollback for agent-driven code changes.

    Example::

        cp = GitCheckpoint(repo_path=Path("/path/to/repo"))
        result = cp.create_checkpoint(message="pre-inspector-review")
        # ... run Inspector ...
        if inspector_approved:
            cp.commit_accepted("Inspector approved: add auth module")
        else:
            cp.rollback(result.stash_ref)
    """

    def __init__(self, repo_path: Path | None = None) -> None:
        """Set up the checkpoint manager.

        Args:
            repo_path: Path to the git repository root.  Defaults to the
                current working directory.
        """
        self._repo_path = repo_path or Path.cwd()
        self._is_available = self._check_git_available()
        if not self._is_available:
            logger.warning(_GIT_UNAVAILABLE_MSG)

    def _check_git_available(self) -> bool:
        """Verify git is available and the path is a git repository.

        Returns:
            True if git commands can be run in the configured path.
        """
        ok, _, _ = _run_git(["rev-parse", "--git-dir"], self._repo_path)
        return ok

    def create_checkpoint(self, message: str = "vetinari-checkpoint") -> CheckpointResult:
        """Save the current working state as a git stash.

        Any tracked (modified/staged) files are stashed.  If there are no
        changes the operation succeeds with an empty stash_ref.

        Args:
            message: Description for the stash entry.

        Returns:
            CheckpointResult with the stash reference if created.
        """
        if not self._is_available:
            return CheckpointResult(success=True, message="no-op: git unavailable")

        ok, stdout, stderr = _run_git(
            ["stash", "push", "--include-untracked", "-m", message],
            self._repo_path,
        )

        if not ok:
            logger.warning("[GitCheckpoint] create_checkpoint failed: %s", stderr)
            return CheckpointResult(success=False, message="stash failed", error=stderr)

        # "No local changes to save" is a valid no-op outcome
        if "No local changes" in stdout:
            return CheckpointResult(success=True, message="No changes to checkpoint", stash_ref="")

        # Extract stash ref from output ("Saved working directory and index state ...")
        stash_ref = "stash@{0}"  # most recent stash is always stash@{0}
        logger.info("[GitCheckpoint] Checkpoint created: %s (%s)", stash_ref, message)
        return CheckpointResult(success=True, message=stdout, stash_ref=stash_ref)

    def rollback(self, stash_ref: str = "stash@{0}") -> CheckpointResult:
        """Restore working state from a stash, discarding current changes.

        First resets any uncommitted changes, then pops the stash.

        Args:
            stash_ref: Git stash reference to restore (default: latest stash).

        Returns:
            CheckpointResult indicating success or failure.

        Raises:
            RuntimeError: If git rollback is unavailable or no stash ref is provided.
        """
        if not self._is_available:
            raise RuntimeError("git checkpoint rollback unavailable: git is not available")

        try:
            stash_ref = require_nonempty(stash_ref, field_name="stash_ref")
        except ValueError as exc:
            raise RuntimeError("git checkpoint rollback requires a non-empty stash_ref") from exc

        # Discard current working tree changes
        ok_reset, _, err_reset = _run_git(["checkout", "--", "."], self._repo_path)
        if not ok_reset:
            logger.warning("[GitCheckpoint] checkout -- . failed: %s", err_reset)
            # Non-fatal — try stash pop anyway

        ok, stdout, stderr = _run_git(["stash", "pop", stash_ref], self._repo_path)
        if not ok:
            logger.error("[GitCheckpoint] rollback failed for %s: %s", stash_ref, stderr)
            return CheckpointResult(success=False, message="stash pop failed", error=stderr)

        logger.info("[GitCheckpoint] Rolled back to %s", stash_ref)
        return CheckpointResult(success=True, message=stdout, stash_ref=stash_ref)

    def commit_accepted(self, commit_message: str) -> CheckpointResult:
        """Stage all changes and create a commit, accepting the checkpoint.

        Drops the most recent stash if one exists so it does not interfere
        with future checkpoints.

        Args:
            commit_message: The git commit message.

        Returns:
            CheckpointResult indicating success or failure.
        """
        if not self._is_available:
            return CheckpointResult(success=True, message="no-op: git unavailable")

        # Stage all modified/untracked files
        ok_add, _, err_add = _run_git(["add", "-A"], self._repo_path)
        if not ok_add:
            logger.warning("[GitCheckpoint] git add -A failed: %s", err_add)

        ok_commit, stdout, stderr = _run_git(
            ["commit", "-m", commit_message, "--allow-empty"],
            self._repo_path,
        )
        if not ok_commit:
            logger.error("[GitCheckpoint] commit failed: %s", stderr)
            return CheckpointResult(success=False, message="commit failed", error=stderr)

        # Drop the stash that was protecting this checkpoint
        _run_git(["stash", "drop"], self._repo_path)

        logger.info("[GitCheckpoint] Accepted: committed with message %r", commit_message)
        return CheckpointResult(success=True, message=stdout)
