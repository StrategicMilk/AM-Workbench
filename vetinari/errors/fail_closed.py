"""Shared fail-closed error helpers for unavailable or untrusted state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

T = TypeVar("T")


class FailClosedError(RuntimeError):
    """Raised when unknown state must block instead of becoming a default."""

    def __init__(self, signal: str, reason: str, *, recovery: str = "") -> None:
        self.signal = signal
        self.reason = reason
        self.recovery = recovery
        message = f"{signal}: {reason}"
        if recovery:
            message = f"{message}; recovery={recovery}"
        super().__init__(message)


def require_present(value: T | None, signal: str, *, recovery: str = "") -> T:
    """Return ``value`` or raise when a required signal is unavailable.

    Args:
        value: Required value that may be absent.
        signal: Stable signal name used in the fail-closed error.
        recovery: Optional operator-facing recovery guidance.

    Returns:
        The original value, narrowed to a present value.

    Raises:
        FailClosedError: If ``value`` is ``None``.
    """
    if value is None:
        raise FailClosedError(signal, "required value is missing", recovery=recovery)
    return value


def require_mapping(value: Any, signal: str, *, recovery: str = "") -> Mapping[str, Any]:
    """Return a mapping or raise when shape is untrusted.

    Args:
        value: Candidate mapping loaded from an external or dynamic source.
        signal: Stable signal name used in the fail-closed error.
        recovery: Optional operator-facing recovery guidance.

    Returns:
        ``value`` narrowed to a string-keyed mapping.

    Raises:
        FailClosedError: If ``value`` is not a mapping.
    """
    if not isinstance(value, Mapping):
        raise FailClosedError(signal, f"expected mapping, got {type(value).__name__}", recovery=recovery)
    return value


__all__ = ["FailClosedError", "require_mapping", "require_present"]
