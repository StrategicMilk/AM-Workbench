"""Durable workflow adapter dispatch methods for DurableExecutionEngine."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.orchestration.execution_graph import ExecutionGraph, ExecutionTaskNode
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.types import EvidenceBasis, ShardKind, StatusEnum
from vetinari.workbench.durable_workflow_adapter import (
    _DURABLE_RECEIPT_ACTOR,
    DurableWorkflowAdapter,
    WorkflowAdapterBackendDown,
    WorkflowAdapterError,
    WorkflowStep,
    WorkflowStepKind,
    parent_plan_id_for,
    pre_retry_policy_check,
)

logger = logging.getLogger(__name__)


_PROJECT_ID_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


class _DurableExecutionAdapterSupport:
    """Adapter-specific task dispatch behavior shared by the durable engine."""

    def _dispatch_step_via_adapter(
        self,
        graph: ExecutionGraph,
        task: ExecutionTaskNode,
        *,
        prior_attempts: int,
        adapter: DurableWorkflowAdapter | None = None,
    ) -> dict[str, Any]:
        """Route one task through the registered workflow adapter and emit a receipt."""
        workflow_adapter = adapter if adapter is not None else self._workflow_adapter
        if workflow_adapter is None:
            raise WorkflowAdapterError("cannot dispatch durable workflow step without a registered adapter", task.id)

        step = self._workflow_step_for(graph, task)
        pre_retry_policy_check(step, workflow_adapter, prior_attempts=prior_attempts)
        try:
            result = workflow_adapter.run_step(step)
        except WorkflowAdapterError:
            raise
        except Exception as exc:
            raise WorkflowAdapterBackendDown(
                f"adapter run_step raised {type(exc).__name__}: {exc}",
                step.step_id,
            ) from exc

        self._emit_durable_step_receipt(graph, task, step, result.success, result.error)
        if result.success:
            task.status = StatusEnum.COMPLETED
            task.error = ""
            task.output_data = result.output
            task.completed_at = datetime.now(timezone.utc).isoformat()
            self._emit_event(
                "task_completed",
                task.id,
                {"status": StatusEnum.COMPLETED.value, "attempts": prior_attempts + 1},
                graph.plan_id,
            )
            self._record_learning(task, task.id, result.output)
            if self._on_task_complete:
                self._on_task_complete(task)
            self._save_checkpoint(graph.plan_id, graph)
            response: dict[str, Any] = {
                "status": StatusEnum.COMPLETED.value,
                "output": result.output,
                "tokens_used": result.output.get("tokens_used", 0),
                "metadata": result.output.get("metadata", {}),
            }
            response.update(result.output)
            return response

        task.status = StatusEnum.FAILED
        task.error = result.error or "durable workflow adapter reported failure"
        task.completed_at = datetime.now(timezone.utc).isoformat()
        self._emit_event(
            "task_failed",
            task.id,
            {"status": StatusEnum.FAILED.value, "error": task.error, "attempts": prior_attempts + 1},
            graph.plan_id,
        )
        if self._on_task_fail:
            self._on_task_fail(task)
        self._save_checkpoint(graph.plan_id, graph)
        return {"status": StatusEnum.FAILED.value, "error": task.error}

    def _resume_step_via_adapter(
        self,
        *,
        execution_id: str,
        step_id: str,
        prior_attempts: int,
    ) -> dict[str, Any]:
        """Resume a paused checkpointed task through the registered adapter."""
        if self._workflow_adapter is None:
            raise WorkflowAdapterError(
                f"cannot resume execution {execution_id!r} via durable adapter - no adapter registered",
                step_id,
            )
        graph = self.load_checkpoint(execution_id)
        if graph is None:
            raise WorkflowAdapterError(f"no checkpoint found for execution {execution_id!r}", step_id)
        task = graph.nodes.get(step_id)
        if task is None:
            raise WorkflowAdapterError(
                f"step {step_id!r} not found in checkpoint for execution {execution_id!r}",
                step_id,
            )
        return self._dispatch_step_via_adapter(graph, task, prior_attempts=prior_attempts)

    def _workflow_step_for(self, graph: ExecutionGraph, task: ExecutionTaskNode) -> WorkflowStep:
        payload = task.input_data or {}
        kind = self._workflow_step_kind(task.task_type)
        step = WorkflowStep(
            step_id=task.id,
            kind=kind,
            plan_id=str(payload.get("plan_id") or graph.plan_id),
            task_id=task.id,
            payload_hash=self._payload_hash(payload),
            parent_plan_id=str(payload["parent_plan_id"]) if payload.get("parent_plan_id") else None,
        )
        if step.parent_plan_id is None:
            parent_plan_id = parent_plan_id_for(step)
            if parent_plan_id is not None:
                step = WorkflowStep(
                    step_id=step.step_id,
                    kind=step.kind,
                    plan_id=step.plan_id,
                    task_id=step.task_id,
                    payload_hash=step.payload_hash,
                    parent_plan_id=parent_plan_id,
                )
        return step

    @staticmethod
    def _workflow_step_kind(task_type: str) -> WorkflowStepKind:
        try:
            return WorkflowStepKind(task_type)
        except ValueError:
            logger.warning("unknown task_type %r - defaulting durable workflow kind to AGENT", task_type)
            return WorkflowStepKind.AGENT

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _emit_durable_step_receipt(
        self,
        graph: ExecutionGraph,
        task: ExecutionTaskNode,
        step: WorkflowStep,
        passed: bool,
        error: str | None,
    ) -> None:

        now = datetime.now(timezone.utc).isoformat()
        parent_link = (f"parent_plan_id:{step.parent_plan_id}",) if step.parent_plan_id else ()
        evidence_payload = {
            "step_id": step.step_id,
            "plan_id": step.plan_id,
            "task_id": step.task_id,
            "kind": step.kind.value,
            "payload_hash": step.payload_hash,
            "passed": passed,
            "error": error or "",
        }
        evidence_hash = hashlib.sha256(
            json.dumps(evidence_payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        receipt = WorkReceipt(
            project_id=self._receipt_project_id(graph.plan_id),
            agent_id="durable-workflow-adapter",
            agent_type=_DURABLE_RECEIPT_ACTOR,
            kind=WorkReceiptKind.DURABLE_STEP,
            outcome=OutcomeSignal(
                passed=passed,
                score=1.0 if passed else 0.0,
                basis=EvidenceBasis.TOOL_EVIDENCE,
                tool_evidence=(
                    ToolEvidence(
                        tool_name="durable_workflow_adapter",
                        command=f"run_step kind={step.kind.value} step_id={step.step_id}",
                        exit_code=0 if passed else 1,
                        stdout_snippet=f"plan_id={step.plan_id} task_id={step.task_id} payload_hash={step.payload_hash}",
                        stdout_hash=evidence_hash,
                        passed=passed,
                    ),
                ),
                provenance=Provenance(
                    source="vetinari.orchestration.durable_execution",
                    timestamp_utc=now,
                    tool_name="durable_workflow_adapter",
                ),
                issues=(error,) if error else (),
                kind=ShardKind.STANDARD,
            ),
            started_at_utc=now,
            finished_at_utc=now,
            inputs_summary=f"durable step: {task.id}"[:200],
            outputs_summary=f"status={'passed' if passed else 'failed'}"[:200],
            linked_claim_ids=parent_link,
        )
        self._receipt_store.append(receipt)

    @staticmethod
    def _receipt_project_id(plan_id: str) -> str:
        safe = _PROJECT_ID_SAFE_CHARS.sub("_", plan_id).strip("_")
        return safe or "durable-workflow"
