"""Processing-depth variant configuration for orchestration and web callers."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum


class VariantLevel(Enum):
    """Processing depth levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class VariantConfig:
    """Configuration for a variant level."""

    level: VariantLevel
    max_context_tokens: int
    max_planning_depth: int
    enable_verification: bool
    enable_self_improvement: bool
    description: str

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"VariantConfig(level={self.level!r}, max_context_tokens={self.max_context_tokens!r})"


VARIANT_CONFIGS: dict[VariantLevel, VariantConfig] = {
    VariantLevel.LOW: VariantConfig(
        level=VariantLevel.LOW,
        max_context_tokens=4096,
        max_planning_depth=2,
        enable_verification=False,
        enable_self_improvement=False,
        description="Fast mode -- minimal context, quick responses",
    ),
    VariantLevel.MEDIUM: VariantConfig(
        level=VariantLevel.MEDIUM,
        max_context_tokens=16384,
        max_planning_depth=5,
        enable_verification=True,
        enable_self_improvement=True,
        description="Balanced -- good context, verification enabled",
    ),
    VariantLevel.HIGH: VariantConfig(
        level=VariantLevel.HIGH,
        max_context_tokens=32768,
        max_planning_depth=10,
        enable_verification=True,
        enable_self_improvement=True,
        description="Deep analysis -- full context, thorough verification",
    ),
}


class VariantManager:
    """Manages variant level selection and configuration."""

    def __init__(self, default_level: str = "medium"):
        self._current = VariantLevel(default_level)

    def get_config(self) -> VariantConfig:
        """Return the configuration for the current variant level."""
        return VARIANT_CONFIGS[self._current]

    def set_level(self, level: str) -> VariantConfig:
        """Switch to a different variant level and return its config.

        Returns:
            Value produced for the caller.
        """
        self._current = VariantLevel(level)
        return self.get_config()

    @property
    def current_level(self) -> str:
        """Return the current level as a plain string."""
        return self._current.value

    def get_all_levels(self) -> list[dict[str, str]]:
        """Return metadata for every available level."""
        return [{"level": value.level.value, "description": value.description} for value in VARIANT_CONFIGS.values()]


_variant_manager: VariantManager | None = None
_variant_manager_lock = threading.Lock()


def get_variant_manager() -> VariantManager:
    """Return the process-wide VariantManager singleton, creating it on first call.

    Returns:
        Value produced for the caller.
    """
    global _variant_manager
    if _variant_manager is None:
        with _variant_manager_lock:
            if _variant_manager is None:
                _variant_manager = VariantManager(default_level="medium")
    return _variant_manager


def set_variant_level(level: str) -> VariantConfig:
    """Set the process-wide variant level and return its config.

    Delegates to the process-wide VariantManager singleton so callers do not
    need to import and dereference the singleton directly.

    Args:
        level: One of 'low', 'medium', or 'high'.

    Returns:
        The VariantConfig for the new level.
    """
    return get_variant_manager().set_level(level)
