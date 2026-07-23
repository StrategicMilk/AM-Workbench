"""Subject-bound erasure and export helpers for privacy-bearing records."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class ErasureRecord:
    """A normalized subject erasure/export marker."""

    subject_id: str
    source: str
    token: str
    recorded_at: str

    def __repr__(self) -> str:
        """Represent the erasure marker without exposing the raw subject id."""
        return (
            "ErasureRecord("
            f"source={self.source!r}, "
            f"token={self.token!r}, "
            f"recorded_at={self.recorded_at!r}, "
            "subject_id='<redacted>'"
            ")"
        )


def _require_non_empty(value: str | None, field: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"{field} is required for subject-bound privacy operations")
    return str(value).strip()


def build_erasure_token(*, source: str, subject_id: str) -> str:
    """Derive a deterministic non-PII erasure token for a subject/source pair.

    Args:
        source: Logical store or pipeline namespace that owns the erasure marker.
        subject_id: Raw subject identifier used only as the hash input.

    Returns:
        Stable token in ``<source>:<digest-prefix>`` form without the raw subject id.

    Raises:
        ValueError: If ``source`` or ``subject_id`` is empty after trimming whitespace.
    """
    safe_source = _require_non_empty(source, "source")
    safe_subject = _require_non_empty(subject_id, "subject_id")
    digest = hashlib.sha256(f"{safe_source}\0{safe_subject}".encode()).hexdigest()
    return f"{safe_source}:{digest[:24]}"


def register_erasure_record(*, source: str, subject_id: str) -> ErasureRecord:
    """Build a timestamped erasure marker for a subject/source pair.

    Args:
        source: Logical store or pipeline namespace that owns the marker.
        subject_id: Raw subject identifier retained in the record and omitted from the token.

    Returns:
        Immutable ``ErasureRecord`` containing the subject, source, token, and UTC timestamp.

    Raises:
        ValueError: If ``source`` or ``subject_id`` is empty after trimming whitespace.
    """
    safe_subject = _require_non_empty(subject_id, "subject_id")
    safe_source = _require_non_empty(source, "source")
    return ErasureRecord(
        subject_id=safe_subject,
        source=safe_source,
        token=build_erasure_token(source=safe_source, subject_id=safe_subject),
        recorded_at=datetime.now(timezone.utc).isoformat(),
    )


def _metadata_subject(metadata: Mapping[str, Any] | None) -> str | None:
    if not isinstance(metadata, Mapping):
        return None
    receipt = metadata.get("privacy_receipt")
    if isinstance(receipt, Mapping) and receipt.get("subject_id"):
        return str(receipt["subject_id"])
    privacy = metadata.get("privacy")
    if isinstance(privacy, Mapping) and privacy.get("subject_id"):
        return str(privacy["subject_id"])
    if metadata.get("subject_id"):
        return str(metadata["subject_id"])
    return None


def entry_subject_id(entry: Any) -> str | None:
    """Extract a privacy subject id from a MemoryEntry-like object or dict.

    Args:
        entry: Mapping or object with optional ``metadata`` or top-level privacy
            receipt fields carrying privacy subject fields.

    Returns:
        Explicit subject id when present in recognized metadata locations; otherwise ``None``.
    """
    if isinstance(entry, Mapping):
        subject = _metadata_subject(entry)
        if subject:
            return subject
        metadata = entry.get("metadata")
    else:
        metadata = getattr(entry, "metadata", None)
    return _metadata_subject(metadata if isinstance(metadata, Mapping) else None)


def entry_matches_subject(entry: Any, subject_id: str) -> bool:
    """Check whether an entry is explicitly bound to the requested subject.

    Args:
        entry: Mapping or object whose metadata may contain a privacy subject id.
        subject_id: Subject id that must match exactly after whitespace normalization.

    Returns:
        ``True`` when the entry metadata carries the normalized subject id; otherwise ``False``.

    Raises:
        ValueError: If ``subject_id`` is empty after trimming whitespace.
    """
    safe_subject = _require_non_empty(subject_id, "subject_id")
    return entry_subject_id(entry) == safe_subject


def redact_entry_for_export(entry: Any, *, subject_id: str) -> dict[str, Any]:
    """Serialize an entry for subject export while replacing durable content fields.

    Args:
        entry: Mapping or object exposing ``to_dict()`` that represents a subject-bound record.
        subject_id: Subject id to stamp into the privacy export metadata.

    Returns:
        Export payload with content and summary redacted plus privacy export metadata.

    Raises:
        TypeError: If ``entry`` is neither a mapping nor an object exposing ``to_dict()``.
        ValueError: If ``subject_id`` is empty after trimming whitespace.
    """
    safe_subject = _require_non_empty(subject_id, "subject_id")
    if hasattr(entry, "to_dict"):
        payload = entry.to_dict()
    elif isinstance(entry, Mapping):
        payload = dict(entry)
    else:
        raise TypeError("entry must be a mapping or expose to_dict()")
    payload["content"] = "[redacted:subject-export]"
    payload["summary"] = "[redacted:subject-export]" if payload.get("summary") else ""
    metadata = dict(payload.get("metadata") or {})
    metadata["privacy_export"] = {
        "subject_id": safe_subject,
        "erasure_token": build_erasure_token(source="memory.export", subject_id=safe_subject),
        "redaction_applied": True,
    }
    payload["metadata"] = metadata
    return payload


def filter_subject_export(entries: Iterable[Any], *, subject_id: str) -> list[dict[str, Any]]:
    """Collect redacted exports for records explicitly bound to one subject.

    Args:
        entries: Candidate records to inspect for explicit privacy subject metadata.
        subject_id: Subject id that entries must match before export.

    Returns:
        Redacted payloads for matching entries, preserving the iterable order.

    Raises:
        TypeError: If a matching entry cannot be serialized by ``redact_entry_for_export``.
        ValueError: If ``subject_id`` is empty after trimming whitespace.
    """
    safe_subject = _require_non_empty(subject_id, "subject_id")
    return [
        redact_entry_for_export(entry, subject_id=safe_subject)
        for entry in entries
        if entry_matches_subject(entry, safe_subject)
    ]


__all__ = [
    "ErasureRecord",
    "build_erasure_token",
    "entry_matches_subject",
    "entry_subject_id",
    "filter_subject_export",
    "redact_entry_for_export",
    "register_erasure_record",
]
