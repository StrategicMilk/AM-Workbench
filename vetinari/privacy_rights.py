"""Privacy rights request handlers for local Workbench data."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from vetinari.security.erasure import ErasureResult, erase_subject_everywhere
from vetinari.security.fail_closed import sanitize_untrusted_text

logger = logging.getLogger(__name__)


class SubjectExportStore(Protocol):
    """Protocol for stores that can export subject-visible data."""

    def export_subject_data(self, subject: str) -> dict[str, Any]:
        """Return data visible to the subject.

        Returns:
            Serializable subject export payload.
        """
        ...


class SubjectOptOutStore(Protocol):
    """Protocol for stores that can persist opt-out decisions."""

    def record_subject_opt_out(self, subject: str, *, reason: str) -> dict[str, Any]:
        """Record a subject opt-out decision.

        Returns:
            Serializable opt-out record payload.
        """
        ...


class SubjectErasureStore(Protocol):
    """Protocol for additional local stores that can erase subject data."""

    def delete_records_for_subject(self, subject: str) -> int | dict[str, int]:
        """Delete subject-bound records from the store."""
        ...


class LocalPrivacyOptOutStore:
    """Durable local opt-out store for CCPA-style subject requests."""

    def __init__(self, path: str | Path | None = None) -> None:
        root = Path(os.environ.get("VETINARI_DATA_ROOT", ".vetinari"))
        self.path = Path(path) if path is not None else root / "privacy" / "subject_opt_outs.jsonl"

    def record_subject_opt_out(self, subject: str, *, reason: str) -> dict[str, Any]:
        """Append a durable opt-out decision for a subject.

        Returns:
            Stored opt-out record and backing path.
        """
        record = {
            "schema_version": "privacy-rights-opt-out.v1",
            "subject": _clean_subject(subject),
            "reason": sanitize_untrusted_text(reason.strip() or "subject-request", max_length=1_000),
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
            "effect": "exclude_from_training_capture_and_external_sale_or_share",
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return {"record": record, "path": str(self.path)}

    def subject_is_opted_out(self, subject: str) -> bool:
        """Check the local JSONL ledger for a durable opt-out decision.

        Args:
            subject: Subject id to match against persisted opt-out records.

        Returns:
            ``True`` when a matching opt-out record exists; otherwise ``False``.

        Raises:
            ValueError: If ``subject`` is empty after trimming whitespace.
        """
        clean = _clean_subject(subject)
        if not self.path.exists():
            return False
        with self.path.open(encoding="utf-8") as handle:
            for raw in handle:
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping malformed privacy opt-out record in %s",
                        self.path,
                        exc_info=True,
                    )
                    continue
                if isinstance(record, dict) and record.get("subject") == clean:
                    return True
        return False


@dataclass(frozen=True, slots=True)
class PrivacyRightsResult:
    """Serializable privacy-rights response."""

    request_type: str
    subject: str
    status: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_type": self.request_type,
            "subject": self.subject,
            "status": self.status,
            "payload": self.payload,
        }

    def __repr__(self) -> str:
        return (
            f"PrivacyRightsResult(request_type={self.request_type!r}, subject={self.subject!r}, status={self.status!r})"
        )


def handle_right_to_erasure(subject: str, **stores: Any) -> PrivacyRightsResult:
    """Physically erase a subject from configured local privacy stores.

    Returns:
        Completed erasure result payload.
    """
    clean_subject = _clean_subject(subject)
    additional_stores = stores.pop("additional_erasure_stores", None)
    result: ErasureResult = erase_subject_everywhere(clean_subject, **stores)
    additional = _erase_additional_subject_stores(clean_subject, additional_stores)
    payload = result.to_dict()
    if additional:
        payload["additional_stores"] = additional
        payload["total_deleted"] += sum(int(row.get("records_deleted", 0)) for row in additional.values())
    return PrivacyRightsResult(
        request_type="erasure",
        subject=clean_subject,
        status="completed",
        payload=payload,
    )


def handle_right_to_know(
    subject: str,
    *,
    export_store: SubjectExportStore | None = None,
    **stores: Any,
) -> PrivacyRightsResult:
    """Return a subject data export across local privacy-relevant stores.

    Returns:
        Completed right-to-know export payload.

    Raises:
        TypeError: If the configured export store returns a non-dict payload.
    """
    clean_subject = _clean_subject(subject)
    payload = (
        export_store.export_subject_data(clean_subject)
        if export_store is not None
        else export_subject_data_everywhere(clean_subject, **stores)
    )
    if not isinstance(payload, dict):
        raise TypeError("export_subject_data must return a dict")
    return PrivacyRightsResult(
        request_type="right_to_know",
        subject=clean_subject,
        status="completed",
        payload=payload,
    )


def handle_right_to_opt_out(
    subject: str,
    *,
    opt_out_store: SubjectOptOutStore | None = None,
    reason: str = "subject-request",
    store_path: str | Path | None = None,
) -> PrivacyRightsResult:
    """Record a subject opt-out decision in a caller-provided governance store.

    Returns:
        Completed opt-out result payload.

    Raises:
        TypeError: If the opt-out store returns a non-dict payload.
    """
    clean_subject = _clean_subject(subject)
    opt_out_store = opt_out_store or LocalPrivacyOptOutStore(path=store_path)
    payload = opt_out_store.record_subject_opt_out(clean_subject, reason=reason.strip() or "subject-request")
    if not isinstance(payload, dict):
        raise TypeError("record_subject_opt_out must return a dict")
    return PrivacyRightsResult(
        request_type="opt_out",
        subject=clean_subject,
        status="completed",
        payload=payload,
    )


def subject_is_opted_out(subject: str, *, store_path: str | Path | None = None) -> bool:
    """Return whether local training/sharing paths must exclude ``subject``."""
    return LocalPrivacyOptOutStore(path=store_path).subject_is_opted_out(subject)


def require_subject_not_opted_out(subject: str, *, store_path: str | Path | None = None) -> None:
    """Fail closed before training capture or external sharing for opted-out subjects.

    Args:
        subject: Subject id to check against the durable opt-out ledger.
        store_path: Optional JSONL opt-out ledger path for tests or alternate data roots.

    Raises:
        PermissionError: If the subject has a persisted opt-out decision.
        ValueError: If ``subject`` is empty after trimming whitespace.
    """
    clean_subject = _clean_subject(subject)
    if subject_is_opted_out(clean_subject, store_path=store_path):
        raise PermissionError(f"subject {clean_subject!r} has opted out of training capture and sharing")


def export_subject_data_everywhere(
    subject: str,
    *,
    training_collector: Any | None = None,
    feedback_store: Any | None = None,
    receipt_store: Any | None = None,
    additional_export_stores: Mapping[str, SubjectExportStore] | None = None,
) -> dict[str, Any]:
    """Export subject-matching local records from privacy-relevant stores.

    Returns:
        Serializable export payload grouped by store.
    """
    marker = _clean_subject(subject)
    if training_collector is None:
        from vetinari.learning.training_collector import get_training_collector

        training_collector = get_training_collector()
    if feedback_store is None:
        from vetinari.learning.feedback_store import get_feedback_store

        feedback_store = get_feedback_store()
    if receipt_store is None:
        from vetinari.receipts.store import WorkReceiptStore

        receipt_store = WorkReceiptStore()

    training_records = [
        record for record in _training_records(training_collector) if _record_matches_subject(record, marker)
    ]
    feedback_signals = [
        signal for signal in feedback_store.list_signals(limit=None) if _record_matches_subject(signal, marker)
    ]
    receipts = [receipt for receipt in _receipt_records(receipt_store) if _record_matches_subject(receipt, marker)]
    additional_exports = _export_additional_subject_stores(marker, additional_export_stores)
    return {
        "schema_version": "privacy-rights-export.v1",
        "subject": marker,
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "stores": {
            "training_records": training_records,
            "feedback_signals": feedback_signals,
            "work_receipts": receipts,
            **additional_exports,
        },
        "counts": {
            "training_records": len(training_records),
            "feedback_signals": len(feedback_signals),
            "work_receipts": len(receipts),
            **{name: _export_count(value) for name, value in additional_exports.items()},
        },
    }


def _export_additional_subject_stores(
    subject: str,
    stores: Mapping[str, SubjectExportStore] | None,
) -> dict[str, Any]:
    exports: dict[str, Any] = {}
    for name, store in (stores or {}).items():
        if not name or not hasattr(store, "export_subject_data"):
            raise TypeError("additional_export_stores values must implement export_subject_data(subject)")
        payload = store.export_subject_data(subject)
        if not isinstance(payload, dict):
            raise TypeError(f"additional export store {name!r} returned non-dict payload")
        exports[str(name)] = payload.get("records", payload)
    return exports


def _erase_additional_subject_stores(
    subject: str,
    stores: Mapping[str, SubjectErasureStore] | None,
) -> dict[str, dict[str, int]]:
    results: dict[str, dict[str, int]] = {}
    for name, store in (stores or {}).items():
        delete = getattr(store, "delete_records_for_subject", None) or getattr(store, "erase_subject", None)
        if not callable(delete):
            raise TypeError(
                "additional_erasure_stores values must implement delete_records_for_subject or erase_subject"
            )
        raw = delete(subject)
        if isinstance(raw, dict):
            deleted = int(raw.get("records_deleted", raw.get("deleted", 0)))
        else:
            deleted = int(raw)
        _verify_additional_store_erased(subject, store)
        results[str(name)] = {"records_deleted": deleted}
    return results


def _verify_additional_store_erased(subject: str, store: Any) -> None:
    for method_name in ("count_records_for_subject", "records_for_subject", "export_subject_data"):
        method = getattr(store, method_name, None)
        if not callable(method):
            continue
        remaining = method(subject)
        if isinstance(remaining, dict):
            remaining = remaining.get("records", remaining.get("count", []))
        if isinstance(remaining, int) and remaining > 0:
            raise RuntimeError("additional subject store still contains erased records")
        if not isinstance(remaining, int) and remaining:
            raise RuntimeError("additional subject store still contains erased records")
        return


def _export_count(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        records = value.get("records")
        if isinstance(records, list):
            return len(records)
    return 1 if value else 0


def _training_records(training_collector: Any) -> list[dict[str, Any]]:
    if hasattr(training_collector, "flush"):
        training_collector.flush()
    if hasattr(training_collector, "_load_all"):
        records = training_collector._load_all()
    else:
        records = []
    return [_record_to_dict(record) for record in records]


def _receipt_records(receipt_store: Any) -> list[dict[str, Any]]:
    project_ids = receipt_store._project_ids() if hasattr(receipt_store, "_project_ids") else []
    rows: list[dict[str, Any]] = []
    for project_id in project_ids:
        rows.extend(_record_to_dict(receipt) for receipt in receipt_store.iter_receipts(project_id))
    return rows


def _record_matches_subject(record: dict[str, Any], subject: str) -> bool:
    """Return True only for explicit subject bindings, never substring hits."""
    for key in ("subject", "subject_id", "privacy_subject_id", "user_id"):
        if record.get(key) == subject:
            return True
    metadata = record.get("metadata")
    if isinstance(metadata, dict) and _record_matches_subject(metadata, subject):
        return True
    receipt = record.get("privacy_receipt") or record.get("_privacy_envelope")
    if isinstance(receipt, dict) and receipt.get("subject_id") == subject:
        return True
    return _record_summary_marks_subject(record, subject)


def _record_summary_marks_subject(record: dict[str, Any], subject: str) -> bool:
    marker = re.compile(
        rf"(?:^|\b)(?:subject|subject_id|privacy_subject_id|user_id)\s*[=: ]\s*{re.escape(subject)}(?:\b|$)"
    )
    for key in ("inputs_summary", "outputs_summary"):
        value = record.get(key)
        if isinstance(value, str) and marker.search(value):
            return True
    return False


def _record_to_dict(record: Any) -> dict[str, Any]:
    if isinstance(record, dict):
        return dict(record)
    if hasattr(record, "to_dict"):
        value = record.to_dict()
        if isinstance(value, dict):
            return value
    if hasattr(record, "__dict__"):
        return dict(record.__dict__)
    return {"value": str(record)}


def _clean_subject(subject: str) -> str:
    return sanitize_untrusted_text(subject, max_length=512)


__all__ = [
    "LocalPrivacyOptOutStore",
    "PrivacyRightsResult",
    "SubjectExportStore",
    "SubjectOptOutStore",
    "export_subject_data_everywhere",
    "handle_right_to_erasure",
    "handle_right_to_know",
    "handle_right_to_opt_out",
    "require_subject_not_opted_out",
    "subject_is_opted_out",
]
