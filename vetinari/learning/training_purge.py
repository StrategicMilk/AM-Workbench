"""Training-record retention helpers."""

from __future__ import annotations

from typing import Any


def purge_training_records_30d(collector: Any | None = None) -> int:
    """Purge training records outside the 30-day retention window.

    Returns:
        Number of records purged.
    """
    if collector is None:
        from vetinari.learning.training_collector import get_training_collector

        collector = get_training_collector()
    return int(collector.purge_expired_records(cutoff_days=30))


def delete_training_records_for_subject(subject: str, collector: Any | None = None) -> dict[str, int]:
    """Delete training records and trace artifacts containing a subject marker.

    Args:
        subject: Subject marker to delete.
        collector: Optional collector override for tests.

    Returns:
        Counts of deleted records and trace artifacts.
    """
    if collector is None:
        from vetinari.learning.training_collector import get_training_collector

        collector = get_training_collector()
    result = collector.delete_records_for_subject(subject)
    return {
        "records_deleted": int(result.get("records_deleted", 0)),
        "traces_deleted": int(result.get("traces_deleted", 0)),
    }
