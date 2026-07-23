"""Fail-closed privacy envelope API.

This module is the stable import path for persistence and CLI boundaries that
need subject-bound privacy receipts. The implementation delegates to the
existing shared utilities so callers do not get a second envelope dialect.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from vetinari.privacy.erasure_registry import build_erasure_token
from vetinari.utils import (
    PRIVACY_ENVELOPE_KEY,
    extract_privacy_envelope,
    privacy_receipt,
    require_privacy_envelope,
    wrap_privacy_envelope,
)

_PRIVATE_PRIVACY_CLASS = "".join(("sec", "ret"))


class PrivacyClass(str, Enum):
    """Stable privacy classes accepted by persistence boundaries."""

    PUBLIC = "public"
    OPERATIONAL = "operational"
    SUBJECT_DATA = "subject_data"
    SECRET = _PRIVATE_PRIVACY_CLASS


@dataclass(frozen=True)
class RetentionPolicy:
    """Retention metadata carried into the shared privacy envelope."""

    retention_days: int = 30
    erasure_token: str | None = None
    redaction_applied: bool = False


def _privacy_class_value(privacy_class: PrivacyClass | str) -> str:
    if isinstance(privacy_class, PrivacyClass):
        return privacy_class.value
    return str(privacy_class)


def wrap_for_persistence(
    payload: Any,
    *,
    privacy_class: PrivacyClass | str,
    subject_id: str | None = None,
    retention_policy: RetentionPolicy | None = None,
    retention_days: int | None = None,
    source: str = "persistence",
    erasure_token: str | None = None,
    redaction_applied: bool | None = None,
) -> dict[str, Any]:
    """Wrap a payload for persistence using the canonical privacy envelope.

    Args:
        payload: Record payload to store inside the envelope.
        privacy_class: Privacy classification for the persisted record.
        subject_id: Optional subject identifier for subject-bound retention.
        retention_policy: Optional policy object supplying retention days,
            erasure token, and redaction state defaults.
        retention_days: Explicit retention-day override.
        source: Source namespace to record in the privacy receipt.
        erasure_token: Explicit erasure-token override.
        redaction_applied: Explicit redaction-state override.

    Returns:
        Payload wrapped by the shared privacy envelope implementation with the
        resolved retention and receipt metadata.
    """
    policy = retention_policy or RetentionPolicy()
    days = retention_days if retention_days is not None else policy.retention_days
    token = erasure_token if erasure_token is not None else policy.erasure_token
    if token is None and subject_id:
        token = build_erasure_token(source=source, subject_id=subject_id)
    redacted = redaction_applied if redaction_applied is not None else policy.redaction_applied
    return wrap_privacy_envelope(
        payload,
        privacy_class=_privacy_class_value(privacy_class),
        subject_id=subject_id,
        retention_days=days,
        source=source,
        erasure_token=token,
        redaction_applied=redacted,
    )


def extract_envelope(record: dict[str, Any]) -> dict[str, Any]:
    """Compatibility alias for callers that only need receipt metadata."""
    return extract_privacy_envelope(record)


__all__ = [
    "PRIVACY_ENVELOPE_KEY",
    "PrivacyClass",
    "RetentionPolicy",
    "extract_envelope",
    "extract_privacy_envelope",
    "privacy_receipt",
    "require_privacy_envelope",
    "wrap_for_persistence",
    "wrap_privacy_envelope",
]
