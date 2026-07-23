"""Compatibility import path for orchestration variant configuration."""

from __future__ import annotations

from vetinari.orchestration.variant_system import (
    VARIANT_CONFIGS,
    VariantConfig,
    VariantLevel,
    VariantManager,
    get_variant_manager,
)

__all__ = [
    "VARIANT_CONFIGS",
    "VariantConfig",
    "VariantLevel",
    "VariantManager",
    "get_variant_manager",
]
