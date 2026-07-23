"""Event types published by the WorkReceipt subsystem.

A separate module avoids a circular import between ``vetinari.events``
(generic event bus + canonical Event dataclasses) and
``vetinari.receipts.store`` (publisher of the receipt-specific event).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from vetinari.events import Event


@dataclass(frozen=True, slots=True)
class ReceiptAppended(Event):
    """Published when a WorkReceipt is appended to the store.

    The Control Center SSE channel subscribes to this event to push
    real-time updates of agent work to attached UI clients.

    Attributes:
        project_id: Project the receipt belongs to.
        receipt_id: Unique identifier of the appended receipt.
        kind: WorkReceiptKind value (e.g. ``"worker_task"``) as a string.
        passed: Whether the embedded OutcomeSignal verdict is positive.
        awaiting_user: Whether this receipt indicates the project is
            blocked on user input.
        timestamp: Wall-clock time when the event was created.
    """

    project_id: str = ""
    receipt_id: str = ""
    kind: str = ""
    passed: bool = False
    awaiting_user: bool = False

    def __post_init__(self) -> None:
        """Pin the event_type discriminator to the canonical topic name."""
        object.__setattr__(self, "event_type", "receipt.appended")

    def __repr__(self) -> str:
        return (
            f"ReceiptAppended(project_id={self.project_id!r}, "
            f"receipt_id={self.receipt_id!r}, kind={self.kind!r}, "
            f"passed={self.passed!r}, awaiting_user={self.awaiting_user!r})"
        )


def receipt_appended(
    *,
    project_id: str,
    receipt_id: str,
    kind: str,
    passed: bool,
    awaiting_user: bool,
) -> ReceiptAppended:
    """Construct a ReceiptAppended event with the current wall-clock timestamp.

    Args:
        project_id: Project the receipt belongs to.
        receipt_id: Unique identifier of the appended receipt.
        kind: WorkReceiptKind value as a string (use ``kind.value``).
        passed: Whether the embedded OutcomeSignal verdict is positive.
        awaiting_user: Whether this receipt indicates the project is
            blocked on user input.

    Returns:
        A populated ReceiptAppended event ready to publish.
    """
    return ReceiptAppended(
        event_type="receipt.appended",
        timestamp=time.time(),
        project_id=project_id,
        receipt_id=receipt_id,
        kind=kind,
        passed=passed,
        awaiting_user=awaiting_user,
    )


def emit_receipt_event(
    *,
    project_id: str,
    receipt_id: str,
    kind: str,
    passed: bool,
    awaiting_user: bool,
) -> ReceiptAppended:
    """Compatibility alias for constructing receipt append events.

    Args:
        project_id: Project the receipt belongs to.
        receipt_id: Unique identifier of the appended receipt.
        kind: WorkReceiptKind value as a string.
        passed: Whether the embedded OutcomeSignal verdict is positive.
        awaiting_user: Whether this receipt blocks on user input.

    Returns:
        A populated ReceiptAppended event ready to publish.
    """
    return receipt_appended(
        project_id=project_id,
        receipt_id=receipt_id,
        kind=kind,
        passed=passed,
        awaiting_user=awaiting_user,
    )


__all__ = ["ReceiptAppended", "emit_receipt_event", "receipt_appended"]
