"""Extension protocol contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ExtensionProtocolManifest:
    """Declarative manifest for an installable extension.

    Args:
        name: Stable extension name.
        version: Extension version string.
        entrypoint: Import path or executable entrypoint.
        capabilities: Capability labels advertised by the extension.
    """

    name: str
    version: str
    entrypoint: str
    capabilities: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Return a compact manifest identity for diagnostics."""
        return (
            f"ExtensionProtocolManifest(name={self.name!r}, version={self.version!r}, entrypoint={self.entrypoint!r})"
        )


@runtime_checkable
class ExtensionProtocol(Protocol):
    """Runtime protocol implemented by Vetinari extensions."""

    name: str
    version: str
    entrypoint: str
    capabilities: list[str]


ExtensionManifest = ExtensionProtocolManifest

__all__ = ["ExtensionManifest", "ExtensionProtocol", "ExtensionProtocolManifest"]
