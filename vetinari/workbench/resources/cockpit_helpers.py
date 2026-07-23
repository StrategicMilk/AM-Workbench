"""Private helper functions for Workbench resource cockpit models."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.workbench.resources.governor import MachineProfile

RESOURCE_COCKPIT_PUBLIC_EXPORTS = [
    "EXPECTED_WAIT_BUCKETS",
    "SAFE_ACTION_IDS",
    "LeaseSummary",
    "PolicyProposal",
    "QueuedJobSummary",
    "ResourceCockpit",
    "ResourceCockpitSnapshot",
    "SafeActionRow",
    "build_policy_proposal",
    "safe_actions_for_lease",
    "scheduler_machine_profile_provider",
]


def scheduler_machine_profile_provider(
    base_provider: Callable[[], MachineProfile | None],
    scheduler: Any,
) -> Callable[[], MachineProfile | None]:
    """Support scheduler machine profile provider behavior for Vetinari callers.

    Args:
        base_provider: Provider name or adapter selected for the operation.
        scheduler: Scheduler value consumed by scheduler_machine_profile_provider().

    Returns:
        Value produced for the caller.
    """

    def _provider() -> MachineProfile | None:
        profile = base_provider()
        if profile is None:
            return None
        snapshot = scheduler.queue_depth_snapshot()
        values = asdict(profile)
        values["queue_depth"] = int(snapshot["queue_depth"])
        values["queue_capacity"] = int(snapshot["queue_capacity"])
        values["evidence_ids"] = tuple(dict.fromkeys((*profile.evidence_ids, "scheduler:queue-depth-snapshot")))
        return MachineProfile(**values)

    return _provider


def _enum_value(value: Any) -> str:
    return value.value if isinstance(value, Enum) else str(value)


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


def _require_text_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
    for item in value:
        _require_text(item, f"{field_name} entry")


__all__ = [
    "RESOURCE_COCKPIT_PUBLIC_EXPORTS",
    "_enum_value",
    "_now_utc",
    "_require_text",
    "_require_text_tuple",
    "scheduler_machine_profile_provider",
]
