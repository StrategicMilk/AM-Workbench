"""Low-level git operations for `vetinari.tools.git_tool`."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


# Conventional commit type registry
COMMIT_TYPES: dict[str, str] = {
    "feat": "A new feature",
    "fix": "A bug fix",
    "refactor": "Code change that neither fixes a bug nor adds a feature",
    "docs": "Documentation only changes",
    "test": "Adding missing tests or correcting existing tests",
    "chore": "Changes to build process or auxiliary tools",
    "style": "Formatting, missing semicolons, etc; no code change",
    "perf": "Performance improvement",
    "ci": "Changes to CI configuration files and scripts",
}


@dataclass
class CommitInfo:
    """Structured commit information following Conventional Commits."""

    type: str  # feat, fix, refactor, etc.
    scope: str | None = None
    description: str = ""
    body: str = ""
    breaking: bool = False
    files_changed: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"CommitInfo(type={self.type!r}, scope={self.scope!r}, breaking={self.breaking!r})"

    def format_message(self) -> str:
        """Format as a conventional commit message string.

        Returns:
            The formatted conventional commit message.
        """
        prefix = self.type
        if self.scope:
            prefix = f"{self.type}({self.scope})"
        if self.breaking:
            prefix += "!"
        msg = f"{prefix}: {self.description}"
        if self.body:
            msg += f"\n\n{self.body}"
        return msg


@dataclass(frozen=True, slots=True)
class BranchInfo:
    """Information about a git branch."""

    name: str
    is_current: bool = False
    ahead: int = 0
    behind: int = 0
    last_commit: str = ""
    created_from: str = ""

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"BranchInfo(name={self.name!r}, is_current={self.is_current!r}, ahead={self.ahead!r})"


@dataclass(frozen=True, slots=True)
class ConflictInfo:
    """Information about a merge conflict."""

    file_path: str
    conflict_type: str  # "content", "rename", "delete"
    ours_content: str = ""
    theirs_content: str = ""
    suggestion: str = ""

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"ConflictInfo(file_path={self.file_path!r}, conflict_type={self.conflict_type!r})"


@dataclass(frozen=True, slots=True)
class GitResult:
    """Result of a git operation."""

    success: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0

    def __repr__(self) -> str:
        return f"GitResult(success={self.success!r}, return_code={self.return_code!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)


class GitOperationsHighLevelMixin:
    """High-level git helpers mixed into GitOperations."""

    if TYPE_CHECKING:
        _run: Any

    def classify_changes(self, diff_text: str | None = None) -> str:
        """Classify staged (or unstaged) changes into a conventional commit type.

        If *diff_text* is ``None`` the method inspects the currently staged
        changes, falling back to unstaged changes.

        Args:
            diff_text: Optional diff text to classify. When ``None`` the live
                staged diff is used.

        Returns:
            A conventional commit type string (e.g. ``"feat"``, ``"fix"``).
        """
        if diff_text is None:
            r = self._run(["diff", "--cached", "--stat"])
            diff_text = r.stdout

        if not diff_text:
            r = self._run(["diff", "--stat"])
            diff_text = r.stdout

        lower = diff_text.lower()
        files = self._paths_from_diff_stat(diff_text)

        if files and any(self._is_docs_path(path) for path in files):
            return "docs"
        if files and any(self._is_ci_path(path) for path in files):
            return "ci"
        if files and any(self._is_test_path(path) for path in files):
            return "test"
        if re.search(r"\b(readme|docs?|documentation)\b", lower):
            return "docs"
        if re.search(r"\b(ci|workflow|pipeline)\b", lower) or ".github/" in lower:
            return "ci"
        if re.search(r"\b(fix|bug|patch|hotfix)\b", lower):
            return "fix"
        if re.search(r"\b(refactor|rename|move|reorganize)\b", lower):
            return "refactor"
        if re.search(r"\b(perf|optim|speed|cache)\b", lower):
            return "perf"
        return "feat"

    def generate_commit_message(
        self,
        description: str | None = None,
        scope: str | None = None,
    ) -> CommitInfo:
        """Generate a conventional commit message from staged changes.

        Args:
            description: Optional commit description. When ``None`` one is
                inferred from the changed file paths.
            scope: Optional conventional-commit scope. When ``None`` one is
                inferred from the changed file paths.

        Returns:
            A :class:`CommitInfo` instance ready to be formatted.
        """
        diff_stat_result = self._run(["diff", "--cached", "--stat"])
        diff_names_result = self._run(["diff", "--cached", "--name-only"])

        diff_stat = diff_stat_result.stdout
        diff_names = diff_names_result.stdout

        files = [f.strip() for f in diff_names.split("\n") if f.strip()] if diff_names else []
        commit_type = self.classify_changes(diff_stat)

        if not description:
            description = self._infer_description(files)

        if not scope and files:
            scope = self._infer_scope(files)

        return CommitInfo(
            type=commit_type,
            scope=scope,
            description=description,
            files_changed=files,
        )

    def generate_pr_description(self, base_branch: str = "main") -> dict[str, Any]:
        """Generate a PR description from commit history since *base_branch*.

        Args:
            base_branch: The branch to compare against. Defaults to ``"main"``.

        Returns:
            A dict with keys ``"title"`` (str), ``"body"`` (str), and
            ``"commits"`` (int).
        """
        log_result = self._run(["log", f"{base_branch}..HEAD", "--oneline"])
        diff_result = self._run(["diff", f"{base_branch}..HEAD", "--stat"])

        log_output = log_result.stdout
        diff_stat = diff_result.stdout

        commits = [line.strip() for line in log_output.split("\n") if line.strip()] if log_output else []

        title = commits[0].split(" ", 1)[1] if commits else "Update"

        body_lines = ["## Summary", ""]
        for c in commits:
            parts = c.split(" ", 1)
            body_lines.append(f"- {parts[1] if len(parts) > 1 else c}")

        body_lines.extend(["", "## Changes", "", diff_stat or "No changes detected"])

        return {
            "title": title[:70],
            "body": "\n".join(body_lines),
            "commits": len(commits),
        }

    def detect_conflicts(self, target_branch: str = "main") -> list[ConflictInfo]:
        """Detect potential merge conflicts with *target_branch*.

        Performs a dry-run merge and parses the git output for CONFLICT lines.

        Args:
            target_branch: The branch to test merging into the current branch.
                Defaults to ``"main"``.

        Returns:
            A list of :class:`ConflictInfo` instances, one per conflicting file.
            Returns an empty list when no conflicts are detected.
        """
        result = self._run(["merge", "--no-commit", "--no-ff", target_branch, "--dry-run"])

        conflicts: list[ConflictInfo] = []
        if result.return_code != 0 and "conflict" in result.stderr.lower():
            for line in result.stderr.split("\n"):
                if "CONFLICT" in line:
                    match = re.search(r"CONFLICT.*?:\s*(.+)", line)
                    file_path = match.group(1).strip() if match else "unknown"
                    conflict_type = "content"
                    if "rename" in line.lower():
                        conflict_type = "rename"
                    elif "delete" in line.lower():
                        conflict_type = "delete"
                    conflicts.append(ConflictInfo(file_path=file_path, conflict_type=conflict_type))
        return conflicts

    # -- private helpers ----------------------------------------------------

    def _infer_description(self, files: list[str]) -> str:
        """Infer a commit description from changed file paths.

        Args:
            files: List of changed file paths.

        Returns:
            A short human-readable description string.
        """
        if not files:
            return "update files"
        if len(files) == 1:
            return f"update {files[0].split('/')[-1]}"
        common_dir = self._common_directory(files)
        if common_dir:
            return f"update {common_dir} ({len(files)} files)"
        return f"update {len(files)} files"

    @staticmethod
    def _paths_from_diff_stat(diff_text: str) -> list[str]:
        """Extract changed file paths from git stat/name-only style output."""
        paths: list[str] = []
        for raw_line in diff_text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith((" ", "-")) or (" file" in line and "changed" in line):
                continue
            path = line.split("|", 1)[0].strip() if "|" in line else line
            if not path or (" " in path and "/" not in path and "\\" not in path and "." not in path):
                continue
            paths.append(path.replace("\\", "/"))
        return paths

    @staticmethod
    def _is_docs_path(path: str) -> bool:
        lowered = path.lower()
        return lowered.endswith(".md") or lowered.startswith("docs/") or "readme" in lowered

    @staticmethod
    def _is_ci_path(path: str) -> bool:
        lowered = path.lower()
        return lowered.startswith((".github/", "ci/")) or "/workflows/" in lowered or "pipeline" in lowered

    @staticmethod
    def _is_test_path(path: str) -> bool:
        lowered = path.lower()
        return (
            lowered.startswith(("tests/", "test/", "test_"))
            or "/tests/" in lowered
            or lowered.endswith(("_test.py", ".test.js", ".test.ts", ".spec.js", ".spec.ts"))
        )

    @staticmethod
    def _infer_scope(files: list[str]) -> str | None:
        """Infer the conventional-commit scope from changed file paths.

        Args:
            files: List of changed file paths.

        Returns:
            A single scope string when all files share one top-level directory,
            otherwise ``None``.
        """
        dirs: set[str] = set()
        for f in files:
            parts = f.split("/")
            if len(parts) > 1:
                dirs.add(parts[0] if parts[0] != "vetinari" else parts[1] if len(parts) > 2 else parts[0])
        if len(dirs) == 1:
            return dirs.pop()
        return None

    @staticmethod
    def _common_directory(files: list[str]) -> str:
        """Find the common directory prefix shared by all *files*.

        Args:
            files: List of file paths.

        Returns:
            The common directory path string, or an empty string when there is
            no shared prefix.
        """
        if not files:
            return ""
        parts = [f.split("/") for f in files]
        common: list[str] = []
        for segments in zip(*parts):
            if len(set(segments)) == 1:
                common.append(segments[0])
            else:
                break
        return "/".join(common)
