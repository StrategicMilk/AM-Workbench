"""WorkReceipt subsystem.

Durable, append-only records of agent work. One receipt per discrete unit
(planning round, worker task, inspector pass, training step, release step)
embeds the OutcomeSignal that backs the verdict so downstream consumers
(Control Center UI, Attention track, audit) can answer "what did this
agent actually do, did it succeed, and is the project waiting on the user?"
in one record.

This is part of the post-execution observability surface: agents produce
receipts at completion; the Control Center reads receipts back out via the
HTTP API and SSE channel.
"""

from __future__ import annotations

from vetinari.receipts.emit import (
    record_agent_completion,
    record_release_step,
    record_training_step,
    record_workbench_event,
)
from vetinari.receipts.events import ReceiptAppended
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.receipts.store import WorkReceiptStore

__all__ = [
    "ReceiptAppended",
    "WorkReceipt",
    "WorkReceiptKind",
    "WorkReceiptStore",
    "record_agent_completion",
    "record_release_step",
    "record_training_step",
    "record_workbench_event",
]
