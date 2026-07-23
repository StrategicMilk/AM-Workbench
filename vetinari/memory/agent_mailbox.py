"""Typed agent mailbox backed by blackboard-v2 state.

The mailbox is intentionally import-safe: it does not create a singleton,
start workers, or register callbacks. Callers construct ``AgentMailbox`` with
an optional snapshot path and every mutation returns a typed receipt.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from vetinari.memory.agent_mailbox_persistence import MailboxPersistenceMixin
from vetinari.memory.agent_mailbox_receipts import MailboxReceiptMixin
from vetinari.memory.agent_mailbox_signals import (
    _coerce_utc,
    _iso,
    _missing_completion_signals,
    _missing_submit_signals,
    _parse_utc,
)
from vetinari.memory.agent_mailbox_timeouts import MailboxTimeoutMixin
from vetinari.memory.blackboard_v2 import (
    MailboxClaim,
    MailboxLease,
    MailboxMessage,
    MailboxMessageKind,
    MailboxMessageState,
    MailboxReceipt,
    MailboxReceiptOutcome,
    MailboxReferences,
    MailboxSubmitRequest,
    PermissionContext,
)

WorkbenchMailboxChannel = Literal["agent_queue", "automation", "memory_spine"]
WORKBENCH_MAILBOX_CHANNELS: tuple[WorkbenchMailboxChannel, ...] = (
    "agent_queue",
    "automation",
    "memory_spine",
)

_KIND_BY_CHANNEL: dict[WorkbenchMailboxChannel, MailboxMessageKind] = {
    "agent_queue": MailboxMessageKind.QUEUE_SIGNAL,
    "automation": MailboxMessageKind.AUTOMATION_SIGNAL,
    "memory_spine": MailboxMessageKind.MEMORY_SIGNAL,
}


@dataclass(frozen=True, slots=True)
class MailboxHealth:
    """Fail-closed health summary for the persisted mailbox snapshot."""

    status: Literal["ok", "degraded"]
    reason: str = ""

    @property
    def degraded(self) -> bool:
        return self.status == "degraded"


class AgentMailbox(MailboxPersistenceMixin, MailboxReceiptMixin, MailboxTimeoutMixin):
    """Thread-safe typed mailbox with leases, retries, and dead letters."""

    def __init__(self, storage_path: str | Path | None = None, *, auto_restore: bool = True) -> None:
        self._storage_path = Path(storage_path) if storage_path is not None else None
        self._lock = threading.RLock()
        self._messages: dict[str, MailboxMessage] = {}
        self._receipts: list[MailboxReceipt] = []
        self._damaged_reason = ""
        if auto_restore and self._storage_path is not None and self._storage_path.exists():
            self._restore()

    def health(self) -> MailboxHealth:
        """Execute the health operation.

        Returns:
            MailboxHealth value produced by health().
        """
        with self._lock:
            if self._damaged_reason:
                return MailboxHealth(status="degraded", reason=self._damaged_reason)
            return MailboxHealth(status="ok")

    def list_messages(self) -> tuple[MailboxMessage, ...]:
        """Execute the list messages operation.

        Returns:
            Collection of messages values.
        """
        with self._lock:
            return tuple(self._messages.values())

    def list_receipts(self) -> tuple[MailboxReceipt, ...]:
        """Execute the list receipts operation.

        Returns:
            Collection of receipts values.
        """
        with self._lock:
            return tuple(self._receipts)

    def get_message(self, message_id: str) -> MailboxMessage | None:
        """Execute the get message operation.

        Returns:
            Resolved message value.
        """
        with self._lock:
            return self._messages.get(message_id)

    def submit(self, request: MailboxSubmitRequest, *, now: datetime | None = None) -> MailboxReceipt:
        """Publish a message or return a blocked receipt when signals are missing.

        Returns:
            MailboxReceipt value produced by submit().
        """
        now = _coerce_utc(now)
        with self._lock:
            guard = self._guard_receipt("submit", request.permission_context, "mailbox.submit", now=now)
            if guard is not None:
                return guard
            missing = _missing_submit_signals(request)
            if missing:
                receipt = self._receipt(
                    operation="submit",
                    outcome=MailboxReceiptOutcome.BLOCKED,
                    actor=request.permission_context.actor if request.permission_context else "",
                    reasons=("missing-required-mailbox-signals",),
                    missing_signals=missing,
                    now=now,
                )
                self._receipts.append(receipt)
                self._persist_locked()
                return receipt

            message_id = f"mb_{uuid.uuid4().hex}"
            expires_at = now + timedelta(seconds=request.timeout_seconds)
            message = MailboxMessage(
                message_id=message_id,
                sender=request.sender,
                recipients=request.recipients,
                kind=request.kind,
                subject=request.subject,
                content=request.content,
                created_at_utc=_iso(now),
                expires_at_utc=_iso(expires_at),
                max_attempts=max(1, int(request.max_attempts)),
                references=request.references,
                provenance=dict(request.provenance),
                safety_ref=request.safety_ref,
                confidence=float(request.confidence),
            )
            self._messages[message_id] = message
            receipt = self._receipt(
                operation="submit",
                outcome=MailboxReceiptOutcome.ACCEPTED,
                message_id=message_id,
                actor=request.permission_context.actor,
                reasons=("message-persisted",),
                now=now,
            )
            self._receipts.append(receipt)
            self._persist_locked()
            return receipt

    def publish_workbench_signal(
        self,
        *,
        channel: WorkbenchMailboxChannel,
        source_id: str,
        sender: str,
        recipients: tuple[str, ...],
        summary: str,
        permission_context: PermissionContext | None,
        provenance: dict[str, str],
        receipt_authority: str,
        safety_ref: str,
        confidence: float | None,
        timeout_seconds: float,
        prompt_refs: tuple[str, ...] = (),
        tool_call_refs: tuple[str, ...] = (),
        memory_refs: tuple[str, ...] = (),
        receipt_refs: tuple[str, ...] = (),
        causal_message_ids: tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> MailboxReceipt:
        """Documented owned-scope entry point for Workbench consumers.

                Existing agent queue, automation, and memory-spine packs can call this
                without this pack editing their files. Missing source identity still
                fails closed through the same receipt path as direct submissions.

        Returns:
            MailboxReceipt value produced by publish_workbench_signal().
        """
        references = MailboxReferences(
            causal_message_ids=causal_message_ids,
            prompt_refs=prompt_refs,
            tool_call_refs=tool_call_refs,
            memory_refs=memory_refs,
            receipt_refs=receipt_refs,
        )
        request = MailboxSubmitRequest(
            sender=sender,
            recipients=recipients,
            kind=_KIND_BY_CHANNEL[channel],
            subject=f"{channel}:{source_id}",
            content=summary,
            permission_context=permission_context,
            provenance={**provenance, "workbench_channel": channel, "source_id": source_id},
            receipt_authority=receipt_authority,
            safety_ref=safety_ref,
            confidence=confidence,
            timeout_seconds=timeout_seconds,
            references=references,
        )
        if not source_id.strip():
            return self._blocked_without_request(
                "submit",
                permission_context,
                ("missing-required-mailbox-signals",),
                ("source_id",),
                now=_coerce_utc(now),
            )
        return self.submit(request, now=now)

    def claim_next(
        self,
        *,
        recipient: str,
        lease_owner: str,
        permission_context: PermissionContext | None,
        lease_seconds: float,
        now: datetime | None = None,
    ) -> MailboxClaim:
        """Atomically claim the oldest pending message for a recipient.

        Returns:
            MailboxClaim value produced by claim_next().
        """
        now = _coerce_utc(now)
        with self._lock:
            guard = self._guard_receipt("claim", permission_context, "mailbox.claim", now=now)
            if guard is not None:
                return MailboxClaim(message=None, receipt=guard)
            missing: list[str] = []
            if not recipient.strip():
                missing.append("recipient")
            if not lease_owner.strip():
                missing.append("lease_owner")
            if lease_seconds <= 0:
                missing.append("lease_seconds")
            if missing:
                receipt = self._receipt(
                    operation="claim",
                    outcome=MailboxReceiptOutcome.BLOCKED,
                    actor=permission_context.actor if permission_context else "",
                    reasons=("missing-required-claim-signals",),
                    missing_signals=tuple(missing),
                    now=now,
                )
                self._receipts.append(receipt)
                self._persist_locked()
                return MailboxClaim(message=None, receipt=receipt)

            self._reap_timeouts_locked(now)
            for message in self._claimable_messages_locked(recipient):
                lease = MailboxLease(
                    lease_id=f"lease_{uuid.uuid4().hex}",
                    owner=lease_owner,
                    granted_at_utc=_iso(now),
                    expires_at_utc=_iso(now + timedelta(seconds=lease_seconds)),
                )
                message.state = MailboxMessageState.LEASED
                message.lease = lease
                message.attempts += 1
                receipt = self._receipt(
                    operation="claim",
                    outcome=MailboxReceiptOutcome.CLAIMED,
                    message_id=message.message_id,
                    lease_id=lease.lease_id,
                    actor=permission_context.actor,
                    reasons=("lease-granted",),
                    now=now,
                )
                self._receipts.append(receipt)
                self._persist_locked()
                return MailboxClaim(message=message, receipt=receipt)

            receipt = self._receipt(
                operation="claim",
                outcome=MailboxReceiptOutcome.NO_MESSAGE,
                actor=permission_context.actor,
                reasons=("no-claimable-message",),
                now=now,
            )
            self._receipts.append(receipt)
            self._persist_locked()
            return MailboxClaim(message=None, receipt=receipt)

    def complete(
        self,
        message_id: str,
        lease_id: str,
        *,
        result_summary: str,
        permission_context: PermissionContext | None,
        receipt_authority: str,
        provenance: dict[str, str],
        now: datetime | None = None,
    ) -> MailboxReceipt:
        """Execute the complete operation.

        Args:
            message_id: Message id value consumed by complete().
            lease_id: Lease id value consumed by complete().
            result_summary: Result summary value consumed by complete().
            permission_context: Permission context value consumed by complete().
            receipt_authority: Receipt authority value consumed by complete().
            provenance: Provenance value consumed by complete().
            now: Now value consumed by complete().

        Returns:
            MailboxReceipt value produced by complete().
        """
        now = _coerce_utc(now)
        with self._lock:
            guard = self._guard_receipt("complete", permission_context, "mailbox.complete", now=now)
            if guard is not None:
                return guard
            missing = _missing_completion_signals(result_summary, receipt_authority, provenance)
            message = self._messages.get(message_id)
            if message is None:
                missing = (*missing, "message_id")
            elif message.lease is None or message.lease.lease_id != lease_id:
                missing = (*missing, "active_lease")
            elif _parse_utc(message.lease.expires_at_utc) <= now:
                missing = (*missing, "unexpired_lease")
            if missing:
                receipt = self._receipt(
                    operation="complete",
                    outcome=MailboxReceiptOutcome.BLOCKED,
                    message_id=message_id,
                    lease_id=lease_id,
                    actor=permission_context.actor if permission_context else "",
                    reasons=("missing-or-invalid-completion-signals",),
                    missing_signals=missing,
                    now=now,
                )
                self._receipts.append(receipt)
                self._persist_locked()
                return receipt

            message.state = MailboxMessageState.COMPLETED
            message.lease = None
            message.last_error = ""
            receipt = self._receipt(
                operation="complete",
                outcome=MailboxReceiptOutcome.COMPLETED,
                message_id=message_id,
                lease_id=lease_id,
                actor=permission_context.actor,
                reasons=(result_summary,),
                now=now,
            )
            self._receipts.append(receipt)
            self._persist_locked()
            return receipt

    def fail(
        self,
        message_id: str,
        lease_id: str,
        *,
        error: str,
        retryable: bool,
        permission_context: PermissionContext | None,
        now: datetime | None = None,
    ) -> MailboxReceipt:
        """Execute the fail operation.

        Args:
            message_id: Message id value consumed by fail().
            lease_id: Lease id value consumed by fail().
            error: Error value consumed by fail().
            retryable: Retryable value consumed by fail().
            permission_context: Permission context value consumed by fail().
            now: Now value consumed by fail().

        Returns:
            MailboxReceipt value produced by fail().
        """
        now = _coerce_utc(now)
        with self._lock:
            guard = self._guard_receipt("fail", permission_context, "mailbox.fail", now=now)
            if guard is not None:
                return guard
            message = self._messages.get(message_id)
            missing: list[str] = []
            if message is None:
                missing.append("message_id")
            elif message.lease is None or message.lease.lease_id != lease_id:
                missing.append("active_lease")
            if not error.strip():
                missing.append("error")
            if missing:
                receipt = self._receipt(
                    operation="fail",
                    outcome=MailboxReceiptOutcome.BLOCKED,
                    message_id=message_id,
                    lease_id=lease_id,
                    actor=permission_context.actor if permission_context else "",
                    reasons=("missing-or-invalid-failure-signals",),
                    missing_signals=tuple(missing),
                    now=now,
                )
                self._receipts.append(receipt)
                self._persist_locked()
                return receipt

            message.last_error = error
            message.lease = None
            if retryable and message.attempts < message.max_attempts:
                message.state = MailboxMessageState.PENDING
                outcome = MailboxReceiptOutcome.RETRY_SCHEDULED
                reasons = ("retry-scheduled",)
            else:
                message.state = MailboxMessageState.DEAD_LETTER
                message.dead_letter_reason = error
                outcome = MailboxReceiptOutcome.DEAD_LETTERED
                reasons = ("dead-lettered",)
            receipt = self._receipt(
                operation="fail",
                outcome=outcome,
                message_id=message_id,
                lease_id=lease_id,
                actor=permission_context.actor,
                reasons=reasons,
                now=now,
            )
            self._receipts.append(receipt)
            self._persist_locked()
            return receipt

    def reap_timeouts(self, *, now: datetime | None = None) -> tuple[MailboxReceipt, ...]:
        """Replay timeout transitions for leased and pending messages.

        Returns:
            tuple[MailboxReceipt, ...] value produced by reap_timeouts().
        """
        now = _coerce_utc(now)
        with self._lock:
            if self._damaged_reason:
                receipt = self._receipt(
                    operation="reap_timeouts",
                    outcome=MailboxReceiptOutcome.BLOCKED,
                    reasons=("mailbox-state-unreadable",),
                    missing_signals=("readable_mailbox_state",),
                    now=now,
                )
                self._receipts.append(receipt)
                return (receipt,)
            before = len(self._receipts)
            self._reap_timeouts_locked(now)
            self._persist_locked()
            return tuple(self._receipts[before:])


__all__ = [
    "WORKBENCH_MAILBOX_CHANNELS",
    "AgentMailbox",
    "MailboxHealth",
    "WorkbenchMailboxChannel",
]
