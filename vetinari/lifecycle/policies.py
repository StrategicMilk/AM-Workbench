"""Lifecycle policy protocol and concrete policy implementations.

Defines the ``Policy`` protocol that ``LifecycleStore`` consumes and the two
concrete policies — ``RecyclePolicy`` (short grace window, hard-delete allowed)
and ``ArchivePolicy`` (unbounded retention, view-tiered, no hard delete) — that
back the ``RecycleStore`` and ``ArchiveStore`` facades respectively.

This is step 1 of the lifecycle subsystem; the shared store and the two facades
are built on top of these policy objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from vetinari.lifecycle.store import LifecycleRecord


# Closed set of bucket names returned by Policy.surface_buckets across all
# concrete policies; PolicyFilter.bucket is typed against this so a typo
# (e.g. "recents") is a static error rather than a silent empty result.
BucketName = Literal["recent", "cooling", "cold", "active_grace", "expired"]

# Default tier thresholds for ArchivePolicy (also in safety_defaults.yaml).
_DEFAULT_RECENT_DAYS: int = 7  # Records younger than this are "recent"
_DEFAULT_COOLING_DAYS: int = 30  # Records younger than this (but > recent) are "cooling"


@runtime_checkable
class Policy(Protocol):
    """Contract that every lifecycle policy must satisfy.

    ``LifecycleStore`` uses only this protocol; concrete implementations are
    free to carry extra state (e.g., configurable thresholds) as long as they
    expose these attributes and the ``surface_buckets`` method.
    """

    name: str
    """Short identifier written into every manifest record (e.g. ``"recycle"``)."""

    allows_hard_delete: bool
    """Whether ``LifecycleStore.purge()`` may permanently delete records."""

    default_grace_hours: int | None
    """How long (hours) before a retired record is eligible for hard delete.

    ``None`` means unbounded — the record is kept indefinitely unless
    explicitly purged via ``@protected_mutation``.
    """

    def surface_buckets(self, record: LifecycleRecord, now: datetime) -> str:
        """Return the view-tier bucket name for a record at the given clock.

        Args:
            record: The lifecycle record to classify.
            now: The current UTC datetime used as the reference point.

        Returns:
            A bucket string understood by the facade layer, e.g.
            ``"recent"``, ``"cooling"``, ``"cold"``, ``"active_grace"``,
            or ``"expired"``.
        """
        ...


@dataclass
class RecyclePolicy:
    """Policy for the recycle bin: short grace window with hard-delete allowed.

    Records younger than ``grace_hours`` are in ``"active_grace"`` (restorable);
    older records are ``"expired"`` and eligible for hard delete via
    ``RecycleStore.purge_expired()``.

    Attributes:
        grace_hours: How many hours a retired record stays in active grace.
    """

    grace_hours: int = 72  # 3-day default — configurable via safety_defaults.yaml

    # Policy protocol fields
    name: str = "recycle"
    allows_hard_delete: bool = True
    default_grace_hours: int | None = None  # overridden by grace_hours

    def __repr__(self) -> str:
        """Show policy name and grace_hours for debugging."""
        return f"RecyclePolicy(grace_hours={self.grace_hours})"

    def __post_init__(self) -> None:
        """Set default_grace_hours to match grace_hours for protocol compliance."""
        self.default_grace_hours = self.grace_hours

    def surface_buckets(self, record: LifecycleRecord, now: datetime) -> str:
        """Return 'active_grace' while within grace, 'expired' past grace.

        Args:
            record: The lifecycle record to classify.
            now: The current UTC datetime used as the reference point.

        Returns:
            ``"active_grace"`` if the record was retired within ``grace_hours``
            ago, otherwise ``"expired"``.
        """
        retired_at = datetime.fromisoformat(record.retired_at_utc)
        if retired_at.tzinfo is None:
            retired_at = retired_at.replace(tzinfo=timezone.utc)
        age = now - retired_at
        if age <= timedelta(hours=self.grace_hours):
            return "active_grace"
        return "expired"


@dataclass(frozen=True, slots=True)
class ArchiveEntityThresholds:
    """Archive view-tier thresholds for one entity type."""

    recent_days: int
    cooling_days: int


@dataclass(frozen=True, slots=True)
class ArchivePolicy:
    """Policy for the archive store: unbounded retention, view-tiered by age.

    Records are never hard-deleted through normal API surface; purging requires
    explicit ``@protected_mutation(DestructiveAction.PURGE_ARCHIVE)``.

    Age tiers:
    - ``"recent"``: age <= ``recent_days``
    - ``"cooling"``: ``recent_days`` < age <= ``cooling_days``
    - ``"cold"``: age > ``cooling_days``

    Attributes:
        recent_days: Records younger than this number of days are ``"recent"``.
        cooling_days: Records younger than this (but older than recent) are
            ``"cooling"``; older records are ``"cold"``.
        entity_thresholds: Optional per-entity-type overrides keyed by
            ``LifecycleRecord.entity_type``.
    """

    recent_days: int = _DEFAULT_RECENT_DAYS
    cooling_days: int = _DEFAULT_COOLING_DAYS
    entity_thresholds: dict[str, ArchiveEntityThresholds] = field(default_factory=dict)

    # Policy protocol fields
    name: str = "archive"
    allows_hard_delete: bool = False
    default_grace_hours: int | None = None  # unbounded

    def __repr__(self) -> str:
        """Show policy name and tier thresholds for debugging."""
        return (
            f"ArchivePolicy(recent_days={self.recent_days}, "
            f"cooling_days={self.cooling_days}, "
            f"entity_thresholds={self.entity_thresholds!r})"
        )

    def _thresholds_for(self, record: LifecycleRecord) -> tuple[int, int]:
        """Return archive thresholds for the record's entity type."""
        entity_type = getattr(record, "entity_type", None)
        if entity_type is not None:
            override = self.entity_thresholds.get(entity_type)
            if override is not None:
                return override.recent_days, override.cooling_days
        return self.recent_days, self.cooling_days

    def surface_buckets(self, record: LifecycleRecord, now: datetime) -> str:
        """Return the view tier for a record based on its age.

        Args:
            record: The lifecycle record to classify.
            now: The current UTC datetime used as the reference point.

        Returns:
            ``"recent"``, ``"cooling"``, or ``"cold"`` based on days elapsed
            since ``record.retired_at_utc``.
        """
        retired_at = datetime.fromisoformat(record.retired_at_utc)
        if retired_at.tzinfo is None:
            retired_at = retired_at.replace(tzinfo=timezone.utc)
        age = now - retired_at
        recent_days, cooling_days = self._thresholds_for(record)
        if age <= timedelta(days=recent_days):
            return "recent"
        if age <= timedelta(days=cooling_days):
            return "cooling"
        return "cold"


@dataclass(frozen=True, slots=True)
class PolicyFilter:
    """Optional filter passed to ``LifecycleStore.list()`` to narrow results.

    Attributes:
        bucket: If set, only records whose ``surface_buckets()`` value matches
            this string are returned.
        reason_contains: If set, only records whose ``reason`` contains this
            substring (case-insensitive) are returned.
        work_receipt_id: If set, only the record with this exact
            ``work_receipt_id`` is returned.
    """

    bucket: BucketName | None = None
    reason_contains: str | None = None
    work_receipt_id: str | None = None


__all__ = [
    "ArchiveEntityThresholds",
    "ArchivePolicy",
    "BucketName",
    "Policy",
    "PolicyFilter",
    "RecyclePolicy",
]
