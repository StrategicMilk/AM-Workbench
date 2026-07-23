"""Unified privacy erasure coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vetinari.guards import GateError


@dataclass(frozen=True, slots=True)
class ErasureResult:
    """Counts from an exact-subject erasure run."""

    training_records_deleted: int = 0
    training_traces_deleted: int = 0
    feedback_signals_deleted: int = 0
    receipts_deleted: int = 0
    telemetry_records_deleted: int = 0

    @property
    def total_deleted(self) -> int:
        """Return total persisted rows/artifacts removed."""
        return (
            self.training_records_deleted
            + self.training_traces_deleted
            + self.feedback_signals_deleted
            + self.receipts_deleted
            + self.telemetry_records_deleted
        )

    def to_dict(self) -> dict[str, int]:
        """Return a JSON-friendly result."""
        return {
            "training_records_deleted": self.training_records_deleted,
            "training_traces_deleted": self.training_traces_deleted,
            "feedback_signals_deleted": self.feedback_signals_deleted,
            "receipts_deleted": self.receipts_deleted,
            "telemetry_records_deleted": self.telemetry_records_deleted,
            "total_deleted": self.total_deleted,
        }

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"training_records_deleted={self.training_records_deleted!r}, "
            f"training_traces_deleted={self.training_traces_deleted!r}, "
            f"feedback_signals_deleted={self.feedback_signals_deleted!r}, "
            f"receipts_deleted={self.receipts_deleted!r}, "
            f"telemetry_records_deleted={self.telemetry_records_deleted!r}"
            ")"
        )


def erase_subject_everywhere(
    subject: str,
    *,
    training_collector: Any | None = None,
    feedback_store: Any | None = None,
    receipt_store: Any | None = None,
    telemetry_store: Any | None = None,
) -> ErasureResult:
    """Erase an exact subject marker from privacy-relevant local stores.

    The coordinator is intentionally strict about the subject string and uses
    existing per-store physical deletion APIs rather than marking rows hidden.

    Returns:
        Value produced for the caller.
    """
    marker = subject.strip()
    if not marker:
        return ErasureResult()

    if training_collector is None:
        from vetinari.learning.training_collector import get_training_collector

        training_collector = get_training_collector()
    if feedback_store is None:
        from vetinari.learning.feedback_store import get_feedback_store

        feedback_store = get_feedback_store()
    if receipt_store is None:
        from vetinari.receipts.store import WorkReceiptStore

        receipt_store = WorkReceiptStore()

    training_result = training_collector.delete_records_for_subject(marker)
    telemetry_deleted = 0
    if telemetry_store is not None:
        telemetry_deleted = int(telemetry_store.erase_subject(marker))

    result = ErasureResult(
        training_records_deleted=int(training_result.get("records_deleted", 0)),
        training_traces_deleted=int(training_result.get("traces_deleted", 0)),
        feedback_signals_deleted=int(feedback_store.delete_signals_for_subject(marker)),
        receipts_deleted=int(receipt_store.delete_receipts_for_subject(marker)),
        telemetry_records_deleted=telemetry_deleted,
    )
    _verify_subject_erased(marker, training_collector, feedback_store, receipt_store, telemetry_store)
    return result


def _verify_subject_erased(
    marker: str,
    training_collector: Any,
    feedback_store: Any,
    receipt_store: Any,
    telemetry_store: Any | None = None,
) -> None:
    """Fail closed when a store can still find subject data after deletion."""
    checks = (
        (training_collector, ("count_records_for_subject", "records_for_subject")),
        (feedback_store, ("count_signals_for_subject", "signals_for_subject")),
        (receipt_store, ("count_receipts_for_subject", "receipts_for_subject")),
        (telemetry_store, ("count_records_for_subject", "records_for_subject", "count_telemetry_for_subject")),
    )
    for store, method_names in checks:
        if store is None:
            continue
        for method_name in method_names:
            method = getattr(store, method_name, None)
            if not callable(method):
                continue
            remaining = method(marker)
            if isinstance(remaining, int) and remaining > 0:
                raise GateError("erasure", "deletion verification failed - records still present")
            if not isinstance(remaining, int) and remaining:
                raise GateError("erasure", "deletion verification failed - records still present")
            break


__all__ = ["ErasureResult", "erase_subject_everywhere"]
