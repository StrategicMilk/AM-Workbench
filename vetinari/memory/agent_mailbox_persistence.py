"""Persistence and schema serialization helpers for agent mailbox state."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.memory.agent_mailbox_signals import _iso
from vetinari.memory.blackboard_v2 import (
    SCHEMA_VERSION,
    MailboxError,
    MailboxMessage,
    MailboxReceipt,
    MailboxReceiptOutcome,
)

logger = logging.getLogger(__name__)


class MailboxPersistenceMixin:
    """Shared snapshot restore, persistence, and schema payload behavior."""

    if TYPE_CHECKING:
        _lock: Any
        health: Any

    _storage_path: Path | None
    _messages: dict[str, MailboxMessage]
    _receipts: list[MailboxReceipt]
    _damaged_reason: str

    def to_schema_payload(self) -> dict[str, Any]:
        """Return the schema-compatible persisted mailbox payload.

        Returns:
            Value produced for the caller.
        """
        with self._lock:
            return {
                "schema_version": SCHEMA_VERSION,
                "health": self.health().status,
                "messages": [message.to_payload() for message in self._messages.values()],
                "receipts": [receipt.to_payload() for receipt in self._receipts],
            }

    def _restore(self) -> None:
        assert self._storage_path is not None
        try:
            raw = json.loads(self._storage_path.read_text(encoding="utf-8"))
            if raw.get("schema_version") != SCHEMA_VERSION:
                raise MailboxError("schema_version must be 1")
            messages: dict[str, MailboxMessage] = {}
            for payload in raw.get("messages", []):
                message = MailboxMessage.from_payload(payload)
                messages[message.message_id] = message
            receipts = [MailboxReceipt.from_payload(payload) for payload in raw.get("receipts", [])]
        except Exception as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            self._damaged_reason = f"mailbox-state-unreadable:{type(exc).__name__}"
            self._receipts.append(
                MailboxReceipt(
                    receipt_id=f"mbr_{uuid.uuid4().hex}",
                    operation="restore",
                    outcome=MailboxReceiptOutcome.BLOCKED,
                    emitted_at_utc=_iso(datetime.now(UTC)),
                    reasons=("mailbox-state-unreadable",),
                    missing_signals=("readable_mailbox_state",),
                )
            )
            return
        with self._lock:
            self._messages = messages
            self._receipts = receipts

    def _persist_locked(self) -> None:
        if self._storage_path is None:
            return
        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_schema_payload()
        tmp_path = self._storage_path.with_name(f".{self._storage_path.name}.{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self._storage_path)
