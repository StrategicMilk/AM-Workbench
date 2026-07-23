"""Timeout and claim ordering helpers for agent mailbox state."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from vetinari.memory.agent_mailbox_signals import _parse_utc
from vetinari.memory.blackboard_v2 import MailboxMessage, MailboxMessageState, MailboxReceipt, MailboxReceiptOutcome


class MailboxTimeoutMixin:
    """Shared timeout transition behavior for mailbox messages."""

    if TYPE_CHECKING:
        _receipt: Any

    _messages: dict[str, MailboxMessage]
    _receipts: list[MailboxReceipt]

    def _claimable_messages_locked(self, recipient: str) -> list[MailboxMessage]:
        messages = [
            message
            for message in self._messages.values()
            if message.state is MailboxMessageState.PENDING
            and (recipient in message.recipients or "*" in message.recipients)
        ]
        messages.sort(key=lambda item: (item.created_at_utc, item.message_id))
        return messages

    def _reap_timeouts_locked(self, now: datetime) -> None:
        for message in self._messages.values():
            if message.state is MailboxMessageState.PENDING and _parse_utc(message.expires_at_utc) <= now:
                message.state = MailboxMessageState.DEAD_LETTER
                message.dead_letter_reason = "message-timeout"
                self._receipts.append(
                    self._receipt(
                        operation="reap_timeouts",
                        outcome=MailboxReceiptOutcome.DEAD_LETTERED,
                        message_id=message.message_id,
                        reasons=("message-timeout",),
                        now=now,
                    )
                )
            if message.state is MailboxMessageState.LEASED and message.lease is not None:
                if _parse_utc(message.lease.expires_at_utc) > now:
                    continue
                lease_id = message.lease.lease_id
                message.lease = None
                if message.attempts < message.max_attempts:
                    message.state = MailboxMessageState.PENDING
                    outcome = MailboxReceiptOutcome.RETRY_SCHEDULED
                    reasons = ("lease-expired-retry-scheduled",)
                else:
                    message.state = MailboxMessageState.DEAD_LETTER
                    message.dead_letter_reason = "lease-timeout"
                    outcome = MailboxReceiptOutcome.DEAD_LETTERED
                    reasons = ("lease-timeout-dead-lettered",)
                self._receipts.append(
                    self._receipt(
                        operation="reap_timeouts",
                        outcome=outcome,
                        message_id=message.message_id,
                        lease_id=lease_id,
                        reasons=reasons,
                        now=now,
                    )
                )
