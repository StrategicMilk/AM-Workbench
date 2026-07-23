"""Blocked-paths enforcement helper for the Vetinari sandbox.

Implements the fail-closed path-blocking contract introduced in SESSION-03
SHARD-02 to close the Rule 2 governance gap: no security check may ever
default to *allow* on error. Every unexpected condition raises
``SandboxPolicyViolation`` so the caller is never silently permitted into a
restricted location.

Pipeline role: security gate, invoked before any write operation that may
touch a filesystem path. This is not part of the agent inference pipeline;
it is a pre-condition enforcement layer.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

import yaml

from vetinari.exceptions import SandboxPolicyViolation

logger = logging.getLogger(__name__)


# -- Module-level constants --------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
# Canonical policy location matches every other sandbox consumer
# (vetinari/sandbox_policy.py, vetinari/sandbox_manager.py, vetinari/project/sandbox.py).
# A duplicate at vetinari/config/runtime/sandbox_policy.yaml was removed
# 2026-04-25 to enforce style.md "One owner per concept".
_DEFAULT_POLICY_PATH = _PROJECT_ROOT / "config" / "sandbox_policy.yaml"

__all__ = [
    "RustSandboxAuthorityBridge",
    "RustSandboxDecision",
    "enforce_blocked_paths",
    "reset_blocked_paths_cache",
]

# -- Module-level cache and lock ---------------------------------------------
# Maps policy_path (as string) -> (exact_paths tuple, glob_patterns tuple).
# Protected by _CACHE_LOCK with double-checked locking so the file is parsed
# at most once per unique policy path per process lifetime.
_BLOCKED_PATHS_CACHE: dict[str, tuple[tuple[Path, ...], tuple[str, ...]]] = {}
_CACHE_LOCK = threading.Lock()


# -- Private helpers ---------------------------------------------------------


def _expand_path(raw: str) -> Path:
    """Expand environment variables and user home, then resolve to an absolute path.

    Args:
        raw: A raw path string that may contain ``~``, ``%VAR%``, or ``$VAR``
            tokens.

    Returns:
        A resolved, absolute ``Path`` object (``strict=False`` — the path need
        not exist on disk).
    """
    expanded = os.path.expanduser(os.path.expandvars(raw))
    return Path(expanded).resolve(strict=False)


def _load_blocked_paths(policy_path: Path) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """Parse blocked-path entries from the sandbox policy YAML.

    Reads ``data["rules"]["blocked_paths"]`` and splits entries into two
    groups: glob patterns (containing ``*``, ``?``, or ``[``) and exact
    paths (which are expanded and resolved).

    Args:
        policy_path: Absolute path to the sandbox policy YAML file.

    Returns:
        A 2-tuple of ``(exact_paths, glob_patterns)`` where ``exact_paths``
        is a tuple of resolved ``Path`` objects and ``glob_patterns`` is a
        tuple of raw glob strings.

    Raises:
        SandboxPolicyViolation: If the file does not exist, cannot be parsed
            as YAML, or the ``blocked_paths`` key is not a list.
    """
    if not policy_path.exists():
        raise SandboxPolicyViolation(
            f"Sandbox policy file not found: {policy_path} — cannot enforce blocked-path rules without a policy"
        )

    try:
        data = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SandboxPolicyViolation(
            f"Sandbox policy YAML at {policy_path} could not be parsed — blocked-path enforcement disabled: {exc}"
        ) from exc

    try:
        raw_entries = data["rules"]["blocked_paths"]
    except (KeyError, TypeError) as exc:
        raise SandboxPolicyViolation(
            f"Sandbox policy at {policy_path} is missing rules.blocked_paths — cannot continue: {exc}"
        ) from exc

    if not isinstance(raw_entries, list):
        raise SandboxPolicyViolation(
            f"rules.blocked_paths in {policy_path} must be a list, got {type(raw_entries).__name__}"
        )

    glob_patterns: list[str] = []
    exact_paths: list[Path] = []

    for entry in raw_entries:
        entry_str = str(entry)
        if any(ch in entry_str for ch in ("*", "?", "[")):
            glob_patterns.append(entry_str)
        else:
            exact_paths.append(_expand_path(entry_str))

    return tuple(exact_paths), tuple(glob_patterns)


def _get_blocked_entries(policy_path: Path) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    """Return cached blocked-path entries, parsing the policy file only once.

    Uses double-checked locking so the YAML file is read at most once per
    unique policy path per process lifetime.

    Args:
        policy_path: Absolute path to the sandbox policy YAML file.

    Returns:
        A 2-tuple of ``(exact_paths, glob_patterns)`` as returned by
        ``_load_blocked_paths``.

    Raises:
        SandboxPolicyViolation: Propagated from ``_load_blocked_paths`` if
            the policy file is absent or malformed.
    """
    key = str(policy_path)
    if key not in _BLOCKED_PATHS_CACHE:
        with _CACHE_LOCK:
            if key not in _BLOCKED_PATHS_CACHE:
                _BLOCKED_PATHS_CACHE[key] = _load_blocked_paths(policy_path)
    return _BLOCKED_PATHS_CACHE[key]


def _path_is_under(target: Path, candidate: Path) -> bool:
    """Return True if *target* equals *candidate* or is a descendant of it.

    Uses ``Path.is_relative_to`` (Python 3.9+) which is available across the
    project's minimum Python 3.10 baseline, avoiding an exception-as-control-
    flow pattern.

    Args:
        target: The resolved path being tested.
        candidate: The resolved blocked root to test against.

    Returns:
        ``True`` when ``target`` is ``candidate`` or inside it; ``False``
        otherwise.
    """
    return target.is_relative_to(candidate)


@dataclass(frozen=True, slots=True)
class RustSandboxDecision:
    """Python-visible decision object mirroring the Rust sandbox authority contract."""

    allowed: bool
    reason: str
    resolved_path: Path | None = None


class RustSandboxAuthorityBridge:
    """Fail-closed Python bridge for the Rust sandbox authority contract.

    The Rust kernel owns the branch contract and tests. Python callers use this
    bridge to preserve the same denial semantics at runtime until the packaged
    kernel is loaded directly by the Workbench process.
    """

    _KNOWN_PERMISSIONS = {"read", "write", "delete", "list", "info", "exists", "mkdir", "move"}

    def __init__(self, root: str | Path, allowed_permissions: set[str] | None = None) -> None:
        self.root = Path(root).resolve(strict=False)
        self.allowed_permissions = (
            set(self._KNOWN_PERMISSIONS) if allowed_permissions is None else set(allowed_permissions)
        )

    def check_permission(self, permission: str, path: str | Path) -> RustSandboxDecision:
        """Check whether a sandbox permission is allowed for a path.

        Args:
            permission: File-system operation requested by the caller.
            path: Candidate path, absolute or relative to the sandbox root.

        Returns:
            Decision with an allow flag, reason, and resolved path when allowed.
        """
        if permission not in self._KNOWN_PERMISSIONS:
            return RustSandboxDecision(False, f"unknown_permission:{permission}")
        if permission not in self.allowed_permissions:
            return RustSandboxDecision(False, f"permission_denied:{permission}")

        raw_path = Path(path)
        if ".." in raw_path.parts:
            return RustSandboxDecision(False, f"traversal_denied:{path}")

        candidate = raw_path if raw_path.is_absolute() else self.root / raw_path
        try:
            resolved = candidate.resolve(strict=False)
        except OSError as exc:
            logger.warning("Rust sandbox bridge could not resolve path %s", candidate, exc_info=True)
            return RustSandboxDecision(False, f"unreadable_path:{type(exc).__name__}:{exc}")

        if not resolved.is_relative_to(self.root) and candidate.exists():
            return RustSandboxDecision(False, f"symlink_escape_denied:{resolved}")
        if not resolved.is_relative_to(self.root):
            return RustSandboxDecision(False, f"traversal_denied:{resolved}")

        try:
            if candidate.exists() and candidate.is_symlink():
                target = candidate.resolve(strict=True)
                if not target.is_relative_to(self.root):
                    return RustSandboxDecision(False, f"symlink_escape_denied:{target}")
        except OSError as exc:
            logger.warning("Rust sandbox bridge could not resolve symlink %s", candidate, exc_info=True)
            return RustSandboxDecision(False, f"symlink_unreadable:{type(exc).__name__}:{exc}")

        return RustSandboxDecision(True, "allowed", resolved)

    def require_path(self, permission: str, path: str | Path) -> Path:
        """Resolve a sandbox path or raise on denied access.

        Args:
            permission: File-system operation requested by the caller.
            path: Candidate path, absolute or relative to the sandbox root.

        Returns:
            Resolved path inside the sandbox root.

        Raises:
            PermissionError: If the Rust sandbox decision denies access.
        """
        decision = self.check_permission(permission, path)
        if not decision.allowed or decision.resolved_path is None:
            raise PermissionError(decision.reason)
        return decision.resolved_path


# -- Public API --------------------------------------------------------------


def enforce_blocked_paths(target: Path, policy_path: Path | None = None) -> None:
    """Raise ``SandboxPolicyViolation`` if *target* is inside a blocked path.

    Resolves *target* with ``strict=False`` (so symlinks that point into
    blocked directories are caught via their resolved destination) then checks
    against all entries in the sandbox policy. Any unexpected error during the
    check is treated as a violation — the function NEVER defaults to allow.

    Args:
        target: The filesystem path about to be written to.
        policy_path: Override for the sandbox policy YAML location. Defaults
            to ``_DEFAULT_POLICY_PATH`` when ``None``.

    Raises:
        SandboxPolicyViolation: When the resolved target matches any blocked
            exact path, is a descendant of any blocked path, or matches any
            blocked glob pattern. Also raised on any parse or resolution error
            — fail-closed per governance-rules.md Rule 2.
    """
    effective_policy = policy_path if policy_path is not None else _DEFAULT_POLICY_PATH

    try:
        resolved = target.resolve(strict=False)
        exact_paths, glob_patterns = _get_blocked_entries(effective_policy)

        # Check exact paths and prefix matches
        for blocked in exact_paths:
            if _path_is_under(resolved, blocked):
                logger.warning(
                    "Sandbox denied write to %s — blocked by policy rule %s",
                    resolved,
                    blocked,
                )
                raise SandboxPolicyViolation(f"Write to {resolved} is blocked by sandbox policy rule: {blocked}")

        # Check glob patterns against both the full path string and the basename
        resolved_str = str(resolved)
        basename = resolved.name
        for pattern in glob_patterns:
            if fnmatch.fnmatch(resolved_str, pattern) or fnmatch.fnmatch(basename, pattern):
                logger.warning(
                    "Sandbox denied write to %s — blocked by policy glob pattern %s",
                    resolved,
                    pattern,
                )
                raise SandboxPolicyViolation(f"Write to {resolved} is blocked by sandbox policy glob: {pattern}")

    except SandboxPolicyViolation:
        # Re-raise policy violations directly — do not wrap in another exception.
        raise
    except Exception as exc:
        # Fail closed: ANY unexpected error during the check is a denial.
        # Lines 181-186 implement the Rule 2 fail-closed contract from
        # the repository governance contract — no exception may silently permit
        # a write that the policy was meant to block.
        logger.warning(
            "Sandbox denied write to %s — error during policy check: %s",
            target,
            exc,
        )
        raise SandboxPolicyViolation(
            f"Sandbox policy check for {target} failed with an unexpected error "
            f"({type(exc).__name__}: {exc}) — write denied (fail-closed)"
        ) from exc


def reset_blocked_paths_cache() -> None:
    """Clear the module-level blocked-paths cache.

    Intended for use in tests that need to swap out policy files between
    calls without restarting the interpreter.
    """
    with _CACHE_LOCK:
        _BLOCKED_PATHS_CACHE.clear()
