"""ArchiveStore facade — view-tiered archive built on LifecycleStore.

Provides a high-level API for archiving completed entities with no automatic
hard-delete path.  Records are classified into view tiers by age:

- ``"recent"``  : age <= 7 days
- ``"cooling"`` : 7 < age <= 30 days
- ``"cold"``    : age > 30 days

Tier thresholds are configurable via ``safety_defaults.yaml``.  Hard delete
of an archive record requires explicit ``@protected_mutation`` — the default
UX never exposes a purge button.

``ArchiveStore.sweep()`` is the LW-UX-08 auto-archive hook; call it with a
list of ``ArchiveCandidate`` objects and it archives only those whose cooldown
has elapsed.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from vetinari.lifecycle.policies import ArchiveEntityThresholds as PolicyArchiveEntityThresholds
from vetinari.lifecycle.policies import ArchivePolicy
from vetinari.lifecycle.store import LifecycleRecord, LifecycleStore
from vetinari.safety.safety_defaults import load_safety_defaults  # runtime — used in ArchiveStore.__init__

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ArchiveCandidate:
    """Descriptor for a path that the sweep job should consider archiving.

    Attributes:
        path: The file or directory to potentially archive.
        completed_at: When the work producing this path finished.
        cooldown_hours: How long to wait after completion before archiving.
        reason: Human-readable reason (written into the manifest).
        work_receipt_id: Optional receipt identifier to record in the manifest.
        entity_type: Optional type key used for archive tier overrides.
    """

    path: Path
    completed_at: datetime
    cooldown_hours: float
    reason: str
    work_receipt_id: str | None = None
    entity_type: str | None = None

    def __repr__(self) -> str:
        """Show path and reason for debugging."""
        return f"ArchiveCandidate(path={self.path!r}, reason={self.reason!r})"


class ArchiveStore:
    """View-tiered archive facade over LifecycleStore.

    Entities are moved aside with a manifest and classified into view tiers
    based on their age.  No hard-delete path is exposed via the public API.

    When called with no arguments, ``root``, ``recent_days``, and
    ``cooling_days`` are read from ``config/safety_defaults.yaml`` via
    ``load_safety_defaults()`` so that all callers automatically pick up
    per-deployment configuration.

    Args:
        root: Root directory for archive records.  Defaults to the value
            from ``safety_defaults.yaml`` (``outputs/archive``).
        recent_days: Records younger than this are in the ``"recent"`` tier.
            Defaults to the value from ``safety_defaults.yaml`` (7).
        cooling_days: Records younger than this (but > recent) are
            ``"cooling"``; older records are ``"cold"``.  Defaults to the
            value from ``safety_defaults.yaml`` (30).
        entity_thresholds: Per-entity-type view-tier threshold overrides.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        recent_days: int | None = None,
        cooling_days: int | None = None,
        entity_thresholds: dict[str, PolicyArchiveEntityThresholds] | None = None,
    ) -> None:
        """Initialise the store, reading defaults from safety_defaults.yaml when args are omitted.

        Args:
            root: Root directory for all archive records.  If ``None``, reads
                ``archive_policy.archive_root`` from ``config/safety_defaults.yaml``.
            recent_days: Age threshold (days) for the ``"recent"`` tier.  If
                ``None``, reads ``archive_policy.recent_days`` from the YAML.
            cooling_days: Age threshold (days) for the ``"cooling"`` tier;
                records older than this fall into ``"cold"``.  If ``None``,
                reads ``archive_policy.cooling_days`` from the YAML.
            entity_thresholds: Per-entity-type tier threshold overrides.  If
                ``None``, reads ``archive_policy.entity_thresholds`` from YAML.
        """
        if root is None or recent_days is None or cooling_days is None or entity_thresholds is None:
            defaults = load_safety_defaults()
            effective_root = root if root is not None else defaults.archive_root
            effective_recent = recent_days if recent_days is not None else defaults.recent_days
            effective_cooling = cooling_days if cooling_days is not None else defaults.cooling_days
            effective_entity_thresholds = (
                entity_thresholds
                if entity_thresholds is not None
                else {
                    key: PolicyArchiveEntityThresholds(value.recent_days, value.cooling_days)
                    for key, value in defaults.entity_thresholds.items()
                }
            )
        else:
            effective_root = root
            effective_recent = recent_days
            effective_cooling = cooling_days
            effective_entity_thresholds = entity_thresholds
        policy = ArchivePolicy(
            recent_days=effective_recent,
            cooling_days=effective_cooling,
            entity_thresholds=effective_entity_thresholds,
        )
        self._store = LifecycleStore(root=effective_root, policy=policy)
        self._policy = policy

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def archive(
        self,
        path: Path,
        reason: str,
        work_receipt_id: str | None = None,
        entity_type: str | None = None,
    ) -> LifecycleRecord:
        """Move ``path`` into the archive store and record a manifest.

        Args:
            path: File or directory to archive.  Must exist.
            reason: Human-readable reason for archiving.
            work_receipt_id: Optional work receipt identifier to embed in the
                manifest for audit linkage.
            entity_type: Optional type key used for archive tier overrides.

        Returns:
            A ``LifecycleRecord`` describing the archived entity.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            OSError: If the move or manifest write fails.
        """
        return self._store.retire(path, reason=reason, work_receipt_id=work_receipt_id, entity_type=entity_type)

    def unarchive(self, record_id: str) -> None:
        """Restore an archived entity to its original path.

        Args:
            record_id: UUID hex of the record to restore.

        Raises:
            KeyError: If no record with this ID exists.
            FileExistsError: If the original path is already occupied.
        """
        self._store.restore(record_id)

    def list_by_tier(
        self,
        tier: Literal["recent", "cooling", "cold"],
    ) -> list[LifecycleRecord]:
        """Return all records in the given view tier.

        Args:
            tier: One of ``"recent"``, ``"cooling"``, or ``"cold"``.

        Returns:
            List of ``LifecycleRecord`` objects in the requested tier,
            sorted by ``retired_at_utc`` descending.
        """
        from vetinari.lifecycle.policies import PolicyFilter

        return self._store.list(filter=PolicyFilter(bucket=tier))

    def search(self, query: str) -> list[LifecycleRecord]:
        """Find records by case-insensitive substring match.

        Matches substrings against ``original_path`` and ``reason``. Receipt
        IDs are stored redacted, so receipt lookup matches the exact query via
        the persisted SHA-256 digest. Slow path is acceptable per design
        (archive queries are infrequent).

        Args:
            query: Substring to search for (case-insensitive).

        Returns:
            List of matching ``LifecycleRecord`` objects.
        """
        q = query.lower()
        receipt_query_sha256 = hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()
        return [
            record
            for record in self._store.list()
            if (
                q in record.original_path.lower()
                or q in record.reason.lower()
                or (record.work_receipt_id is not None and q in record.work_receipt_id.lower())
                or (record.work_receipt_id_sha256 is not None and receipt_query_sha256 == record.work_receipt_id_sha256)
            )
        ]

    def sweep(self, candidates: Iterable[ArchiveCandidate]) -> list[LifecycleRecord]:
        """Archive candidates whose cooldown has elapsed; idempotent.

        Candidates that are already absent (already archived or deleted by
        other means) are silently skipped.  Running sweep twice on the same
        list is safe.

        Args:
            candidates: Iterable of ``ArchiveCandidate`` objects.

        Returns:
            List of ``LifecycleRecord`` objects for newly archived entities.
        """
        archived: list[LifecycleRecord] = []
        now = datetime.now(timezone.utc)

        for candidate in candidates:
            # Skip if the path no longer exists (already archived or removed).
            if not candidate.path.exists():
                continue

            completed_at = candidate.completed_at
            if completed_at.tzinfo is None:
                completed_at = completed_at.replace(tzinfo=timezone.utc)

            elapsed = now - completed_at
            if elapsed < timedelta(hours=candidate.cooldown_hours):
                # Cooldown not yet elapsed — leave in place.
                continue

            try:
                record = self._store.retire(
                    candidate.path,
                    reason=candidate.reason,
                    work_receipt_id=candidate.work_receipt_id,
                    entity_type=candidate.entity_type,
                )
                archived.append(record)
                logger.info(
                    "archive.sweep: archived %s (reason=%s)",
                    candidate.path,
                    candidate.reason,
                )
            except Exception as exc:
                logger.warning(
                    "archive.sweep: failed to archive %s — %s",
                    candidate.path,
                    exc,
                )

        return archived

    # ------------------------------------------------------------------
    # Protected hard-delete (only reachable via @protected_mutation)
    # ------------------------------------------------------------------

    def purge_record_with_intent(self, record_id: str, *, intent: object = None) -> None:
        """Permanently delete an archive record after confirmed intent.

        This is the only public hard-delete path on ``ArchiveStore``.  The
        method validates intent, emits a ``DESTRUCTIVE_OP`` ``WorkReceipt``,
        and then calls ``LifecycleStore.purge(record_id, force=True)`` to
        bypass the policy default-deny.

        Implemented inline (rather than wrapped with ``@protected_mutation``)
        to avoid a load-time circular import: the lifecycle package is in
        the import chain that brings up ``vetinari.safety.protected_mutation``,
        so applying the decorator at module load fails before
        ``DestructiveAction`` is bound.  The contract — intent validation,
        receipt emission, fail-closed on missing intent — is identical.

        Args:
            record_id: UUID hex of the record to permanently delete.
            intent: A ``ConfirmedIntent`` carrying ``confirmed_by`` and
                ``reason``.  Required.

        Raises:
            UnconfirmedDestructiveAction: If ``intent`` is missing or not a
                ``ConfirmedIntent`` instance.
            KeyError: If no record with this ID exists.
        """
        # Lazy import — protected_mutation is loaded by now if anyone is
        # actually calling this method (it cannot be the case during module
        # bring-up because the call requires an instance).
        from vetinari.safety.protected_mutation import (
            ConfirmedIntent,
            DestructiveAction,
            UnconfirmedDestructiveAction,
            emit_destructive_op_receipt,
        )

        if intent is None:
            raise UnconfirmedDestructiveAction(
                "Missing required 'intent: ConfirmedIntent' argument. "
                "Archive purge requires an explicit confirmed intent with "
                "confirmed_by and reason fields.",
            )
        if not isinstance(intent, ConfirmedIntent):
            raise UnconfirmedDestructiveAction(
                f"'intent' must be a ConfirmedIntent instance, got {type(intent).__name__}.",
            )

        success = False
        error_msg = ""
        try:
            self._store.purge(record_id, force=True)
            success = True
        except Exception as exc:
            error_msg = str(exc)
            raise
        finally:
            emit_destructive_op_receipt(
                action=DestructiveAction.PURGE_ARCHIVE,
                intent=intent,
                project_id="archive",
                target_path=record_id,
                recycle_record_id=None,
                success=success,
                error_msg=error_msg,
            )


__all__ = [
    "ArchiveCandidate",
    "ArchiveStore",
]
