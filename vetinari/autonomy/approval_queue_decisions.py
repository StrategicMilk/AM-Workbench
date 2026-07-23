"""Extracted implementation helpers for approval_queue.py."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.types import AutonomyLevel

logger = logging.getLogger(__name__)
_STATUS_PENDING = "pending"
_STATUS_APPROVED = "approved"


class ApprovalDecisionMixin:
    """Shared method implementations for the compatibility wrapper."""

    if TYPE_CHECKING:
        _callbacks: Any
        _expire_row_locked: Any
        _expire_stale: Any
        _get_connection: Any
        _is_expired: Any
        _lock: Any

    def _load_pending_decision_locked(
        self,
        conn: Any,
        action_id: str,
        now: str,
    ) -> tuple[dict[str, Any], str, Any] | None:
        """Load a pending queue row and expire it atomically when stale."""
        cursor = conn.execute(
            "SELECT action_type, details_json, confidence, created_at FROM approval_queue "
            "WHERE action_id = ? AND status = ?",
            (action_id, _STATUS_PENDING),
        )
        row = cursor.fetchone()
        if row is None:
            return None

        details = json.loads(row["details_json"])
        action_type = row["action_type"]
        confidence = row["confidence"]
        if self._is_expired(row["created_at"]):
            self._expire_row_locked(conn, action_id, action_type, details, confidence, now)
            conn.commit()
            self._callbacks.pop(action_id, None)
            return None
        return details, action_type, confidence

    @staticmethod
    def _write_decision_locked(
        conn: Any,
        *,
        action_id: str,
        status: str,
        decided_by: str,
        reason: str,
        now: str,
        details: dict[str, Any],
        action_type: str,
        confidence: Any,
    ) -> None:
        """Persist the queue status update and audit decision in one transaction."""
        conn.execute(
            "UPDATE approval_queue SET status = ?, decided_at = ?, decided_by = ? WHERE action_id = ?",
            (status, now, decided_by, action_id),
        )
        decision_str = "approve" if status == _STATUS_APPROVED else "deny"
        conn.execute(
            "INSERT INTO decision_log "
            "(action_id, action_type, autonomy_level, decision, confidence, details_json, outcome, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action_id,
                action_type,
                AutonomyLevel.L1_SUGGEST.value,  # DEFER path is always L1_SUGGEST
                decision_str,
                confidence,
                json.dumps(details),
                reason,
                now,
            ),
        )
        conn.commit()

    def _invoke_decision_callback(self, action_id: str, status: str, details: dict[str, Any]) -> None:
        """Invoke and clear the in-memory callback after the durable decision."""
        callback = self._callbacks.pop(action_id, None)
        if callback is None:
            logger.warning(
                "Action %s %s but no in-memory callback registered — "
                "expected across process restart; decision is persisted "
                "but any resumer must re-register or treat callback as unresumable",
                action_id,
                status,
            )
            return

        try:
            callback(action_id, status, details)
        except Exception:
            logger.warning(
                "Callback for action %s raised an exception — decision recorded but action may not have resumed",
                action_id,
            )

    def _decide(self, action_id: str, status: str, decided_by: str, reason: str = "") -> bool:
        """Apply a decision (approve/reject) to a pending action.

        The queue-status UPDATE and decision_log INSERT are performed in a
        single SQLite transaction so the audit trail is never disconnected from
        the actual approve/reject event.  After the transaction commits, any
        registered in-memory callback is popped and invoked.  Callback
        exceptions are caught and logged — the decision is always considered
        final regardless of whether the callback succeeds.

        If no callback is registered (e.g. after a process restart), a WARNING
        is logged so operators know the resumer must re-register or treat the
        action as unresumable without explicit recovery.
        """
        self._expire_stale()
        now = datetime.now(timezone.utc).isoformat()
        details: dict[str, Any] = {}

        with self._lock:
            conn = self._get_connection()
            try:
                pending = self._load_pending_decision_locked(conn, action_id, now)
                if pending is None:
                    return False
                details, action_type, confidence = pending
                self._write_decision_locked(
                    conn,
                    action_id=action_id,
                    status=status,
                    decided_by=decided_by,
                    reason=reason,
                    now=now,
                    details=details,
                    action_type=action_type,
                    confidence=confidence,
                )
            finally:
                conn.close()

        logger.info(
            "Action %s %s by %s (reason=%s)",
            action_id,
            status,
            decided_by,
            reason or "none",
        )
        self._invoke_decision_callback(action_id, status, details)
        return True
