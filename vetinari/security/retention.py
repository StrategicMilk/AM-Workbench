"""Privacy retention jobs for local learning and evidence stores."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def purge_privacy_retention_stores_30d(
    *,
    now: datetime | None = None,
    training_collector: Any | None = None,
    feedback_store: Any | None = None,
    receipt_store: Any | None = None,
) -> dict[str, int]:
    """Purge all local stores governed by the 30-day privacy retention policy.

    Returns:
        Value produced for the caller.
    """
    if training_collector is None:
        from vetinari.learning.training_collector import get_training_collector

        training_collector = get_training_collector()
    if feedback_store is None:
        from vetinari.learning.feedback_store import get_feedback_store

        feedback_store = get_feedback_store()
    if receipt_store is None:
        from vetinari.receipts.store import WorkReceiptStore

        receipt_store = WorkReceiptStore()

    return {
        "training_records_purged": int(training_collector.purge_expired_records(cutoff_days=30, now=now)),
        "feedback_signals_purged": int(feedback_store.purge_expired_signals(cutoff_days=30, now=now)),
        "receipts_purged": int(receipt_store.purge_expired_receipts_all_projects(cutoff_days=30, now=now)),
    }


__all__ = ["purge_privacy_retention_stores_30d"]
