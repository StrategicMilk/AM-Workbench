"""Deferred tool-context admission layered over Workbench tool-surface pins."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from vetinari.workbench.tool_trust.contracts import ToolSurfaceApproval, ToolSurfaceCapabilityDiff, ToolSurfacePin
from vetinari.workbench.tool_trust.runtime import assess_tool_surface_pin


class ToolContextState(str, Enum):
    """States a tool can occupy before a managed agent inherits it."""

    FILTERED = "filtered"
    DEFERRED = "deferred"
    APPROVAL_REQUIRED = "approval_required"
    CACHED_SCHEMA = "cached_schema"
    STALE = "stale"
    BLOCKED = "blocked"
    ALLOWED = "allowed"


@dataclass(frozen=True, slots=True)
class ToolContextRequest:
    """One tool-context admission request."""

    surface_id: str
    observed_surface: ToolSurfacePin | Mapping[str, Any]
    required_filters: tuple[str, ...] = ()
    matched_filters: tuple[str, ...] = ()
    defer_until_approval: bool = False
    cached_schema_ref: str = ""

    def __post_init__(self) -> None:
        _require_text(self.surface_id, "surface_id")
        object.__setattr__(self, "required_filters", _string_tuple(self.required_filters))
        object.__setattr__(self, "matched_filters", _string_tuple(self.matched_filters))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolContextRequest(surface_id={self.surface_id!r}, observed_surface={self.observed_surface!r}, required_filters={self.required_filters!r})"


@dataclass(frozen=True, slots=True)
class ToolContextDecision:
    """Fail-closed result for a deferred or filtered tool-context request."""

    surface_id: str
    state: ToolContextState
    allowed: bool
    reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...] = ()
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.surface_id, "surface_id")
        object.__setattr__(self, "state", ToolContextState(self.state))
        object.__setattr__(self, "reasons", _string_tuple(self.reasons))
        object.__setattr__(self, "evidence_refs", _string_tuple(self.evidence_refs))
        object.__setattr__(self, "details", {str(key): str(value) for key, value in self.details.items()})
        if self.allowed and self.state is not ToolContextState.ALLOWED:
            raise ValueError("allowed tool contexts must use allowed state")

    def to_dict(self) -> dict[str, object]:
        return {
            "surface_id": self.surface_id,
            "state": self.state.value,
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "evidence_refs": list(self.evidence_refs),
            "details": dict(sorted(self.details.items())),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ToolContextDecision(surface_id={self.surface_id!r}, state={self.state!r}, allowed={self.allowed!r})"


def evaluate_tool_context(
    pinned_surfaces: Mapping[str, ToolSurfacePin | Mapping[str, Any]],
    request: ToolContextRequest,
    *,
    capability_diff: ToolSurfaceCapabilityDiff | None = None,
    approval: ToolSurfaceApproval | None = None,
    now_utc: datetime | None = None,
) -> ToolContextDecision:
    """Evaluate filters, deferral, approval, and pin drift before tool inheritance.

    Args:
        pinned_surfaces: Pinned surfaces value consumed by evaluate_tool_context().
        request: Request object sent through the operation.
        capability_diff: Capability diff value consumed by evaluate_tool_context().
        approval: Approval value consumed by evaluate_tool_context().
        now_utc: Optional deterministic clock value for pin freshness checks.

    Returns:
        ToolContextDecision value produced by evaluate_tool_context().
    """
    missing_filters = tuple(sorted(set(request.required_filters) - set(request.matched_filters)))
    if missing_filters:
        return ToolContextDecision(
            request.surface_id,
            ToolContextState.FILTERED,
            False,
            ("filtered",),
            details={"missing_filters": ",".join(missing_filters)},
        )
    pin_decision = assess_tool_surface_pin(
        pinned_surfaces,
        request.observed_surface,
        capability_diff=capability_diff,
        approval=approval,
        now_utc=now_utc,
    )
    if not pin_decision.allowed:
        if any(reason.value == "stale_pin" for reason in pin_decision.reasons):
            state = ToolContextState.STALE
        elif any(reason.value == "approval_required" for reason in pin_decision.reasons):
            state = ToolContextState.APPROVAL_REQUIRED
        else:
            state = ToolContextState.BLOCKED
        return ToolContextDecision(
            request.surface_id,
            state,
            False,
            tuple(reason.value for reason in pin_decision.reasons),
            details=pin_decision.details,
        )
    if request.defer_until_approval and approval is None:
        return ToolContextDecision(request.surface_id, ToolContextState.DEFERRED, False, ("deferred",))
    evidence = ("tool-surface-pin",)
    if request.cached_schema_ref:
        evidence = (*evidence, request.cached_schema_ref)
    return ToolContextDecision(request.surface_id, ToolContextState.ALLOWED, True, ("allowed",), evidence)


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _string_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(str(value) for value in values if str(value).strip())


__all__ = [
    "ToolContextDecision",
    "ToolContextRequest",
    "ToolContextState",
    "evaluate_tool_context",
]
