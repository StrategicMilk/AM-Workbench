"""MCP protocol version negotiation constants and helpers."""

from __future__ import annotations

from collections.abc import Iterable

# Keep newest first. These are the protocol revisions this runtime has explicit
# compatibility tests for; unknown future revisions must negotiate instead of
# silently succeeding.
SUPPORTED_PROTOCOL_VERSIONS: tuple[str, ...] = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
DEFAULT_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]


def negotiate_protocol_version(
    requested: str,
    supported: Iterable[str] = SUPPORTED_PROTOCOL_VERSIONS,
) -> str:
    """Return the negotiated MCP protocol version or the server's latest fallback.

    Args:
        requested: Request object sent through the operation.
        supported: Supported value consumed by negotiate_protocol_version().

    Returns:
        Value produced for the caller.
    """
    supported_versions = tuple(supported)
    if requested in supported_versions:
        return requested
    return supported_versions[0]


def is_supported_protocol_version(version: str) -> bool:
    """Return whether ``version`` is explicitly supported by this runtime."""
    return version in SUPPORTED_PROTOCOL_VERSIONS
