"""Bounded web startup dependency checks."""

from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_OPTIONAL_WEB_DEPENDENCIES: tuple[str, ...] = (
    "litestar",
    "uvicorn",
    "msgspec",
)


@dataclass(frozen=True, slots=True)
class DependencyCheckResult:
    """Summary of optional web dependency availability."""

    ready: bool
    available: tuple[str, ...] = field(default_factory=tuple)
    unavailable: tuple[str, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    def __repr__(self) -> str:
        return (
            "DependencyCheckResult("
            f"ready={self.ready!r}, available={len(self.available)!r}, "
            f"unavailable={self.unavailable!r}, errors={len(self.errors)!r})"
        )


def check_dependencies(dependencies: tuple[str, ...] = _OPTIONAL_WEB_DEPENDENCIES) -> DependencyCheckResult:
    """Return optional web dependency readiness without propagating probe errors.

    Returns:
        Dependency availability summary with probe errors captured as data.
    """
    available: list[str] = []
    unavailable: list[str] = []
    errors: list[str] = []
    for name in dependencies:
        try:
            found = importlib.util.find_spec(name) is not None
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            logger.warning("Optional web dependency probe failed for %s: %s", name, exc)
            unavailable.append(name)
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if found:
            available.append(name)
        else:
            unavailable.append(name)
    return DependencyCheckResult(
        ready=not unavailable and not errors,
        available=tuple(available),
        unavailable=tuple(unavailable),
        errors=tuple(errors),
    )


__all__ = ["DependencyCheckResult", "check_dependencies"]
