"""Git Operations Tool.

Provides safe git operations for Vetinari agents, including low-level git
commands and higher-level helpers for conventional commits, branch management,
PR description generation, and conflict detection.

All commands run via ``subprocess`` with ``shell=False``.  The working
directory is locked to the project root.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from vetinari.constants import GIT_OPERATION_TIMEOUT
from vetinari.execution_context import ToolPermission, get_context_manager
from vetinari.security.fail_closed import confine_to_root, sanitize_untrusted_text
from vetinari.tool_interface import (
    Tool,
    ToolCategory,
    ToolMetadata,
    ToolParameter,
    ToolResult,
)
from vetinari.tools.git_operations_core import (
    COMMIT_TYPES as COMMIT_TYPES,
)
from vetinari.tools.git_operations_core import (
    BranchInfo as BranchInfo,
)
from vetinari.tools.git_operations_core import (
    CommitInfo as CommitInfo,
)
from vetinari.tools.git_operations_core import (
    ConflictInfo as ConflictInfo,
)
from vetinari.tools.git_operations_core import (
    GitOperationsHighLevelMixin as GitOperationsHighLevelMixin,
)
from vetinari.tools.git_operations_core import (
    GitResult as GitResult,
)

logger = logging.getLogger(__name__)


class GitOperations(GitOperationsHighLevelMixin):
    """Low-level git operations scoped to a repository root."""

    TIMEOUT = 30

    def __init__(self, repo_path: str | Path):
        if not repo_path:
            raise ValueError("repo_path is required - process cwd is not a safe default for sandboxed git operations")
        self.repo = Path(repo_path).resolve()
        self._git = shutil.which("git") or "git"

    def _run(self, args: list[str], timeout: int | None = None) -> GitResult:
        """Execute ``git <args>`` and return the result."""
        cmd = [self._git, *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout or self.TIMEOUT,
                cwd=str(self.repo),
            )
            return GitResult(
                success=proc.returncode == 0,
                stdout=proc.stdout.strip(),
                stderr=proc.stderr.strip(),
                return_code=proc.returncode,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "git %s in %s took too long (>%ss) - aborting",
                " ".join(args),
                self.repo,
                timeout or self.TIMEOUT,
            )
            return GitResult(success=False, stderr="git command timed out", return_code=-1)
        except FileNotFoundError:
            logger.warning("git executable not found - ensure git is installed and on PATH (repo: %s)", self.repo)
            return GitResult(success=False, stderr="git executable not found", return_code=-1)
        except Exception as exc:
            logger.warning(
                "git %s failed with unexpected error in repo %s: %s - returning failed GitResult",
                " ".join(args),
                self.repo,
                exc,
            )
            return GitResult(success=False, stderr=str(exc), return_code=-1)

    def status(self) -> GitResult:
        """Run ``git status --porcelain`` and return the result."""
        return self._run(["status", "--porcelain"])

    def log(self, n: int = 10) -> GitResult:
        """Run ``git log --oneline`` for the most recent commits."""
        return self._run(["log", f"-{n}", "--oneline"])

    def diff(self, base: str = "HEAD", head: str = "") -> GitResult:
        """Show differences between commits, the index, or the working tree.

        Args:
            base: Base value consumed by diff().
            head: Head value consumed by diff().

        Returns:
            Value produced for the caller.
        """
        args = ["diff", base]
        if head:
            args.append(head)
        return self._run(args)

    def init_repo(self) -> GitResult:
        """Initialize a new git repository in the configured repo path."""
        return self._run(["init"])

    def add(self, files: list[str] | None = None) -> GitResult:
        """Stage files for the next commit.

        Returns:
            Value produced for the caller.
        """
        if files:
            return self._run(["add", *files])
        return self._run(["add", "."])

    def commit(self, message: str) -> GitResult:
        """Create a commit with the given message from currently staged changes."""
        return self._run(["commit", "-m", message])

    def create_branch(self, name: str) -> GitResult:
        """Create and switch to a new branch via ``git checkout -b``."""
        return self._run(["checkout", "-b", name])

    def checkout(self, branch: str) -> GitResult:
        """Switch to an existing branch."""
        return self._run(["checkout", branch])

    def current_branch(self) -> GitResult:
        """Return the name of the currently checked-out branch."""
        return self._run(["rev-parse", "--abbrev-ref", "HEAD"])

    def push(self, remote: str = "origin", branch: str = "") -> GitResult:
        """Push local commits to a remote repository.

        Args:
            remote: Remote value consumed by push().
            branch: Branch value consumed by push().

        Returns:
            Value produced for the caller.
        """
        args = ["push", remote]
        if branch:
            args.append(branch)
        return self._run(args, timeout=GIT_OPERATION_TIMEOUT)

    def pull(self, remote: str = "origin", branch: str = "") -> GitResult:
        """Fetch and merge changes from a remote repository.

        Args:
            remote: Remote value consumed by pull().
            branch: Branch value consumed by pull().

        Returns:
            Value produced for the caller.
        """
        args = ["pull", remote]
        if branch:
            args.append(branch)
        return self._run(args, timeout=GIT_OPERATION_TIMEOUT)

    def stash(self, pop: bool = False) -> GitResult:
        """Stash or restore uncommitted changes."""
        return self._run(["stash", "pop"] if pop else ["stash"])

    def tag(self, name: str, message: str = "") -> GitResult:
        """Create a git tag on the current commit.

        Args:
            name: Name used to identify the target object.
            message: Message value consumed by tag().

        Returns:
            Value produced for the caller.
        """
        if message:
            return self._run(["tag", "-a", name, "-m", message])
        return self._run(["tag", name])


class GitTool:
    """Sandboxed git command runner scoped to one repository root."""

    TIMEOUT = GitOperations.TIMEOUT

    def __init__(self, repo_root: str | Path):
        if not repo_root:
            raise ValueError("repo_root is required")
        self.repo_root = Path(repo_root).resolve()
        self._git = shutil.which("git") or "git"

    def run_command(self, args: list[str]) -> GitResult:
        """Run ``git`` with sandbox validation for path-like arguments.

        Args:
            args: Git CLI arguments without the leading ``git`` executable.

        Returns:
            GitResult containing stdout, stderr, and the process return code.

        Raises:
            ValueError: If a path-like argument escapes ``repo_root``.
        """
        self._validate_args(args)
        try:
            proc = subprocess.run(
                [self._git, *args],
                capture_output=True,
                text=True,
                timeout=self.TIMEOUT,
                cwd=str(self.repo_root),
            )
        except subprocess.TimeoutExpired:
            logger.warning("git %s in %s timed out", " ".join(args), self.repo_root)
            return GitResult(success=False, stderr="git command timed out", return_code=-1)
        except FileNotFoundError:
            logger.warning("git executable not found for repo %s", self.repo_root)
            return GitResult(success=False, stderr="git executable not found", return_code=-1)

        return GitResult(
            success=proc.returncode == 0,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
            return_code=proc.returncode,
        )

    def _validate_args(self, args: list[str]) -> None:
        """Reject path-like git arguments that resolve outside ``repo_root``."""
        for arg in args:
            sanitize_untrusted_text(arg, max_length=4096)
            if not _looks_like_git_path_arg(arg):
                continue
            candidate = Path(arg)
            confine_to_root(self.repo_root, candidate)


def _looks_like_git_path_arg(arg: str) -> bool:
    """Return whether a git argument should be treated as a filesystem path."""
    if arg in {".", ".."} or arg.startswith("~"):
        return True
    return "/" in arg or "\\" in arg


# ---------------------------------------------------------------------------
# Tool wrapper
# ---------------------------------------------------------------------------


class GitOperationsTool(Tool):
    """Vetinari Tool wrapper around :class:`GitOperations`."""

    def __init__(self, repo_path: str):
        self._git = GitOperations(repo_path)
        metadata = ToolMetadata(
            name="git_operations",
            description="Version control operations via git CLI",
            version="1.0.0",
            category=ToolCategory.GIT_OPERATIONS,
            required_permissions=[],
            parameters=[
                ToolParameter(
                    name="operation",
                    type=str,
                    description="Operation: status, log, diff, init, add, commit, branch, checkout, push, pull, stash, tag, current_branch",
                    required=True,
                ),
                ToolParameter(name="message", type=str, description="Commit/tag message", required=False),
                ToolParameter(name="branch", type=str, description="Branch name", required=False),
                ToolParameter(name="files", type=list, description="Files to add", required=False),
                ToolParameter(name="n", type=int, description="Number of log entries", required=False, default=10),
                ToolParameter(name="remote", type=str, description="Remote name", required=False, default="origin"),
            ],
        )
        super().__init__(metadata)

    def execute(self, **kwargs) -> ToolResult:
        """Dispatch a git operation based on the ``operation`` keyword argument.

        Args:
            **kwargs: Must include ``operation`` (str) plus any operation-specific
                parameters such as ``message``, ``branch``, ``files``, or ``n``.

        Returns:
            ToolResult with the operation output or an error description.
        """
        op = kwargs.get("operation", "")

        # Enforce granular git permissions.
        # Read-only ops (status, log, diff, current_branch) require GIT_READ —
        # they are safe in PLANNING mode.
        # Write ops (commit, add, branch, checkout, stash, tag, pull, init) require
        # GIT_COMMIT — only allowed in EXECUTION mode.
        # push is irreversible and requires GIT_PUSH, which also requires confirmation.
        self._enforce_operation_permission(op)

        try:
            if op == "status":
                r = self._git.status()
            elif op == "log":
                r = self._git.log(n=kwargs.get("n", 10))
            elif op == "diff":
                r = self._git.diff(
                    base=kwargs.get("base", "HEAD"),
                    head=kwargs.get("head", ""),
                )
            elif op == "init":
                r = self._git.init_repo()
            elif op == "add":
                r = self._git.add(files=kwargs.get("files"))
            elif op == "commit":
                msg = kwargs.get("message", "")
                if not msg:
                    return ToolResult(success=False, output="", error="message is required for commit")
                r = self._git.commit(msg)
            elif op == "branch":
                name = kwargs.get("branch", "")
                if not name:
                    return ToolResult(success=False, output="", error="branch name is required")
                r = self._git.create_branch(name)
            elif op == "checkout":
                name = kwargs.get("branch", "")
                if not name:
                    return ToolResult(success=False, output="", error="branch name is required")
                r = self._git.checkout(name)
            elif op == "current_branch":
                r = self._git.current_branch()
            elif op == "push":
                r = self._git.push(
                    remote=kwargs.get("remote", "origin"),
                    branch=kwargs.get("branch", ""),
                )
            elif op == "pull":
                r = self._git.pull(
                    remote=kwargs.get("remote", "origin"),
                    branch=kwargs.get("branch", ""),
                )
            elif op == "stash":
                r = self._git.stash(pop=kwargs.get("pop", False))
            elif op == "tag":
                name = kwargs.get("name", "")
                if not name:
                    return ToolResult(success=False, output="", error="tag name is required")
                r = self._git.tag(name, message=kwargs.get("message", ""))
            else:
                return ToolResult(success=False, output="", error=f"Unknown operation: {op}")

            return ToolResult(
                success=r.success,
                output=r.stdout or r.stderr,
                error=r.stderr if not r.success else "",
            )

        except Exception as exc:
            logger.exception("GitOperationsTool error")
            return ToolResult(success=False, output="", error=str(exc))

    @staticmethod
    def _enforce_operation_permission(op: str) -> None:
        """Enforce granular git permissions for the requested operation."""
        ctx = get_context_manager()
        if op in ("status", "log", "diff", "current_branch"):
            ctx.enforce_permission(ToolPermission.GIT_READ, f"git_{op}")
        elif op in ("commit", "add", "init", "stash", "tag", "branch", "checkout", "pull"):
            ctx.enforce_permission(ToolPermission.GIT_COMMIT, f"git_{op}")
        elif op == "push":
            ctx.enforce_permission(ToolPermission.GIT_PUSH, "git_push")
