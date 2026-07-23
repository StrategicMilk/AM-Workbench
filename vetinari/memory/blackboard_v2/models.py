"""Typed mailbox records for the Workbench blackboard v2 surface."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.security.fail_closed import (
    SchemaOpenError,
    UntrustedInputError,
    assert_closed_schema,
    sanitize_untrusted_text,
)
from vetinari.security.redaction import redact_text, redact_value

SCHEMA_VERSION = 1
_REFERENCE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*:[A-Za-z0-9_.@/=-]+$")


class MailboxError(ValueError):
    """Raised when a mailbox payload cannot be trusted."""


class MailboxMessageKind(str, Enum):
    """High-level mailbox message categories."""

    HELP_REQUEST = "help_request"
    DELEGATION = "delegation"
    CHALLENGE = "challenge"
    OBSERVATION = "observation"
    QUEUE_SIGNAL = "queue_signal"
    AUTOMATION_SIGNAL = "automation_signal"
    MEMORY_SIGNAL = "memory_signal"


class MailboxMessageState(str, Enum):
    """Lifecycle state for a typed mailbox message."""

    PENDING = "pending"
    LEASED = "leased"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    BLOCKED = "blocked"


class MailboxReceiptOutcome(str, Enum):
    """Receipt outcome for mailbox operations."""

    ACCEPTED = "accepted"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTERED = "dead_lettered"
    BLOCKED = "blocked"
    DEGRADED = "degraded"
    NO_MESSAGE = "no_message"


@dataclass(frozen=True, slots=True)
class PermissionContext:
    """Authority carried by a caller that wants to mutate mailbox state."""

    actor: str
    permission_ref: str
    authority_ref: str
    allowed_actions: tuple[str, ...]

    def has_action(self, action: str) -> bool:
        return action in self.allowed_actions or "mailbox.*" in self.allowed_actions

    def missing_for(self, action: str) -> tuple[str, ...]:
        """Execute the missing for operation.

        Returns:
            tuple[str, ...] value produced by missing_for().
        """
        missing: list[str] = []
        if not self.actor.strip():
            missing.append("permission_context.actor")
        if not self.permission_ref.strip():
            missing.append("permission_context.permission_ref")
        if not self.authority_ref.strip():
            missing.append("permission_context.authority_ref")
        if not self.has_action(action):
            missing.append(f"permission_context.allowed_actions:{action}")
        return tuple(missing)

    def to_payload(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "permission_ref": self.permission_ref,
            "authority_ref": self.authority_ref,
            "allowed_actions": list(self.allowed_actions),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PermissionContext(actor={self.actor!r}, permission_ref={self.permission_ref!r}, authority_ref={self.authority_ref!r})"


@dataclass(frozen=True, slots=True)
class MailboxReferences:
    """Causal, prompt, tool, memory, and receipt references for a message."""

    causal_message_ids: tuple[str, ...] = ()
    prompt_refs: tuple[str, ...] = ()
    tool_call_refs: tuple[str, ...] = ()
    memory_refs: tuple[str, ...] = ()
    receipt_refs: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "causal_message_ids": list(self.causal_message_ids),
            "prompt_refs": list(self.prompt_refs),
            "tool_call_refs": list(self.tool_call_refs),
            "memory_refs": list(self.memory_refs),
            "receipt_refs": list(self.receipt_refs),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MailboxReferences:
        """Build references from a closed mailbox payload.

        Returns:
            A validated mailbox reference set.
        """
        _closed_payload(
            payload,
            allowed_keys=("causal_message_ids", "prompt_refs", "tool_call_refs", "memory_refs", "receipt_refs"),
            field_name="references",
        )
        return cls(
            causal_message_ids=_string_tuple(payload.get("causal_message_ids", ()), "causal_message_ids"),
            prompt_refs=_reference_tuple(payload.get("prompt_refs", ()), "prompt_refs"),
            tool_call_refs=_reference_tuple(payload.get("tool_call_refs", ()), "tool_call_refs"),
            memory_refs=_reference_tuple(payload.get("memory_refs", ()), "memory_refs"),
            receipt_refs=_reference_tuple(payload.get("receipt_refs", ()), "receipt_refs"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MailboxReferences(causal_message_ids={self.causal_message_ids!r}, prompt_refs={self.prompt_refs!r}, tool_call_refs={self.tool_call_refs!r})"


@dataclass(frozen=True, slots=True)
class MailboxLease:
    """One active claim over a mailbox message."""

    lease_id: str
    owner: str
    granted_at_utc: str
    expires_at_utc: str

    def to_payload(self) -> dict[str, str]:
        return {
            "lease_id": self.lease_id,
            "owner": self.owner,
            "granted_at_utc": self.granted_at_utc,
            "expires_at_utc": self.expires_at_utc,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MailboxLease:
        """Build a lease from a closed mailbox payload.

        Returns:
            A validated mailbox lease.
        """
        _closed_payload(
            payload,
            allowed_keys=("lease_id", "owner", "granted_at_utc", "expires_at_utc"),
            required_keys=("lease_id", "owner", "granted_at_utc", "expires_at_utc"),
            field_name="lease",
        )
        return cls(
            lease_id=_required_text(payload.get("lease_id"), "lease.lease_id"),
            owner=_required_text(payload.get("owner"), "lease.owner"),
            granted_at_utc=_required_text(payload.get("granted_at_utc"), "lease.granted_at_utc"),
            expires_at_utc=_required_text(payload.get("expires_at_utc"), "lease.expires_at_utc"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MailboxLease(lease_id={self.lease_id!r}, owner={self.owner!r}, granted_at_utc={self.granted_at_utc!r})"


@dataclass(slots=True)
class MailboxMessage:
    """One durable mailbox message."""

    message_id: str
    sender: str
    recipients: tuple[str, ...]
    kind: MailboxMessageKind
    subject: str
    content: str
    created_at_utc: str
    expires_at_utc: str
    max_attempts: int
    state: MailboxMessageState = MailboxMessageState.PENDING
    attempts: int = 0
    lease: MailboxLease | None = None
    references: MailboxReferences = field(default_factory=MailboxReferences)
    provenance: dict[str, str] = field(default_factory=dict)
    safety_ref: str = ""
    confidence: float = 0.0
    last_error: str = ""
    dead_letter_reason: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "sender": self.sender,
            "recipients": list(self.recipients),
            "kind": self.kind.value,
            "subject": self.subject,
            "content": redact_text(self.content),
            "created_at_utc": self.created_at_utc,
            "expires_at_utc": self.expires_at_utc,
            "max_attempts": self.max_attempts,
            "state": self.state.value,
            "attempts": self.attempts,
            "lease": self.lease.to_payload() if self.lease else None,
            "references": self.references.to_payload(),
            "provenance": redact_value(dict(self.provenance)),
            "safety_ref": self.safety_ref,
            "confidence": self.confidence,
            "last_error": redact_text(self.last_error),
            "dead_letter_reason": redact_text(self.dead_letter_reason),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MailboxMessage:
        """Execute the from payload operation.

        Returns:
            MailboxMessage value produced by from_payload().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(payload, dict):
            raise MailboxError("message payload must be an object")
        _closed_payload(
            payload,
            allowed_keys=(
                "message_id",
                "sender",
                "recipients",
                "kind",
                "subject",
                "content",
                "created_at_utc",
                "expires_at_utc",
                "max_attempts",
                "state",
                "attempts",
                "lease",
                "references",
                "provenance",
                "safety_ref",
                "confidence",
                "last_error",
                "dead_letter_reason",
            ),
            required_keys=(
                "message_id",
                "sender",
                "recipients",
                "kind",
                "subject",
                "content",
                "created_at_utc",
                "expires_at_utc",
                "state",
            ),
            field_name="message",
        )
        lease_payload = payload.get("lease")
        return cls(
            message_id=_required_text(payload.get("message_id"), "message_id"),
            sender=_required_text(payload.get("sender"), "sender"),
            recipients=_non_empty_string_tuple(payload.get("recipients"), "recipients"),
            kind=MailboxMessageKind(_required_text(payload.get("kind"), "kind")),
            subject=_required_text(payload.get("subject"), "subject"),
            content=_required_text(payload.get("content"), "content"),
            created_at_utc=_required_text(payload.get("created_at_utc"), "created_at_utc"),
            expires_at_utc=_required_text(payload.get("expires_at_utc"), "expires_at_utc"),
            max_attempts=int(payload.get("max_attempts", 1)),
            state=MailboxMessageState(_required_text(payload.get("state"), "state")),
            attempts=int(payload.get("attempts", 0)),
            lease=MailboxLease.from_payload(lease_payload) if isinstance(lease_payload, dict) else None,
            references=MailboxReferences.from_payload(payload.get("references") or {}),
            provenance=_string_mapping(payload.get("provenance") or {}, "provenance"),
            safety_ref=str(payload.get("safety_ref", "")),
            confidence=float(payload.get("confidence", 0.0)),
            last_error=str(payload.get("last_error", "")),
            dead_letter_reason=str(payload.get("dead_letter_reason", "")),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MailboxMessage(message_id={self.message_id!r}, sender={self.sender!r}, recipients={self.recipients!r})"


@dataclass(frozen=True, slots=True)
class MailboxReceipt:
    """Operation receipt emitted by every mailbox mutation attempt."""

    receipt_id: str
    operation: str
    outcome: MailboxReceiptOutcome
    emitted_at_utc: str
    message_id: str = ""
    lease_id: str = ""
    actor: str = ""
    reasons: tuple[str, ...] = ()
    missing_signals: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.outcome in {
            MailboxReceiptOutcome.ACCEPTED,
            MailboxReceiptOutcome.CLAIMED,
            MailboxReceiptOutcome.COMPLETED,
            MailboxReceiptOutcome.RETRY_SCHEDULED,
            MailboxReceiptOutcome.DEAD_LETTERED,
            MailboxReceiptOutcome.NO_MESSAGE,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "operation": self.operation,
            "outcome": self.outcome.value,
            "emitted_at_utc": self.emitted_at_utc,
            "message_id": self.message_id,
            "lease_id": self.lease_id,
            "actor": self.actor,
            "reasons": list(self.reasons),
            "missing_signals": list(self.missing_signals),
            "passed": self.passed,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MailboxReceipt:
        """Build a receipt from a closed mailbox payload.

        Returns:
            A validated mailbox receipt.
        """
        _closed_payload(
            payload,
            allowed_keys=(
                "receipt_id",
                "operation",
                "outcome",
                "emitted_at_utc",
                "message_id",
                "lease_id",
                "actor",
                "reasons",
                "missing_signals",
                "passed",
            ),
            required_keys=("receipt_id", "operation", "outcome", "emitted_at_utc"),
            field_name="receipt",
        )
        return cls(
            receipt_id=_required_text(payload.get("receipt_id"), "receipt_id"),
            operation=_required_text(payload.get("operation"), "operation"),
            outcome=MailboxReceiptOutcome(_required_text(payload.get("outcome"), "outcome")),
            emitted_at_utc=_required_text(payload.get("emitted_at_utc"), "emitted_at_utc"),
            message_id=str(payload.get("message_id", "")),
            lease_id=str(payload.get("lease_id", "")),
            actor=str(payload.get("actor", "")),
            reasons=_string_tuple(payload.get("reasons", ()), "reasons"),
            missing_signals=_string_tuple(payload.get("missing_signals", ()), "missing_signals"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MailboxReceipt(receipt_id={self.receipt_id!r}, operation={self.operation!r}, outcome={self.outcome!r})"


@dataclass(frozen=True, slots=True)
class MailboxSubmitRequest:
    """Validated input envelope for publishing a mailbox message."""

    sender: str
    recipients: tuple[str, ...]
    kind: MailboxMessageKind
    subject: str
    content: str
    permission_context: PermissionContext | None
    provenance: dict[str, str]
    receipt_authority: str
    safety_ref: str
    confidence: float | None
    timeout_seconds: float
    references: MailboxReferences = field(default_factory=MailboxReferences)
    max_attempts: int = 3

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MailboxSubmitRequest(sender={self.sender!r}, recipients={self.recipients!r}, kind={self.kind!r})"


@dataclass(frozen=True, slots=True)
class MailboxClaim:
    """Result of attempting to claim the next message."""

    message: MailboxMessage | None
    receipt: MailboxReceipt


def _required_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MailboxError(f"{field_name} must be a non-empty string")
    try:
        return sanitize_untrusted_text(value, max_length=20_000)
    except UntrustedInputError as exc:
        raise MailboxError(f"{field_name} is unsafe: {exc}") from exc


def _string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, (list, tuple)):
        raise MailboxError(f"{field_name} must be a list or tuple")
    result = tuple(_required_text(item, field_name) for item in value)
    if any(not item.strip() for item in result):
        raise MailboxError(f"{field_name} cannot contain blank strings")
    return result


def _reference_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MailboxError(f"{field_name} must be a non-empty string")
    text = value.strip()
    try:
        return sanitize_untrusted_text(text, max_length=2_000)
    except UntrustedInputError as exc:
        if str(exc) == "untrusted text contains prompt-control markers" and _REFERENCE_ID_RE.fullmatch(text):
            return text
        raise MailboxError(f"{field_name} is unsafe: {exc}") from exc


def _reference_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, (list, tuple)):
        raise MailboxError(f"{field_name} must be a list or tuple")
    result = tuple(_reference_text(item, field_name) for item in value)
    if any(not item.strip() for item in result):
        raise MailboxError(f"{field_name} cannot contain blank strings")
    return result


def _non_empty_string_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    result = _string_tuple(value, field_name)
    if not result:
        raise MailboxError(f"{field_name} must contain at least one value")
    return result


def _string_mapping(value: Any, field_name: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise MailboxError(f"{field_name} must be an object")
    result = {_required_text(key, field_name): _required_text(item, field_name) for key, item in value.items()}
    if any(not key.strip() or not item.strip() for key, item in result.items()):
        raise MailboxError(f"{field_name} cannot contain blank keys or values")
    return result


def _closed_payload(
    payload: dict[str, Any],
    *,
    allowed_keys: tuple[str, ...],
    required_keys: tuple[str, ...] = (),
    field_name: str,
) -> None:
    try:
        assert_closed_schema(payload, allowed_keys=allowed_keys, required_keys=required_keys)
    except SchemaOpenError as exc:
        raise MailboxError(f"{field_name} payload is not closed: {exc}") from exc
