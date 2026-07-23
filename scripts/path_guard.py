"""Single source-of-truth path-containment and identifier-validation helpers consumed by affected scripts; eliminates per-site ad-hoc checks."""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT: Path = Path(__file__).resolve().parents[1]
_DEFAULT_NAME_PATTERN: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


class ContainmentError(ValueError):
    """Raised when a path resolves outside the configured root."""

    def __init__(self, requested: str, root: str, *, label: str = "path") -> None:
        self.requested = requested
        self.root = root
        self.label = label
        super().__init__(f"{label} {requested!r} escapes repo root {root!r}")


def contain(path: Path | str, root: Path | str = REPO_ROOT) -> Path:
    """Return the resolved path only when it is contained by root."""
    resolved = Path(path).resolve()
    root_resolved = Path(root).resolve()
    try:
        contained = resolved.is_relative_to(root_resolved)
    except AttributeError:
        contained = root_resolved == resolved or root_resolved in resolved.parents
    if not contained:
        raise ContainmentError(str(resolved), str(root_resolved))
    return resolved


def contain_or_reject(path: Path | str, root: Path | str = REPO_ROOT) -> tuple[bool, Path | None, str]:
    """Return a non-raising containment result for batch contexts."""
    try:
        resolved = contain(path, root)
    except ContainmentError as exc:
        return False, None, str(exc)
    return True, resolved, ""


def safe_name(
    s: str,
    *,
    allow: frozenset[str] | None = None,
    pattern: re.Pattern[str] | None = None,
) -> str:
    """Validate a slug, identifier, or allow-listed name."""
    if allow is not None:
        if s not in allow:
            raise ValueError(f"name {s!r} not in allow-list")
        return s
    if pattern is not None:
        if not pattern.fullmatch(s):
            raise ValueError(f"name {s!r} does not match {pattern.pattern!r}")
        return s
    if not _DEFAULT_NAME_PATTERN.fullmatch(s):
        raise ValueError(f"name {s!r} is not a safe identifier")
    return s


def assert_repo_member(path: Path | str, root: Path | str = REPO_ROOT, label: str = "path") -> Path:
    """Contain a path and include the source field label on failures."""
    try:
        return contain(path, root)
    except ContainmentError as exc:
        raise ContainmentError(exc.requested, exc.root, label=label) from exc


def safe_join(root: Path | str, *parts: str, label: str = "path") -> Path:
    """Join path parts and reject traversal outside root."""
    candidate = Path(root)
    for part in parts:
        candidate = candidate / part
    return assert_repo_member(candidate, root=root, label=label)
