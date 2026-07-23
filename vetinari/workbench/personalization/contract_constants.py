"""Shared personalization contract constants."""

from __future__ import annotations

BLOCKER_MISSING_PROVENANCE = "missing_provenance"
BLOCKER_MISSING_ALLOWED_USE = "missing_allowed_use"
BLOCKER_MISSING_RETENTION = "missing_retention"
BLOCKER_MISSING_REVOCATION = "missing_revocation"
BLOCKER_MISSING_AUDIT_TRAIL = "missing_audit_trail"
BLOCKER_MISSING_CONSENT = "missing_consent"
BLOCKER_MISSING_REDACTION = "missing_redaction"
BLOCKER_MISSING_DEDUPE = "missing_dedupe"
BLOCKER_MISSING_SPLIT_FIREWALL = "missing_split_firewall"
BLOCKER_MISSING_SENSITIVITY_REVIEW = "missing_sensitivity_review"
BLOCKER_MISSING_DEPENDENCY = "missing_dependency"
BLOCKER_MISSING_CONFIDENCE = "missing_confidence"
BLOCKER_RAW_USER_LOG = "raw_user_log_not_trainable"
BLOCKER_PROFILE_FACT = "profile_fact_not_trainable"
BLOCKER_SENSITIVE_CONTEXT = "sensitive_context_not_trainable"
BLOCKER_OPAQUE_MODEL_WEIGHTS = "opaque_model_weights_not_allowed"
BLOCKER_EXPIRED = "profile_record_expired"
BLOCKER_REVOKED = "profile_record_revoked"
BLOCKER_DELETE_REQUESTED = "profile_record_delete_requested"
BLOCKER_CONFLICT = "profile_record_conflict"
BLOCKER_DOWNSTREAM_GATE_MISSING = "downstream_anti_sycophancy_gate_missing"
BLOCKER_DOWNSTREAM_GATE_FAILED = "downstream_anti_sycophancy_gate_failed"

__all__ = [
    "BLOCKER_CONFLICT",
    "BLOCKER_DELETE_REQUESTED",
    "BLOCKER_DOWNSTREAM_GATE_FAILED",
    "BLOCKER_DOWNSTREAM_GATE_MISSING",
    "BLOCKER_EXPIRED",
    "BLOCKER_MISSING_ALLOWED_USE",
    "BLOCKER_MISSING_AUDIT_TRAIL",
    "BLOCKER_MISSING_CONFIDENCE",
    "BLOCKER_MISSING_CONSENT",
    "BLOCKER_MISSING_DEDUPE",
    "BLOCKER_MISSING_DEPENDENCY",
    "BLOCKER_MISSING_PROVENANCE",
    "BLOCKER_MISSING_REDACTION",
    "BLOCKER_MISSING_RETENTION",
    "BLOCKER_MISSING_REVOCATION",
    "BLOCKER_MISSING_SENSITIVITY_REVIEW",
    "BLOCKER_MISSING_SPLIT_FIREWALL",
    "BLOCKER_OPAQUE_MODEL_WEIGHTS",
    "BLOCKER_PROFILE_FACT",
    "BLOCKER_RAW_USER_LOG",
    "BLOCKER_REVOKED",
    "BLOCKER_SENSITIVE_CONTEXT",
]
