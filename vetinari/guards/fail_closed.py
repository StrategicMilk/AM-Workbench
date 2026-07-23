"""Fail-closed guard primitives for Vetinari security and quality gates.

Every gate that cannot complete its check MUST raise GateError, not return a
pass-equivalent value.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")


class GateError(RuntimeError):
    """Terminal gate failure that blocks instead of silently passing."""

    gate_name: str
    reason: str

    def __init__(self, gate_name: str, reason: str, cause: BaseException | None = None) -> None:
        self.gate_name = gate_name
        self.reason = reason
        super().__init__(f"[{gate_name}] {reason}")
        if cause is not None:
            self.__cause__ = cause

    def __repr__(self) -> str:
        return f"GateError(gate_name={self.gate_name!r}, reason={self.reason!r}, cause={self.__cause__!r})"


class require_subsystem:
    """Context manager that converts subsystem exceptions into GateError."""

    def __init__(self, gate_name: str, subsystem_name: str) -> None:
        self.gate_name = gate_name
        self.subsystem_name = subsystem_name

    def __enter__(self) -> require_subsystem:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> bool:
        if exc_type is None:
            return False
        if exc is not None and issubclass(exc_type, Exception):
            logger.error("[%s] %s subsystem unavailable: %s", self.gate_name, self.subsystem_name, exc)
            raise GateError(
                self.gate_name,
                f"{self.subsystem_name} subsystem unavailable: {exc}",
                exc,
            ) from exc
        return False


def closed_gate(gate_name: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Wrap a gate callable so exceptions and None returns become GateError.

    Returns:
        Decorator that preserves the wrapped callable signature.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                logger.error("[%s] gate callable raised", gate_name, exc_info=True)
                raise GateError(gate_name, str(exc), exc) from exc
            if result is None:
                raise GateError(gate_name, "gate returned None instead of a result")
            return result

        return wrapper

    return decorator


def strict_invoke(gate_name: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Invoke a gate callable and fail closed on exceptions or None.

    Args:
        gate_name: Name included in the raised GateError.
        fn: Callable to execute under fail-closed semantics.
        *args: Positional arguments forwarded to ``fn``.
        **kwargs: Keyword arguments forwarded to ``fn``.

    Returns:
        Non-None result returned by ``fn``.

    Raises:
        GateError: If ``fn`` raises or returns None.
    """
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        logger.error("[%s] strict_invoke target raised", gate_name, exc_info=True)
        raise GateError(gate_name, str(exc), exc) from exc
    if result is None:
        raise GateError(gate_name, "strict_invoke target returned None")
    return result


__all__ = ["GateError", "closed_gate", "require_subsystem", "strict_invoke"]
