"""Local MCP schema cache drift checks for Workbench tool contexts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MCPSchemaCacheStatus(str, Enum):
    """Cache trust states."""

    ALLOWED = "allowed"
    APPROVAL_REQUIRED = "approval_required"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class MCPSchemaCacheEntry:
    """Pinned MCP schema receipt keyed by source and schema hash."""

    surface_id: str
    source_id: str
    transport: str
    command: str
    version: str
    authority_owner: str
    schema_hash: str
    inspector_evidence_ref: str

    def __post_init__(self) -> None:
        for field_name in (
            "surface_id",
            "source_id",
            "transport",
            "command",
            "version",
            "authority_owner",
            "schema_hash",
            "inspector_evidence_ref",
        ):
            _require_text(getattr(self, field_name), field_name)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MCPSchemaCacheEntry(surface_id={self.surface_id!r}, source_id={self.source_id!r}, transport={self.transport!r})"


@dataclass(frozen=True, slots=True)
class MCPSchemaCacheDecision:
    """Result of comparing an observed MCP schema to the local cache."""

    status: MCPSchemaCacheStatus
    allowed: bool
    reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", MCPSchemaCacheStatus(self.status))
        if self.allowed and self.status is not MCPSchemaCacheStatus.ALLOWED:
            raise ValueError("allowed cache decisions must use allowed status")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MCPSchemaCacheDecision(status={self.status!r}, allowed={self.allowed!r}, reasons={self.reasons!r})"


def assess_mcp_schema_cache(
    cached: MCPSchemaCacheEntry | None,
    observed: MCPSchemaCacheEntry | None,
    *,
    approval_ref: str = "",
) -> MCPSchemaCacheDecision:
    """Allow cached schema reuse only when every authority-bearing field matches.

    Args:
        cached: Cached value consumed by assess_mcp_schema_cache().
        observed: Observed value consumed by assess_mcp_schema_cache().
        approval_ref: Approval ref value consumed by assess_mcp_schema_cache().

    Returns:
        MCPSchemaCacheDecision value produced by assess_mcp_schema_cache().
    """
    if cached is None or observed is None:
        return MCPSchemaCacheDecision(MCPSchemaCacheStatus.BLOCKED, False, ("schema-cache-missing",), ())
    drift = [
        f"{field_name}-drift"
        for field_name in ("source_id", "transport", "command", "version", "authority_owner", "schema_hash")
        if getattr(cached, field_name) != getattr(observed, field_name)
    ]
    if not drift:
        return MCPSchemaCacheDecision(
            MCPSchemaCacheStatus.ALLOWED,
            True,
            ("schema-cache-match",),
            (cached.inspector_evidence_ref,),
        )
    if approval_ref.strip() and set(drift) <= {"version-drift", "schema_hash-drift"}:
        return MCPSchemaCacheDecision(
            MCPSchemaCacheStatus.APPROVAL_REQUIRED,
            False,
            (*drift, "approval-required"),
            (cached.inspector_evidence_ref, approval_ref),
        )
    return MCPSchemaCacheDecision(MCPSchemaCacheStatus.BLOCKED, False, tuple(drift), (cached.inspector_evidence_ref,))


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


__all__ = [
    "MCPSchemaCacheDecision",
    "MCPSchemaCacheEntry",
    "MCPSchemaCacheStatus",
    "assess_mcp_schema_cache",
]
