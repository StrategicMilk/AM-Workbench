"""Reusable fail-closed guards for security-sensitive boundaries."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from vetinari.security.path_confinement import PathConfinementError, confine_path


class SecurityFailClosedError(ValueError):
    """Base class for fail-closed guard rejections."""


FailClosedError = SecurityFailClosedError


class SandboxUnavailableError(SecurityFailClosedError):
    """Raised when code execution would proceed without a required sandbox."""


class UntrustedInputError(SecurityFailClosedError):
    """Raised when untrusted text contains control or prompt-injection markers."""


class PathTraversalError(SecurityFailClosedError):
    """Raised when a caller attempts to escape a declared filesystem root."""


class SchemaOpenError(SecurityFailClosedError):
    """Raised when an input mapping is missing required shape constraints."""


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PROMPT_CONTROL_RE = re.compile(
    r"(?is)(?:^|\b)(?:system|developer|assistant|tool)\s*[:=]|"
    r"\b(?:ignore|disregard|override|forget)\b.{0,80}\b(?:instructions?|rules?|guardrails?|policy)\b"
)


def require_sandbox_or_raise(sandbox: object, *, label: str = "sandbox") -> object:
    """Return ``sandbox`` only when it is explicitly available.

    Falsey values, ``available=False``, ``enabled=False``, or ``disabled=True``
    are treated as unavailable. The guard deliberately raises instead of
    returning a status so callers cannot accidentally continue unsandboxed.

    Returns:
        The original sandbox object when it is available.

    Raises:
        SandboxUnavailableError: If the sandbox is absent or disabled.
    """
    if sandbox is None or sandbox is False:
        raise SandboxUnavailableError(f"{label} is required")
    if getattr(sandbox, "available", True) is False:
        raise SandboxUnavailableError(f"{label} is unavailable")
    if getattr(sandbox, "enabled", True) is False:
        raise SandboxUnavailableError(f"{label} is disabled")
    if getattr(sandbox, "disabled", False) is True:
        raise SandboxUnavailableError(f"{label} is disabled")
    return sandbox


def sanitize_untrusted_text(value: object, *, max_length: int = 20_000) -> str:
    """Normalize and validate text from untrusted sources.

    The function is intentionally conservative: it rejects non-text values,
    control characters, prompt-control markers, and oversized payloads. Safe
    text is returned stripped so callers can persist or compare it directly.

    Returns:
        The stripped trusted text.

    Raises:
        UntrustedInputError: If the value is not safe text.
    """
    if not isinstance(value, str):
        raise UntrustedInputError("untrusted text must be a string")
    text = value.strip()
    if not text:
        raise UntrustedInputError("untrusted text is empty")
    if len(text) > max_length:
        raise UntrustedInputError("untrusted text exceeds maximum length")
    if _CONTROL_CHARS_RE.search(text):
        raise UntrustedInputError("untrusted text contains control characters")
    if _PROMPT_CONTROL_RE.search(text):
        raise UntrustedInputError("untrusted text contains prompt-control markers")
    return text


def confine_to_root(root: str | Path, candidate: str | Path) -> Path:
    """Resolve ``candidate`` under ``root`` or raise ``PathTraversalError``.

    Args:
        root: Filesystem root that bounds the candidate.
        candidate: Path to resolve under ``root``.

    Returns:
        The resolved confined path.

    Raises:
        PathTraversalError: If the candidate escapes ``root``.
    """
    try:
        return confine_path(Path(root), candidate)
    except PathConfinementError as exc:
        raise PathTraversalError(str(exc)) from exc


def assert_closed_schema(
    payload: Mapping[str, Any],
    *,
    allowed_keys: Iterable[str],
    required_keys: Iterable[str] = (),
) -> Mapping[str, Any]:
    """Reject mappings with unknown or missing fields.

    Returning the original mapping lets existing callers avoid copies while
    still making schema drift fail closed at the boundary.

    Returns:
        The original mapping after validation.

    Raises:
        SchemaOpenError: If the payload shape is open or incomplete.
    """
    if not isinstance(payload, Mapping):
        raise SchemaOpenError("payload must be a mapping")
    allowed = frozenset(allowed_keys)
    required = frozenset(required_keys)
    unknown = set(payload) - allowed
    if unknown:
        raise SchemaOpenError(f"unknown fields: {', '.join(sorted(str(item) for item in unknown))}")
    missing = required - set(payload)
    if missing:
        raise SchemaOpenError(f"missing required fields: {', '.join(sorted(str(item) for item in missing))}")
    return payload


__all__ = [
    "FailClosedError",
    "PathTraversalError",
    "SandboxUnavailableError",
    "SchemaOpenError",
    "SecurityFailClosedError",
    "UntrustedInputError",
    "assert_closed_schema",
    "confine_to_root",
    "require_sandbox_or_raise",
    "sanitize_untrusted_text",
]
