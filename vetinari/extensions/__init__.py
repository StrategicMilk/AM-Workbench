"""Extension protocol and registry package."""

from __future__ import annotations

from vetinari.extensions.protocol import ExtensionManifest, ExtensionProtocol
from vetinari.extensions.registry import (
    ExtensionRecord,
    ExtensionRegistry,
    get_extension_registry,
    reset_extension_registry,
)

__all__ = [
    "ExtensionManifest",
    "ExtensionProtocol",
    "ExtensionRecord",
    "ExtensionRegistry",
    "get_extension_registry",
    "reset_extension_registry",
]
