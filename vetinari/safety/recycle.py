"""RecycleStore — short-grace-window recycle bin backed by LifecycleStore.

``RecycleStore`` is a thin facade over ``LifecycleStore`` configured with
``RecyclePolicy``.  It is the ONLY path in the system that hard-deletes bytes
(via ``purge_expired()``), and that method is the SHARD-02 / VET142 exclusion
target — its exact qualified name ``vetinari.safety.recycle.RecycleStore.purge_expired``
MUST remain stable so the VET142 pre-commit check can exclude it from the
"no bare shutil.rmtree in production code" rule.

All other destructive routes call ``RecycleStore.retire()`` BEFORE any
filesystem deletion so the entity can be restored within the grace window.
"""

from __future__ import annotations

import logging
from pathlib import Path

from vetinari.lifecycle.policies import RecyclePolicy
from vetinari.lifecycle.store import LifecycleRecord, LifecycleStore
from vetinari.safety.safety_defaults import load_safety_defaults

logger = logging.getLogger(__name__)


class RecycleStore:
    """Thin facade over LifecycleStore with RecyclePolicy.

    Entities retired here are restorable within the grace window.  After the
    grace window expires they are eligible for permanent deletion via
    ``purge_expired()``.

    When called with no arguments, ``root`` and ``grace_hours`` are read from
    ``config/safety_defaults.yaml`` via ``load_safety_defaults()`` so that
    all callers automatically pick up per-deployment configuration.

    Args:
        root: Root directory for recycle records.  Defaults to the value
            from ``safety_defaults.yaml`` (``outputs/recycle``).
        grace_hours: How many hours a record stays in ``"active_grace"``
            before becoming ``"expired"``.  Defaults to the value from
            ``safety_defaults.yaml`` (72).
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        grace_hours: int | None = None,
    ) -> None:
        """Initialise the store, reading defaults from safety_defaults.yaml when args are omitted.

        Args:
            root: Root directory for recycle records.  If ``None``, reads
                ``recycle_policy.recycle_root`` from ``config/safety_defaults.yaml``.
            grace_hours: Grace window in hours before records become eligible
                for permanent deletion.  If ``None``, reads
                ``recycle_policy.grace_hours`` from ``config/safety_defaults.yaml``.
        """
        if root is None or grace_hours is None:
            defaults = load_safety_defaults()
            effective_root = root if root is not None else defaults.recycle_root
            effective_grace = grace_hours if grace_hours is not None else defaults.grace_hours
        else:
            effective_root = root
            effective_grace = grace_hours
        self._policy = RecyclePolicy(grace_hours=effective_grace)
        self._store = LifecycleStore(root=effective_root, policy=self._policy)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retire(
        self,
        path: Path,
        reason: str,
        work_receipt_id: str | None = None,
    ) -> LifecycleRecord:
        """Move ``path`` into the recycle bin with a manifest.

        Must be called BEFORE any filesystem deletion of the target so the
        entity is restorable within the grace window.

        Args:
            path: File or directory to retire.  Must exist.
            reason: Human-readable reason for the retirement.
            work_receipt_id: Optional receipt identifier to embed in the
                manifest.

        Returns:
            A ``LifecycleRecord`` describing the retired entity.

        Raises:
            FileNotFoundError: If ``path`` does not exist.
            OSError: If the move or manifest write fails (original untouched).
        """
        return self._store.retire(path, reason=reason, work_receipt_id=work_receipt_id)

    def restore(self, record_id: str) -> None:
        """Restore a recycled entity to its original path.

        Args:
            record_id: UUID hex of the record to restore.

        Raises:
            KeyError: If no record with this ID exists.
            FileExistsError: If the original path is already occupied.
        """
        self._store.restore(record_id)

    def list_all(self) -> list[LifecycleRecord]:
        """Return all records in the recycle bin (both tiers).

        Returns:
            List of all ``LifecycleRecord`` objects sorted by
            ``retired_at_utc`` descending.
        """
        return self._store.list()

    def purge_expired(self, grace_hours: int | None = None) -> list[LifecycleRecord]:
        """Permanently delete all expired records (past the grace window).

        This is the ONLY method in the system that hard-deletes bytes.  It is
        the SHARD-02 VET142 exclusion target — the method name and module path
        MUST NOT be changed.

        Args:
            grace_hours: Override the instance grace window for this call.
                If ``None``, uses the instance's configured grace_hours.

        Returns:
            List of ``LifecycleRecord`` objects that were permanently deleted.
        """
        from datetime import datetime, timezone

        from vetinari.lifecycle.policies import RecyclePolicy

        effective_grace = grace_hours if grace_hours is not None else self._policy.grace_hours
        # Use a temporary policy to evaluate buckets with the caller-supplied grace.
        eval_policy = RecyclePolicy(grace_hours=effective_grace)
        now = datetime.now(timezone.utc)

        all_records = self._store.list()
        purged: list[LifecycleRecord] = []

        for record in all_records:
            bucket = eval_policy.surface_buckets(record, now)
            if bucket == "expired":
                try:
                    self._store.purge(record.record_id)
                    purged.append(record)
                    logger.info(
                        "recycle.purge_expired: permanently deleted record %s (original=%s)",
                        record.record_id,
                        record.original_path,
                    )
                except Exception as exc:
                    logger.warning(
                        "recycle.purge_expired: failed to purge record %s — %s",
                        record.record_id,
                        exc,
                    )

        return purged


def system_internal_recycle(
    path: Path,
    reason: str,
    work_receipt_id: str | None = None,
) -> LifecycleRecord:
    """Retire a path via the shared RecycleStore singleton on behalf of the system.

    Use this for cleanup paths that are not triggered directly by a human
    (e.g. rollback after a failed creation, merge-overwrite cleanup).  The
    call is recorded in the recycle manifest so it is auditable and
    recoverable within the grace window.

    The caller must NOT hold a lock on ``path`` when calling this — the
    RecycleStore acquires its own per-path lock internally.

    Args:
        path: File or directory to retire.  Must exist.
        reason: Human-readable reason for the retirement; stored in the
            manifest and visible in recycle-bin listings.
        work_receipt_id: Optional receipt identifier to embed in the
            manifest for cross-referencing with the WorkReceipt audit trail.

    Returns:
        A ``LifecycleRecord`` describing the retired entity.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        OSError: If the move or manifest write fails (original untouched).
    """
    from vetinari.safety.protected_mutation import get_recycle_store

    store = get_recycle_store()
    record = store.retire(path, reason=reason, work_receipt_id=work_receipt_id)
    logger.info(
        "system_internal_recycle: retired %s (reason=%r, record=%s)",
        path,
        reason,
        record.record_id,
    )
    return record


__all__ = ["RecycleStore", "system_internal_recycle"]
