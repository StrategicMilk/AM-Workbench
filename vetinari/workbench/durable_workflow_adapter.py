"""Durable workflow adapter boundary for Workbench orchestration.

Pipeline role: maps Vetinari workflow steps onto optional durable backends
without letting those backends become the system of record. Imports are
side-effect free: no files, sockets, databases, or receipt stores are opened
at module import. The in-process adapter keeps only memory-local history.
Engine-side registration and receipt writes live in durable_execution.py.
"""

from __future__ import annotations

import enum
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from vetinari.types import AgentType

_DURABLE_RECEIPT_ACTOR = AgentType.WORKBENCH
_ADAPTER_REGISTRY_LOCK = threading.Lock()
_DEFAULT_INPROCESS_ADAPTER: InProcessWorkflowAdapter | None = None


class WorkflowStepKind(enum.Enum):
    """Workflow step categories understood by durable adapters."""

    AGENT = "agent"
    TOOL = "tool"
    MODEL = "model"
    SIDE_EFFECT = "side_effect"


class WorkflowAdapterError(Exception):
    """Base exception for durable workflow adapter failures."""

    def __init__(self, reason: str, step_id: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.step_id = step_id


class WorkflowAdapterBackendDown(WorkflowAdapterError):
    """Raised when a registered backend is unavailable."""


class WorkflowAdapterReplayMismatch(WorkflowAdapterError):
    """Raised when backend replay disagrees with authoritative receipts."""


class WorkflowAdapterPolicyDenied(WorkflowAdapterError):
    """Raised when policy denies a side-effecting retry."""


@dataclass(frozen=True, slots=True)
class WorkflowStep:
    """One adapter-visible workflow step boundary."""

    step_id: str
    kind: WorkflowStepKind
    plan_id: str
    task_id: str
    payload_hash: str
    parent_plan_id: str | None = None

    def __repr__(self) -> str:
        return (
            f"WorkflowStep(step_id={self.step_id!r}, kind={self.kind.value!r}, "
            f"plan_id={self.plan_id!r}, task_id={self.task_id!r})"
        )


@dataclass(frozen=True, slots=True)
class WorkflowStepResult:
    """Result returned by a durable workflow adapter."""

    step_id: str
    success: bool
    output: dict[str, Any]
    error: str | None
    retry_attempt: int
    latency_ms: int

    def __repr__(self) -> str:
        return (
            f"WorkflowStepResult(step_id={self.step_id!r}, success={self.success!r}, "
            f"retry_attempt={self.retry_attempt!r})"
        )


@dataclass(frozen=True, slots=True)
class WorkflowReceipt:
    """Minimal replay receipt used to compare adapter history to Vetinari history."""

    step_id: str
    plan_id: str
    parent_plan_id: str | None
    kind_value: str
    occurred_at_utc: str
    payload_hash: str
    outcome_hash: str

    def __repr__(self) -> str:
        return f"WorkflowReceipt(step_id={self.step_id!r}, plan_id={self.plan_id!r}, kind_value={self.kind_value!r})"


@runtime_checkable
class DurableWorkflowAdapter(Protocol):
    """Protocol implemented by in-process and external durable backends."""

    def run_step(self, step: WorkflowStep) -> WorkflowStepResult:
        """Run one step and return its result."""
        ...

    def replay(self, plan_id: str) -> Sequence[WorkflowStep]:
        """Return the adapter's recorded step sequence for a plan."""
        ...

    def health(self) -> bool:
        """Return whether the adapter is currently healthy enough for dispatch."""
        ...


class InProcessWorkflowAdapter:
    """Zero-dependency adapter used for tests and local workflow history."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._history: dict[str, list[WorkflowStep]] = {}

    def run_step(self, step: WorkflowStep) -> WorkflowStepResult:
        """Record the step in process memory and return an empty successful result.

        Args:
            step: The workflow step to record.

        Returns:
            Successful empty result for the recorded step.

        Raises:
            WorkflowAdapterError: If the step is missing required identifiers.
        """
        if not step.step_id or not step.plan_id or not step.task_id:
            raise WorkflowAdapterError("workflow step requires step_id, plan_id, and task_id", step.step_id)
        with self._lock:
            self._history.setdefault(step.plan_id, []).append(step)
        return WorkflowStepResult(
            step_id=step.step_id,
            success=True,
            output={},
            error=None,
            retry_attempt=0,
            latency_ms=0,
        )

    def replay(self, plan_id: str) -> Sequence[WorkflowStep]:
        """Return recorded steps for *plan_id* in insertion order.

        Args:
            plan_id: Plan whose step history should be replayed.

        Returns:
            Tuple of steps recorded for the plan.
        """
        with self._lock:
            return tuple(self._history.get(plan_id, ()))

    def health(self) -> bool:
        """The in-process adapter has no external provider to lose."""
        return True


def is_side_effect_step(step: WorkflowStep) -> bool:
    """Return True when a workflow step may perform an irreversible side effect."""
    return step.kind is WorkflowStepKind.SIDE_EFFECT


def pre_retry_policy_check(
    step: WorkflowStep,
    adapter: DurableWorkflowAdapter,
    *,
    prior_attempts: int = 0,
) -> None:
    """Fail closed before retrying side-effecting steps.

    Side-effecting steps are denied once they have already been attempted and
    either the adapter is unhealthy or the retry count reaches the hard ceiling.
    Non-side-effecting steps are not denied by this helper.

    Args:
        step: Step about to be retried.
        adapter: Adapter whose health participates in the retry decision.
        prior_attempts: Number of attempts already made for this step.

    Raises:
        WorkflowAdapterPolicyDenied: If retrying would violate side-effect policy.
    """
    if not is_side_effect_step(step) or prior_attempts < 1:
        return
    adapter_healthy = False
    try:
        adapter_healthy = adapter.health()
    except Exception as exc:
        raise WorkflowAdapterPolicyDenied(
            f"adapter health check failed before side-effect retry: {exc}",
            step.step_id,
        ) from exc
    if not adapter_healthy or prior_attempts >= 3:
        raise WorkflowAdapterPolicyDenied(
            f"side-effect retry denied for step {step.step_id!r} after {prior_attempts} attempt(s)",
            step.step_id,
        )


def get_default_inprocess_adapter() -> InProcessWorkflowAdapter:
    """Return the process-local in-process adapter singleton.

    Returns:
        The lazily-created in-process adapter singleton.
    """
    global _DEFAULT_INPROCESS_ADAPTER
    if _DEFAULT_INPROCESS_ADAPTER is None:
        with _ADAPTER_REGISTRY_LOCK:
            if _DEFAULT_INPROCESS_ADAPTER is None:
                _DEFAULT_INPROCESS_ADAPTER = InProcessWorkflowAdapter()
    return _DEFAULT_INPROCESS_ADAPTER


def reset_default_inprocess_adapter_for_test() -> None:
    """Clear the process-local in-process adapter singleton."""
    global _DEFAULT_INPROCESS_ADAPTER
    with _ADAPTER_REGISTRY_LOCK:
        _DEFAULT_INPROCESS_ADAPTER = None


def replay_workflow_from_receipts(
    receipts: Sequence[WorkflowReceipt],
    adapter: DurableWorkflowAdapter,
) -> tuple[WorkflowStep, ...]:
    """Rebuild a step sequence from receipts and verify adapter agreement.

    Args:
        receipts: Ordered authoritative receipt corpus.
        adapter: Adapter whose replay output must match the corpus.

    Returns:
        Tuple of replayed workflow steps.

    Raises:
        WorkflowAdapterReplayMismatch: If replay output and receipts diverge.
    """
    if not receipts:
        return ()
    plan_id = receipts[0].plan_id
    adapter_steps = tuple(adapter.replay(plan_id))
    if len(adapter_steps) != len(receipts):
        raise WorkflowAdapterReplayMismatch(
            f"adapter returned {len(adapter_steps)} steps; receipt corpus has {len(receipts)}",
        )
    reconstructed: list[WorkflowStep] = []
    for index, (receipt, adapter_step) in enumerate(zip(receipts, adapter_steps, strict=True)):
        if (
            receipt.step_id != adapter_step.step_id
            or receipt.plan_id != adapter_step.plan_id
            or receipt.kind_value != adapter_step.kind.value
            or receipt.payload_hash != adapter_step.payload_hash
        ):
            raise WorkflowAdapterReplayMismatch(
                (
                    f"step {index} mismatch: receipt "
                    f"({receipt.step_id!r}, {receipt.kind_value!r}) vs adapter "
                    f"({adapter_step.step_id!r}, {adapter_step.kind.value!r})"
                ),
                receipt.step_id,
            )
        reconstructed.append(adapter_step)
    return tuple(reconstructed)


def parent_plan_id_for(step: WorkflowStep) -> str | None:
    """Return the recursive-Foreman parent plan id for *step*, if one exists.

    Args:
        step: Workflow step whose plan id may be a child plan id.

    Returns:
        Parent plan id, or None when the plan is not registered as a child.
    """
    from vetinari.agents.consolidated.foreman import _PLAN_PARENT_LOCK, _PLAN_PARENT_MAP

    with _PLAN_PARENT_LOCK:
        return _PLAN_PARENT_MAP.get(step.plan_id)


__all__ = [
    "DurableWorkflowAdapter",
    "InProcessWorkflowAdapter",
    "WorkflowAdapterBackendDown",
    "WorkflowAdapterError",
    "WorkflowAdapterPolicyDenied",
    "WorkflowAdapterReplayMismatch",
    "WorkflowReceipt",
    "WorkflowStep",
    "WorkflowStepKind",
    "WorkflowStepResult",
    "get_default_inprocess_adapter",
    "is_side_effect_step",
    "parent_plan_id_for",
    "pre_retry_policy_check",
    "replay_workflow_from_receipts",
    "reset_default_inprocess_adapter_for_test",
]
