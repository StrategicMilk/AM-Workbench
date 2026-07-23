"""Typed lease records linking runs to workbench scheduler lanes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from vetinari.runtime.workbench_scheduler import Lane
from vetinari.utils.serialization import dataclass_to_dict


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


class LeaseStatus(str, Enum):
    """Lifecycle states for scheduler lease records."""

    REQUESTED = "requested"
    GRANTED = "granted"
    RELEASED = "released"
    DENIED = "denied"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class WorkbenchLease:
    """A typed link from a run to a workbench scheduler lease."""

    lease_id: str
    lane: Lane
    status: LeaseStatus
    lease_handle: str
    granted_at_utc: str
    released_at_utc: str
    requested_for_run_id: str
    vram_share: float

    def __post_init__(self) -> None:
        _require_non_empty(self.lease_id, "lease_id")
        _require_non_empty(self.lease_handle, "lease_handle")
        _require_non_empty(self.granted_at_utc, "granted_at_utc")
        _require_non_empty(self.requested_for_run_id, "requested_for_run_id")
        if self.status in {LeaseStatus.RELEASED, LeaseStatus.DENIED, LeaseStatus.EXPIRED}:
            _require_non_empty(self.released_at_utc, "released_at_utc")
        if not 0.0 <= self.vram_share <= 1.0:
            raise ValueError("vram_share must be between 0.0 and 1.0")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchLease(lease_id={self.lease_id!r}, lane={self.lane!r}, status={self.status!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the operator-console JSON contract for this lease."""
        return dataclass_to_dict(self)


__all__ = ["LeaseStatus", "WorkbenchLease"]
