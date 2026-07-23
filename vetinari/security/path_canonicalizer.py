"""Shared fail-closed path segment canonicalizers."""

from __future__ import annotations

import re
from typing import Final

_PROJECT_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PROJECT_ID_FORBIDDEN_MARKERS: Final[tuple[str, ...]] = ("/", "\\", "..", "\x00", " ", ";")


class PathCanonicalizationError(ValueError):
    """Raised when a caller supplied value cannot be used as a safe path segment."""


class ProjectIdRejected(PathCanonicalizationError):
    """Raised when a Workbench project id is not safe for path-scoped storage."""

    def __init__(self, value: object) -> None:
        super().__init__(
            f"rejected project_id {value!r}; use 1-64 ASCII letters, digits, '_' or '-' with no path markers"
        )
        self.value = value


def canonicalize_project_id(value: str | None) -> str:
    """Return a Workbench project id that is safe to use as one path segment.

    Returns:
        str value produced by canonicalize_project_id().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(value, str):
        raise ProjectIdRejected(value)
    if not value or len(value) > 64:
        raise ProjectIdRejected(value)
    if any(marker in value for marker in _PROJECT_ID_FORBIDDEN_MARKERS):
        raise ProjectIdRejected(value)
    if _PROJECT_ID_RE.fullmatch(value) is None:
        raise ProjectIdRejected(value)
    return value


__all__ = [
    "PathCanonicalizationError",
    "ProjectIdRejected",
    "canonicalize_project_id",
]
