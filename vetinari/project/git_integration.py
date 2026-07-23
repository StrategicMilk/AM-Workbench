"""Git branch strategy for autonomous code changes.

This module wraps :class:`vetinari.tools.git_tool.GitOperations` with
higher-level helpers that follow Vetinari's branch naming convention and
generate human-readable pull-request descriptions from scan findings.

Pipeline role: Step 3 of 4 — branch management. After the sandbox confirms
a proposed change is safe, this module creates the feature branch, stages
the files, and produces a PR description ready to open on GitHub/GitLab.

Naming convention: all autonomous fix branches are prefixed
``vetinari/fix/`` so they are easy to identify and clean up later.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from vetinari.tools.git_tool import CommitInfo, ConflictInfo, GitOperations

logger = logging.getLogger(__name__)


# Branch prefix that marks all Vetinari-created fix branches.
_BRANCH_PREFIX: str = "vetinari/fix"

# Maximum characters in the slug portion of a branch name.
_MAX_SLUG_LEN: int = 50


# -- Data structures ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BranchResult:
    """Outcome of a branch creation attempt.

    Attributes:
        branch_name: Full name of the branch that was (or would have been)
            created.
        created: True when the branch was newly created successfully.
        error: Error message when creation failed, None on success.
    """

    branch_name: str
    created: bool
    error: str | None = None

    def __repr__(self) -> str:
        return f"BranchResult(branch={self.branch_name!r}, created={self.created!r})"


@dataclass
class PRResult:
    """Content for a pull request generated from scan findings and changes.

    Attributes:
        title: Short (≤70 char) PR title suitable for GitHub/GitLab.
        body: Full markdown body describing what was changed and why.
        branch: The source branch the PR would be opened from.
        files_changed: Relative paths of all files included in the change.
    """

    title: str
    body: str
    branch: str
    files_changed: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"PRResult(title={self.title!r}, files={len(self.files_changed)})"


# -- Public API ---------------------------------------------------------------


def create_fix_branch(project_path: Path, description: str) -> BranchResult:
    """Create a ``vetinari/fix/<slug>`` branch in the target repository.

    The branch name is derived from *description* by lower-casing it,
    replacing whitespace and special characters with hyphens, and
    truncating to :data:`_MAX_SLUG_LEN` characters.

    D5 fix: validates the resulting branch name before calling git so that
    invalid ref names (empty slug, trailing ``/``, ``..`` sequences) are
    caught locally and returned as ``BranchResult(created=False)`` rather
    than producing a misleading git error.

    Args:
        project_path: Absolute path to the target project's git root.
        description: Short human-readable description of the fix, e.g.
            ``"remove unused imports"``. Used to build the branch slug.

    Returns:
        A :class:`BranchResult` describing whether the branch was created.
    """
    slug = _slugify(description)
    branch_name = f"{_BRANCH_PREFIX}/{slug}"

    invalid_reason = _check_branch_name(branch_name)
    if invalid_reason:
        logger.warning(
            "Refusing to create branch with invalid ref name %r — %s",
            branch_name,
            invalid_reason,
        )
        return BranchResult(
            branch_name=branch_name,
            created=False,
            error=f"invalid git ref name: {invalid_reason}",
        )

    git = GitOperations(project_path)
    result = git.create_branch(branch_name)

    if result.success:
        logger.info("Created fix branch %s in %s", branch_name, project_path)
        return BranchResult(branch_name=branch_name, created=True)

    logger.warning(
        "Could not create branch %s in %s — git reported: %s",
        branch_name,
        project_path,
        result.stderr,
    )
    return BranchResult(
        branch_name=branch_name,
        created=False,
        error=result.stderr or "git checkout -b failed with no output",
    )


def create_pr_description(findings: list, changes: dict[str, str]) -> PRResult:
    """Generate a pull-request title and markdown body from scan findings.

    The PR body lists each finding that motivated the change and all files
    that were modified so reviewers can quickly understand what and why.

    Args:
        findings: List of :class:`vetinari.project.scanner.ScanFinding`
            objects (or any objects with ``category``, ``severity``, and
            ``message`` attributes) that this fix addresses.
        changes: Mapping of relative file path to new content. The keys are
            used to populate the "Files changed" section.

    Returns:
        A :class:`PRResult` with a complete title and markdown body.
    """
    files_changed = sorted(changes.keys())
    category_counts: dict[str, int] = {}
    for finding in findings:
        cat = getattr(finding, "category", "issue")
        category_counts[cat] = category_counts.get(cat, 0) + 1

    # Build a concise title from the dominant category.
    if category_counts:
        dominant = max(category_counts, key=lambda k: category_counts[k])
        title = f"fix: resolve {category_counts[dominant]} {dominant} issue(s)"
    else:
        title = f"fix: update {len(files_changed)} file(s)"
    title = title[:70]

    # Build markdown body.
    lines: list[str] = [
        "## Summary",
        "",
        "Automated fix generated by Vetinari.",
        "",
    ]

    if findings:
        lines += ["## Issues addressed", ""]
        for finding in findings[:20]:  # Cap at 20 to keep PRs readable
            sev = getattr(finding, "severity", "")
            msg = getattr(finding, "message", str(finding))
            cat = getattr(finding, "category", "")
            lines.append(f"- **[{sev}/{cat}]** {msg}")
        if len(findings) > 20:
            lines.append(f"- … and {len(findings) - 20} more")
        lines.append("")

    if files_changed:
        lines += ["## Files changed", ""]
        for fp in files_changed:
            lines.append(f"- `{fp}`")
        lines.append("")

    lines += [
        "## Review checklist",
        "",
        "- [ ] Changes are safe and do not alter public API",
        "- [ ] Tests pass in CI",
        "- [ ] No regressions in related areas",
    ]

    return PRResult(
        title=title,
        body="\n".join(lines),
        branch="",  # Caller sets this after create_fix_branch
        files_changed=files_changed,
    )


def has_uncommitted_changes(project_path: Path) -> bool:
    """Check whether the working tree has staged or unstaged modifications.

    D5 fix: git-status failures are now raised as ``RuntimeError`` rather than
    being collapsed into a ``True`` (dirty-tree) return value.  Callers that
    previously received a spurious ``True`` on a broken repo will now see an
    explicit error, allowing them to distinguish a genuine dirty tree from a
    git invocation failure.

    Args:
        project_path: Absolute path to the target project's git root.

    Returns:
        True when the working tree is dirty (has uncommitted changes).

    Raises:
        RuntimeError: If git status cannot be determined (e.g. not a git repo,
            git not on PATH, or unexpected exit code).
    """
    git = GitOperations(project_path)
    result = git.status()
    if not result.success:
        msg = f"Could not determine git status for {project_path} — git reported: {result.stderr!r}"
        logger.error(msg)
        raise RuntimeError(msg)
    return bool(result.stdout.strip())


def commit_changes(project_path: Path, message: str, files: list[str]) -> bool:
    """Stage specific files and create a commit in the target repository.

    Args:
        project_path: Absolute path to the target project's git root.
        message: Conventional commit message string.
        files: Relative file paths to stage before committing. Only these
            files will be included in the commit.

    Returns:
        True when the commit was created successfully, False otherwise.
    """
    git = GitOperations(project_path)

    add_result = git.add(files=files)
    if not add_result.success:
        logger.warning(
            "Could not stage files in %s — commit aborted: %s",
            project_path,
            add_result.stderr,
        )
        return False

    commit_result = git.commit(message)
    if not commit_result.success:
        logger.warning(
            "Commit failed in %s — staged changes remain: %s",
            project_path,
            commit_result.stderr,
        )
        return False

    logger.info("Committed %d file(s) in %s", len(files), project_path)
    return True


def generate_commit_message_for_path(repo_path: Path) -> CommitInfo:
    """Generate a commit message by inspecting the staged diff of a repository.

    Creates a short-lived :class:`vetinari.tools.git_tool.GitOperations`
    instance, calls :meth:`generate_commit_message`, and returns the result.

    Args:
        repo_path: Absolute path to the target repository root.

    Returns:
        A :class:`CommitInfo` instance with the generated commit message for
        the currently staged changes.
    """
    git = GitOperations(repo_path)
    commit_info = git.generate_commit_message()
    logger.debug(
        "Generated commit message for %s: %s",
        repo_path,
        commit_info.message[:80] if hasattr(commit_info, "message") else str(commit_info)[:80],
    )
    return commit_info


def detect_merge_conflicts(repo_path: Path) -> list[ConflictInfo]:
    """Detect files with merge conflicts in the target repository.

    Creates a short-lived :class:`vetinari.tools.git_tool.GitOperations`
    instance and calls :meth:`detect_conflicts` to identify conflicted files.

    Args:
        repo_path: Absolute path to the target repository root.

    Returns:
        List of file paths that contain merge conflict markers.
        Empty list when the working tree is clean.
    """
    git = GitOperations(repo_path)
    conflicts = git.detect_conflicts()
    if conflicts:
        logger.warning(
            "Detected %d file(s) with merge conflicts in %s",
            len(conflicts),
            repo_path,
        )
    return conflicts


# -- Private helpers ----------------------------------------------------------


def _slugify(text: str) -> str:
    """Convert *text* to a URL-safe branch slug.

    Lowercases, replaces non-alphanumeric characters with hyphens, collapses
    runs of hyphens, strips leading/trailing hyphens, and truncates.

    Args:
        text: Arbitrary human-readable description string.

    Returns:
        A slug string safe for use in a git branch name, possibly empty when
        *text* contains no alphanumeric characters.
    """
    slug = text.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:_MAX_SLUG_LEN]


def _check_branch_name(name: str) -> str | None:
    """Validate *name* as a git ref name and return a reason string if invalid.

    Checks the subset of git ref-name rules that ``_slugify`` cannot guarantee:
    empty trailing component (description was all punctuation → empty slug),
    leading or trailing ``/``, consecutive ``/`` separators, and ``..``
    sequences anywhere in the name.

    Args:
        name: The full branch name to validate, e.g. ``"vetinari/fix/my-bug"``.

    Returns:
        A human-readable reason string when *name* is invalid, or ``None``
        when the name is acceptable.
    """
    if not name or name.strip("/") == "":
        return "branch name is empty or all slashes"
    if name.startswith("/") or name.endswith("/"):
        return "branch name must not start or end with '/'"
    if "//" in name:
        return "branch name must not contain consecutive slashes"
    if ".." in name:
        return "branch name must not contain '..'"
    # A component that is empty after stripping prefix — slug was empty.
    parts = name.split("/")
    if any(p == "" for p in parts):
        return "branch name contains an empty component"
    return None
