"""Retention receipt helpers for telemetry persistence."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from vetinari.utils import privacy_receipt


def _build_retention_receipt(
    *,
    now: float,
    cutoff: float,
    retention_days: int,
    owner_ref: str,
    dry_run: bool,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the audited telemetry retention receipt payload."""
    return {
        "receipt_id": f"telemetry-prune:{datetime.now(timezone.utc).isoformat()}-{uuid.uuid4().hex[:8]}",
        "created_at": now,
        "cutoff": cutoff,
        "retention_days": retention_days,
        "owner_ref": owner_ref,
        "dry_run": dry_run,
        "pruned_count": len(candidates),
        "candidate_rows": candidates,
        "privacy_receipt": privacy_receipt(
            privacy_class="operational",
            retention_days=retention_days,
            source="telemetry_retention.prune_receipt",
            redaction_applied=True,
        ),
        "restore_contract": "restore from operator backup; retention receipts intentionally exclude deleted payload bodies",
    }
