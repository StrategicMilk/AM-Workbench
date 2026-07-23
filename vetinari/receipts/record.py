"""WorkReceipt record and WorkReceiptKind enum.

A WorkReceipt is the durable answer to "what did this agent actually do
and did it succeed?" One receipt is appended per discrete unit of work
(planning round, worker task, inspector pass, training step, release
step). The embedded ``OutcomeSignal`` carries the evidence basis so the
Control Center and audit tools can distinguish tool-evidence-backed
verdicts from LLM-judgment ones.

The receipt is the unit the Attention track on the Control Center reads
to surface projects blocked on user input, with a structured
``awaiting_reason`` set by Foreman or Inspector at the time the block
was detected.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from vetinari.agents.contracts import OutcomeSignal
from vetinari.types import AgentType

_INPUTS_SUMMARY_MAX_LEN = 200
_OUTPUTS_SUMMARY_MAX_LEN = 200


class WorkReceiptKind(str, Enum):
    """Discrete kinds of work that emit a receipt.

    PLAN_ROUND fires once per Foreman planning round; WORKER_TASK fires
    once per Worker task completion; INSPECTOR_PASS fires once per
    Inspector verification cascade; TRAINING_STEP fires per checkpoint
    or epoch in the training runner; RELEASE_STEP fires per release
    pipeline stage that produces a verifiable artifact. PLAN_RUNTIME_EDIT
    records live plan diffs applied between execution layers.
    """

    PLAN_ROUND = "plan_round"
    WORKER_TASK = "worker_task"
    INSPECTOR_PASS = "inspector_pass"
    TRAINING_STEP = "training_step"
    RELEASE_STEP = "release_step"
    DESTRUCTIVE_OP = "destructive_op"  # Lifecycle-guarded destructive operations (recycle/purge/delete)
    SPINE_EVENT = "spine_event"  # Workbench metadata-spine append events
    PLAN_RUNTIME_EDIT = "plan_runtime_edit"
    TRACE_EXPORT = "trace_export"  # OTel/OpenInference trace export emissions (success or typed failure)
    DURABLE_STEP = "durable_step"  # Workbench durable workflow step boundary events
    POLICY_DECISION = "policy_decision"  # Workbench gateway-policy decision records
    WORKBENCH_EVENT = "workbench_event"  # General Workbench subsystem receipts (ADR-0103)


def _new_receipt_id() -> str:
    """Generate a new unique receipt identifier."""
    return uuid.uuid4().hex


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class WorkReceipt:
    """One durable record of an agent's work, with verdict and provenance.

    Receipts are immutable: once appended to the store they are never
    rewritten. Mutations to project state happen via new receipts that
    reference earlier ones by ``receipt_id``.

    Attributes:
        receipt_id: Unique identifier for this receipt (defaults to a
            fresh UUID hex string).
        project_id: Identifier of the project this receipt belongs to.
        agent_id: Concrete agent instance identifier (not the agent type).
        agent_type: The actor role. For factory-pipeline work this is
            FOREMAN / WORKER / INSPECTOR (ADR-0061); for auxiliary
            runners outside the factory pipeline (training, release)
            this is TRAINING / RELEASE (ADR-0103). Use the AgentType
            enum, never a raw string.
        kind: Discrete kind of work this receipt records.
        started_at_utc: ISO-8601 UTC timestamp the work began.
        finished_at_utc: ISO-8601 UTC timestamp the work finished.
        inputs_summary: Plain-English summary of the inputs (<=200 chars).
        outputs_summary: Plain-English summary of the outputs (<=200 chars).
        outcome: The OutcomeSignal that backs the verdict. Carries the
            evidence basis so consumers can decide whether to trust the
            verdict for promotion or release purposes.
        awaiting_user: True iff this receipt represents a project blocked
            on user input. Setting this to True requires a non-empty
            ``awaiting_reason`` (enforced in ``__post_init__``).
        awaiting_reason: Plain-English reason the project is awaiting the
            user (e.g. "plan reviewer refused -- TS-14 non-goals matched").
            Empty string when ``awaiting_user`` is False.
        linked_claim_ids: Identifiers of ClaimsLedger records this
            receipt cites. Empty when the kind has no claim linkage.

    Raises:
        ValueError: At construction time when ``awaiting_user`` is True
            and ``awaiting_reason`` is empty or whitespace, or when an
            inputs/outputs summary exceeds the 200-character limit.
    """

    project_id: str
    agent_id: str
    agent_type: AgentType
    kind: WorkReceiptKind
    outcome: OutcomeSignal
    receipt_id: str = field(default_factory=_new_receipt_id)
    started_at_utc: str = field(default_factory=_utc_now_iso)
    finished_at_utc: str = field(default_factory=_utc_now_iso)
    inputs_summary: str = ""
    outputs_summary: str = ""
    awaiting_user: bool = False
    awaiting_reason: str | None = None
    linked_claim_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """Enforce the awaiting_user/awaiting_reason invariant.

        Raises:
            ValueError: If awaiting_user is True without a non-empty
                awaiting_reason, or if a summary exceeds its length cap.
        """
        if self.awaiting_user and not (self.awaiting_reason and self.awaiting_reason.strip()):
            raise ValueError(
                "awaiting_user=True requires a non-empty awaiting_reason — "
                "the Attention track must show a structured reason, not a blank tile.",
            )
        if len(self.inputs_summary) > _INPUTS_SUMMARY_MAX_LEN:
            raise ValueError(
                f"inputs_summary exceeds {_INPUTS_SUMMARY_MAX_LEN} chars "
                f"(got {len(self.inputs_summary)}) — trim before constructing the receipt.",
            )
        if len(self.outputs_summary) > _OUTPUTS_SUMMARY_MAX_LEN:
            raise ValueError(
                f"outputs_summary exceeds {_OUTPUTS_SUMMARY_MAX_LEN} chars "
                f"(got {len(self.outputs_summary)}) — trim before constructing the receipt.",
            )

    def __repr__(self) -> str:
        return (
            f"WorkReceipt(receipt_id={self.receipt_id!r}, "
            f"kind={self.kind.value!r}, "
            f"passed={self.outcome.passed!r}, "
            f"awaiting_user={self.awaiting_user!r})"
        )


__all__ = ["WorkReceipt", "WorkReceiptKind"]
