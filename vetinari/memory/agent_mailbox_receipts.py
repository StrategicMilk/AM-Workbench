"""Receipt and permission guard helpers for agent mailbox state."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from vetinari.memory.agent_mailbox_signals import _iso
from vetinari.memory.blackboard_v2 import MailboxReceipt, MailboxReceiptOutcome, PermissionContext


class MailboxReceiptMixin:
    """Shared receipt construction and fail-closed guard behavior."""

    if TYPE_CHECKING:
        _persist_locked: Any

    _damaged_reason: str
    _receipts: list[MailboxReceipt]

    def _guard_receipt(
        self,
        operation: str,
        permission_context: PermissionContext | None,
        action: str,
        *,
        now: datetime,
    ) -> MailboxReceipt | None:
        if self._damaged_reason:
            return self._blocked_without_request(
                operation,
                permission_context,
                ("mailbox-state-unreadable",),
                ("readable_mailbox_state",),
                now=now,
            )
        if permission_context is None:
            return self._blocked_without_request(
                operation,
                None,
                ("missing-permission-context",),
                ("permission_context",),
                now=now,
            )
        missing = permission_context.missing_for(action)
        if missing:
            return self._blocked_without_request(
                operation,
                permission_context,
                ("missing-permission-context",),
                missing,
                now=now,
            )
        return None

    def _blocked_without_request(
        self,
        operation: str,
        permission_context: PermissionContext | None,
        reasons: tuple[str, ...],
        missing_signals: tuple[str, ...],
        *,
        now: datetime,
    ) -> MailboxReceipt:
        receipt = self._receipt(
            operation=operation,
            outcome=MailboxReceiptOutcome.BLOCKED,
            actor=permission_context.actor if permission_context else "",
            reasons=reasons,
            missing_signals=missing_signals,
            now=now,
        )
        self._receipts.append(receipt)
        if not self._damaged_reason:
            self._persist_locked()
        return receipt

    @staticmethod
    def _receipt(
        *,
        operation: str,
        outcome: MailboxReceiptOutcome,
        now: datetime,
        message_id: str = "",
        lease_id: str = "",
        actor: str = "",
        reasons: tuple[str, ...] = (),
        missing_signals: tuple[str, ...] = (),
    ) -> MailboxReceipt:
        return MailboxReceipt(
            receipt_id=f"mbr_{uuid.uuid4().hex}",
            operation=operation,
            outcome=outcome,
            emitted_at_utc=_iso(now),
            message_id=message_id,
            lease_id=lease_id,
            actor=actor,
            reasons=reasons,
            missing_signals=missing_signals,
        )
