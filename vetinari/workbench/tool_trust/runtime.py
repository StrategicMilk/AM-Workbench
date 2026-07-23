"""Fail-closed runtime assessment for pinned Workbench tool surfaces."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from vetinari.workbench.tool_trust.contracts import (
    ToolSurfaceApproval,
    ToolSurfaceCapabilityDiff,
    ToolSurfacePin,
    ToolSurfacePowerChange,
    ToolSurfaceTrustDecision,
    ToolTrustReason,
    ToolTrustStatus,
    WorkbenchToolTrustError,
)

logger = logging.getLogger(__name__)


def _utc_now_iso(now_utc: datetime | None = None) -> str:
    return (now_utc or datetime.now(timezone.utc)).isoformat()


def _coerce_pin(value: ToolSurfacePin | Mapping[str, Any]) -> ToolSurfacePin:
    if isinstance(value, ToolSurfacePin):
        return value
    return ToolSurfacePin.from_mapping(value)


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkbenchToolTrustError(f"captured_at_utc is unreadable: {value!r}") from exc
    if parsed.tzinfo is None:
        raise WorkbenchToolTrustError("captured_at_utc must include timezone")
    return parsed.astimezone(timezone.utc)


def _blocked(
    surface_id: str,
    reasons: tuple[ToolTrustReason, ...],
    *,
    now_utc: datetime | None,
    capability_diff: ToolSurfaceCapabilityDiff | None = None,
    approval: ToolSurfaceApproval | None = None,
    details: Mapping[str, str] | None = None,
) -> ToolSurfaceTrustDecision:
    return ToolSurfaceTrustDecision(
        surface_id=surface_id,
        status=ToolTrustStatus.BLOCKED,
        allowed=False,
        reasons=reasons,
        checked_at_utc=_utc_now_iso(now_utc),
        capability_diff=capability_diff,
        approval=approval,
        details=details or {},
    )


def _diff_matches(left: ToolSurfaceCapabilityDiff, right: ToolSurfaceCapabilityDiff) -> bool:
    return (
        left.surface_id == right.surface_id
        and dict(left.old_power) == dict(right.old_power)
        and dict(left.new_power) == dict(right.new_power)
        and left.reasons == right.reasons
        and left.permission_expansions == right.permission_expansions
    )


def build_tool_surface_pin(payload: Mapping[str, Any]) -> ToolSurfacePin:
    """Build and validate a tool-surface pin from a schema-shaped mapping."""
    return ToolSurfacePin.from_mapping(payload)


def build_capability_diff(
    old_pin: ToolSurfacePin | Mapping[str, Any],
    new_pin: ToolSurfacePin | Mapping[str, Any],
    *,
    now_utc: datetime | None = None,
) -> ToolSurfaceCapabilityDiff:
    """Return the power delta between a pinned surface and an observed surface.

    Args:
        old_pin: Old pin value consumed by build_capability_diff().
        new_pin: New pin value consumed by build_capability_diff().
        now_utc: Now utc value consumed by build_capability_diff().

    Returns:
        Newly constructed capability diff value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    old = _coerce_pin(old_pin)
    new = _coerce_pin(new_pin)
    if old.surface_id != new.surface_id:
        raise WorkbenchToolTrustError("cannot diff different surface_id values")

    old_power = old.power()
    new_power = new.power()
    changes: list[ToolSurfacePowerChange] = []
    permission_expansions: tuple[str, ...] = ()

    if old.command != new.command:
        changes.append(ToolSurfacePowerChange("command", old.command, new.command, ToolTrustReason.COMMAND_CHANGED))
    if old.host != new.host:
        changes.append(ToolSurfacePowerChange("host", old.host, new.host, ToolTrustReason.HOST_CHANGED))
    if old.transport != new.transport:
        changes.append(
            ToolSurfacePowerChange(
                "transport",
                old.transport.value,
                new.transport.value,
                ToolTrustReason.TRANSPORT_CHANGED,
            )
        )
    if old.version != new.version:
        changes.append(ToolSurfacePowerChange("version", old.version, new.version, ToolTrustReason.VERSION_CHANGED))
    old_permissions = set(old.permissions)
    new_permissions = set(new.permissions)
    added_permissions = tuple(sorted(new_permissions - old_permissions))
    if added_permissions:
        permission_expansions = added_permissions
        changes.append(
            ToolSurfacePowerChange(
                "permissions",
                tuple(sorted(old_permissions)),
                tuple(sorted(new_permissions)),
                ToolTrustReason.PERMISSIONS_EXPANDED,
            )
        )
    if old.trust_boundary != new.trust_boundary:
        changes.append(
            ToolSurfacePowerChange(
                "trust_boundary",
                old.trust_boundary,
                new.trust_boundary,
                ToolTrustReason.TRUST_BOUNDARY_CHANGED,
            )
        )

    return ToolSurfaceCapabilityDiff(
        surface_id=old.surface_id,
        old_power=old_power,
        new_power=new_power,
        changes=tuple(changes),
        permission_expansions=permission_expansions,
        generated_at_utc=_utc_now_iso(now_utc),
    )


def create_approval_record(
    diff: ToolSurfaceCapabilityDiff,
    *,
    approval_id: str,
    approved_by: str,
    evidence_refs: tuple[str, ...],
    policy_verdict_ref: str,
    approved_at_utc: str | None = None,
    now_utc: datetime | None = None,
) -> ToolSurfaceApproval:
    """Create an approval that records exactly the old and new tool power."""
    return ToolSurfaceApproval(
        approval_id=approval_id,
        surface_id=diff.surface_id,
        approved_by=approved_by,
        approved_at_utc=approved_at_utc or _utc_now_iso(now_utc),
        old_power=dict(diff.old_power),
        new_power=dict(diff.new_power),
        diff_reasons=diff.reasons,
        evidence_refs=evidence_refs,
        policy_verdict_ref=policy_verdict_ref,
    )


def assess_tool_surface_pin(
    pinned_surfaces: Mapping[str, ToolSurfacePin | Mapping[str, Any]],
    observed_surface: ToolSurfacePin | Mapping[str, Any],
    *,
    capability_diff: ToolSurfaceCapabilityDiff | None = None,
    approval: ToolSurfaceApproval | None = None,
    now_utc: datetime | None = None,
) -> ToolSurfaceTrustDecision:
    """Decide whether an observed tool surface can be trusted before use.

        Unknown surfaces, corrupt pins, stale pins, and unapproved capability drift
        return blocked decisions rather than silently inheriting new authority.

    Args:
        pinned_surfaces: Pinned surfaces value consumed by assess_tool_surface_pin().
        observed_surface: Observed surface value consumed by assess_tool_surface_pin().
        capability_diff: Capability diff value consumed by assess_tool_surface_pin().
        approval: Approval value consumed by assess_tool_surface_pin().
        now_utc: Now utc value consumed by assess_tool_surface_pin().

    Returns:
        ToolSurfaceTrustDecision value produced by assess_tool_surface_pin().
    """
    now = now_utc or datetime.now(timezone.utc)
    observed_or_decision = _observed_pin_or_blocked(observed_surface, now)
    if isinstance(observed_or_decision, ToolSurfaceTrustDecision):
        return observed_or_decision
    observed = observed_or_decision
    raw_pinned = pinned_surfaces.get(observed.surface_id)
    if raw_pinned is None:
        return _blocked(observed.surface_id, (ToolTrustReason.UNKNOWN_TOOL_SURFACE,), now_utc=now)

    pinned_or_decision = _pinned_pin_or_blocked(raw_pinned, observed.surface_id, now)
    if isinstance(pinned_or_decision, ToolSurfaceTrustDecision):
        return pinned_or_decision
    pinned, captured_at = pinned_or_decision
    if now.astimezone(timezone.utc) - captured_at > timedelta(hours=pinned.max_staleness_hours):
        return _blocked(observed.surface_id, (ToolTrustReason.STALE_PIN,), now_utc=now)

    calculated_diff = build_capability_diff(pinned, observed, now_utc=now)
    return _decision_for_capability_diff(
        observed,
        calculated_diff,
        capability_diff=capability_diff,
        approval=approval,
        now=now,
    )


def _observed_pin_or_blocked(
    observed_surface: ToolSurfacePin | Mapping[str, Any],
    now: datetime,
) -> ToolSurfacePin | ToolSurfaceTrustDecision:
    try:
        return _coerce_pin(observed_surface)
    except (ValueError, TypeError, WorkbenchToolTrustError) as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return _blocked(
            "unknown",
            (ToolTrustReason.CORRUPT_TOOL_SURFACE,),
            now_utc=now,
            details={"error": str(exc)},
        )


def _pinned_pin_or_blocked(
    raw_pinned: ToolSurfacePin | Mapping[str, Any],
    surface_id: str,
    now: datetime,
) -> tuple[ToolSurfacePin, datetime] | ToolSurfaceTrustDecision:
    try:
        pinned = _coerce_pin(raw_pinned)
        captured_at = _parse_utc(pinned.captured_at_utc)
    except (ValueError, TypeError, WorkbenchToolTrustError) as exc:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return _blocked(
            surface_id,
            (ToolTrustReason.CORRUPT_TOOL_SURFACE,),
            now_utc=now,
            details={"error": str(exc)},
        )
    return pinned, captured_at


def _decision_for_capability_diff(
    observed: ToolSurfacePin,
    calculated_diff: ToolSurfaceCapabilityDiff,
    *,
    capability_diff: ToolSurfaceCapabilityDiff | None,
    approval: ToolSurfaceApproval | None,
    now: datetime,
) -> ToolSurfaceTrustDecision:
    if not calculated_diff.requires_approval:
        return ToolSurfaceTrustDecision(
            surface_id=observed.surface_id,
            status=ToolTrustStatus.ALLOWED,
            allowed=True,
            reasons=(ToolTrustReason.ALLOWED,),
            checked_at_utc=_utc_now_iso(now),
            capability_diff=calculated_diff,
            approval=approval,
        )

    if capability_diff is None:
        return _blocked(
            observed.surface_id,
            (ToolTrustReason.MISSING_CAPABILITY_DIFF, *calculated_diff.reasons),
            now_utc=now,
            capability_diff=calculated_diff,
        )

    if not _diff_matches(capability_diff, calculated_diff):
        return _blocked(
            observed.surface_id,
            (ToolTrustReason.APPROVAL_MISMATCH,),
            now_utc=now,
            capability_diff=calculated_diff,
            approval=approval,
        )

    if approval is None:
        return _blocked(
            observed.surface_id,
            (ToolTrustReason.APPROVAL_REQUIRED, *calculated_diff.reasons),
            now_utc=now,
            capability_diff=calculated_diff,
        )

    if not approval.matches(calculated_diff):
        return _blocked(
            observed.surface_id,
            (ToolTrustReason.APPROVAL_MISMATCH,),
            now_utc=now,
            capability_diff=calculated_diff,
            approval=approval,
        )

    return ToolSurfaceTrustDecision(
        surface_id=observed.surface_id,
        status=ToolTrustStatus.ALLOWED,
        allowed=True,
        reasons=calculated_diff.reasons,
        checked_at_utc=_utc_now_iso(now),
        capability_diff=calculated_diff,
        approval=approval,
    )
