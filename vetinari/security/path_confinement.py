"""Fail-closed path and permission confinement helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath

_DRIVE_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")


class PathConfinementError(ValueError):
    """Raised when a path or scoped permission escapes its boundary."""


def confine_path(root: Path, candidate: str | Path) -> Path:
    """Resolve candidate under root or raise when it escapes.

    Args:
        root: Root directory that bounds the candidate.
        candidate: Relative path requested by a caller.

    Returns:
        Resolved path inside root.

    Raises:
        PathConfinementError: If candidate is empty, absolute, or escaping.
    """
    raw = _require_text(candidate, "path")
    if _is_absolute_like(raw):
        raise PathConfinementError("absolute path rejected")
    candidate_path = Path(raw)
    if ".." in candidate_path.parts or ".." in PurePosixPath(raw.replace("\\", "/")).parts:
        raise PathConfinementError("path escapes confinement root")
    resolved_root = Path(root).resolve()
    resolved = (resolved_root / candidate_path).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise PathConfinementError("path escapes confinement root")
    return resolved


def validate_scoped_permission(permission: str, *, allowed_prefixes: tuple[str, ...]) -> str:
    """Validate extension permissions that include scoped filesystem reads.

    Returns:
        The unchanged permission string.

    Raises:
        PathConfinementError: If the permission is unsupported or escaping.
    """
    text = _require_text(permission, "permission")
    if not text.startswith(allowed_prefixes):
        raise PathConfinementError("permission prefix rejected")
    if ".." in text:
        raise PathConfinementError("permission traversal rejected")
    if text.startswith("fs_read:"):
        value = text.removeprefix("fs_read:")
        if not value or _is_absolute_like(value):
            raise PathConfinementError("permission absolute path rejected")
    return text


def validate_receipt_path(receipt_path: str, *, prefix: str = "receipts/") -> str:
    """Validate receipt paths stay under the governed receipt prefix.

    Returns:
        The unchanged receipt path.

    Raises:
        PathConfinementError: If the receipt path is not scoped to prefix.
    """
    text = _require_text(receipt_path, "receipt_path")
    if not text.startswith(prefix):
        raise PathConfinementError("receipt path prefix rejected")
    suffix = text.removeprefix(prefix)
    if not suffix or ".." in text or _is_absolute_like(suffix):
        raise PathConfinementError("receipt path escapes scope")
    return text


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, (str, Path)):
        raise PathConfinementError(f"{field} must be text")
    text = str(value).strip()
    if not text:
        raise PathConfinementError("empty path")
    return text


def _is_absolute_like(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return (
        os.path.isabs(value)
        or PurePosixPath(normalized).is_absolute()
        or bool(_DRIVE_ABSOLUTE_RE.match(value))
        or value.startswith("\\\\")
        or normalized.startswith("//")
    )
