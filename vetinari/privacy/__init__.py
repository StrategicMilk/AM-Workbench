"""Privacy envelope helpers for persistence and exposure boundaries."""

from __future__ import annotations

from vetinari.privacy.envelope import (
    PRIVACY_ENVELOPE_KEY,
    PrivacyClass,
    RetentionPolicy,
    extract_envelope,
    extract_privacy_envelope,
    privacy_receipt,
    require_privacy_envelope,
    wrap_for_persistence,
    wrap_privacy_envelope,
)
from vetinari.privacy.erasure_registry import (
    ErasureRecord,
    build_erasure_token,
    entry_matches_subject,
    entry_subject_id,
    filter_subject_export,
    redact_entry_for_export,
    register_erasure_record,
)

__all__ = [
    "PRIVACY_ENVELOPE_KEY",
    "ErasureRecord",
    "PrivacyClass",
    "RetentionPolicy",
    "build_erasure_token",
    "entry_matches_subject",
    "entry_subject_id",
    "extract_envelope",
    "extract_privacy_envelope",
    "filter_subject_export",
    "privacy_receipt",
    "redact_entry_for_export",
    "register_erasure_record",
    "require_privacy_envelope",
    "wrap_for_persistence",
    "wrap_privacy_envelope",
]
