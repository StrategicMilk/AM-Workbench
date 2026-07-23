"""Approval lifecycle helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from vetinari.learning.atomic_writers import _write_text_atomic
from vetinari.privacy.envelope import PRIVACY_ENVELOPE_KEY, wrap_for_persistence
from vetinari.security.fail_closed import confine_to_root, sanitize_untrusted_text


def _approval_ref(approval_id: str) -> str:
    safe_approval_id = sanitize_untrusted_text(approval_id, max_length=512)
    return hashlib.sha256(safe_approval_id.encode("utf-8", errors="replace")).hexdigest()


def _resolve_log_path(log_path: str | Path) -> Path:
    raw_path = Path(log_path)
    if any(part == ".." for part in raw_path.parts):
        return confine_to_root(Path.cwd(), raw_path)
    return raw_path


def _approval_record(approval_id: str, event: str) -> dict[str, object]:
    approval_id_hash = _approval_ref(approval_id)
    source_by_event = {
        "created": "governance.create_approval",
        "expired": "governance.expire_approval",
        "revoked": "governance.revoke_approval",
    }
    return {
        "approval_id_sha256": approval_id_hash,
        "approval_id": "[REDACTED]",
        "event": event,
        PRIVACY_ENVELOPE_KEY: wrap_for_persistence(
            {"approval_id_sha256": approval_id_hash, "event": event},
            privacy_class="operational",
            source=source_by_event.get(event, f"governance.{event}_approval"),
            redaction_applied=True,
        )[PRIVACY_ENVELOPE_KEY],
    }


def _append_approval_record(approval_id: str, *, log_path: str | Path, event: str) -> dict[str, bool]:
    approval_id = sanitize_untrusted_text(approval_id, max_length=512)
    path = _resolve_log_path(log_path)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    _write_text_atomic(path, existing + json.dumps(_approval_record(approval_id, event), sort_keys=True) + "\n")
    return {"logged": True}


def create_approval(approval_id: str, *, log_path: str | Path) -> dict[str, bool | str]:
    """Create an approval and append a decision-log record.

    Returns:
        Logging result with the hashed approval identifier.
    """
    approval_id = sanitize_untrusted_text(approval_id, max_length=512)
    result = _append_approval_record(approval_id, log_path=log_path, event="created")
    return {**result, "approval_id_sha256": _approval_ref(approval_id)}


def query_approval(approval_id: str, *, log_path: str | Path) -> dict[str, bool | str]:
    """Return whether an approval currently has an active log record.

    Returns:
        Lookup result with the hashed approval identifier.
    """
    approval_id = sanitize_untrusted_text(approval_id, max_length=512)
    approval_id_hash = _approval_ref(approval_id)
    path = _resolve_log_path(log_path)
    if not path.exists():
        return {"found": False, "approval_id_sha256": approval_id_hash}

    active = False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            record = None
        if not isinstance(record, dict):
            continue
        if record.get("approval_id_sha256") != approval_id_hash:
            continue
        event = record.get("event")
        if event in {"expired", "revoked"}:
            active = False
        elif event == "created":
            active = True
    return {"found": active, "approval_id_sha256": approval_id_hash}


def revoke_approval(approval_id: str, *, log_path: str | Path) -> dict[str, bool]:
    """Revoke an approval and append a decision-log record."""
    return _append_approval_record(approval_id, log_path=log_path, event="revoked")


def expire_approval(approval_id: str, *, log_path: str | Path) -> dict[str, bool]:
    """Expire an approval and append a decision-log record.

    Args:
        approval_id: Approval identifier.
        log_path: JSONL decision log path.

    Returns:
        Result indicating whether the expiry was logged.

    Raises:
        ValueError: If ``approval_id`` is blank.
        OSError: If the decision log cannot be read or written.
    """
    approval_id = sanitize_untrusted_text(approval_id, max_length=512)
    path = _resolve_log_path(log_path)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    _write_text_atomic(path, existing + json.dumps(_approval_record(approval_id, "expired"), sort_keys=True) + "\n")
    return {"logged": True}


__all__ = ["create_approval", "expire_approval", "query_approval", "revoke_approval"]
