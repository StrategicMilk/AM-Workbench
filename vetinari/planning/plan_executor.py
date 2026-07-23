"""Plan execution and approval methods for PlanModeEngine.

Extracted from plan_mode.py to stay under the 550-line ceiling.
Contains methods that execute coding subtasks, check approval requirements,
log approval decisions, and auto-approve low-risk tasks.

All methods here are incorporated into PlanModeEngine via inheritance from
_PlanExecutor.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol, cast

from vetinari.boundary_guards import account_evidence_drop
from vetinari.planning.plan_types import Plan, Subtask, TaskDomain

logger = logging.getLogger(__name__)

APPROVAL_REQUIRED_DOMAINS = frozenset({TaskDomain.CODING})
"""Subtask domains that require explicit approval while plan mode is active."""


class _PlanExecutorOwner(Protocol):
    dry_run_risk_threshold: float

    def log_approval_decision(
        self,
        plan_id: str,
        subtask_id: str,
        approved: bool,
        approver: str,
        reason: str = "",
        risk_score: float = 0.0,
        task_type: str = "",
    ) -> bool:
        """Persist an approval decision for a plan subtask.

        Args:
            plan_id: Parent plan identifier.
            subtask_id: Approved or rejected subtask identifier.
            approved: Whether execution was approved.
            approver: Identity of the approving actor.
            reason: Optional rationale for the decision.
            risk_score: Risk score used for the decision.
            task_type: Domain or execution type for the approved task.

        Returns:
            True when the approval decision was persisted.
        """


class _PlanExecutor:
    """Execution and approval methods for PlanModeEngine.

    Inheritors MUST provide:
    - self.dry_run_risk_threshold (float)
    - self.log_approval_decision(...) -> bool
    """

    # ------------------------------------------------------------------
    # Approval checks
    # ------------------------------------------------------------------

    def requires_approval(self, subtask: Subtask, plan_mode: bool = True) -> bool:
        """Check if a subtask requires human approval.

        In Plan mode, coding tasks require human approval before execution.
        In Build mode, coding tasks proceed without approval (but are still logged).

        Args:
            subtask: The subtask to evaluate.
            plan_mode: Whether plan mode is active.

        Returns:
            True if approval is required, False otherwise.
        """
        if not plan_mode:
            return False

        return bool(subtask.domain in APPROVAL_REQUIRED_DOMAINS)

    def check_subtask_approval_required(self, plan: Plan, subtask_id: str, plan_mode: bool = True) -> dict[str, Any]:
        """Check if a subtask requires approval and return its current status.

        Args:
            plan: The plan containing the subtask.
            subtask_id: The subtask ID to check.
            plan_mode: Whether plan mode is active.

        Returns:
            Dict with ``subtask_id``, ``domain``, ``requires_approval``,
            ``plan_mode``, ``description``, and ``status``. If the subtask
            is not found, returns ``{"requires_approval": False, "error":
            "Subtask not found"}``.
        """
        subtask = plan.get_subtask(subtask_id)
        if not subtask:
            return {"requires_approval": False, "error": "Subtask not found"}

        requires_approval = self.requires_approval(subtask, plan_mode)

        return {
            "subtask_id": subtask_id,
            "domain": subtask.domain.value,
            "requires_approval": requires_approval,
            "plan_mode": plan_mode,
            "description": subtask.description,
            "status": subtask.status.value,
        }

    def log_approval_decision(
        self,
        plan_id: str,
        subtask_id: str,
        approved: bool,
        approver: str,
        reason: str = "",
        risk_score: float = 0.0,
        task_type: str = "",
    ) -> bool:
        """Log an approval decision to the unified memory store.

        Args:
            plan_id: The parent plan ID.
            subtask_id: The subtask that was approved or rejected.
            approved: True if approved, False if rejected.
            approver: Identity of the approver.
            reason: Optional reason for the decision.
            risk_score: Risk score at the time of the decision.
            task_type: Domain or execution type for the approved task. Defaults
                to ``unknown`` instead of pretending every approval is coding.

        Returns:
            True if the log entry was persisted, False otherwise.
        """
        try:
            from vetinari.memory import ApprovalDetails, MemoryEntry, MemoryType
            from vetinari.memory.unified import get_unified_memory_store

            store = get_unified_memory_store()
            if store is not None:
                approval_details = ApprovalDetails(
                    task_id=subtask_id,
                    task_type=task_type.strip() or "unknown",
                    plan_id=plan_id,
                    approval_status="approved" if approved else "rejected",
                    approver=approver,
                    reason=reason,
                    risk_score=risk_score,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )

                entry = MemoryEntry(
                    agent="plan-approval",
                    entry_type=MemoryType.APPROVAL,
                    content=json.dumps(approval_details.to_dict()),
                    summary=f"{'Approved' if approved else 'Rejected'} task {subtask_id} by {approver}",
                    provenance="plan_approval_api",
                )

                store.remember(entry)
                logger.info("Logged approval decision for %s: %s", subtask_id, "approved" if approved else "rejected")
                return True
            logger.warning("Dual memory not available, approval not logged")
            return False

        except Exception as e:
            logger.error("Failed to log approval decision: %s", e)
            return False

    def auto_approve_if_low_risk(self, plan: Plan, subtask: Subtask) -> bool:
        """Auto-approve a subtask if it is low risk and the plan is in dry-run mode.

        Only coding subtasks require approval; non-coding subtasks are always
        auto-approved. Coding subtasks are auto-approved when the plan's
        risk_score is at or below ``dry_run_risk_threshold``.

        Args:
            plan: The plan the subtask belongs to.
            subtask: The subtask candidate for auto-approval.

        Returns:
            True if the subtask was auto-approved, False otherwise.
        """
        if not plan.dry_run:
            return False

        if subtask.domain != TaskDomain.CODING:
            return True

        owner = cast(_PlanExecutorOwner, self)
        if plan.risk_score <= owner.dry_run_risk_threshold:
            approval_record = {
                "plan_id": plan.plan_id,
                "subtask_id": subtask.subtask_id,
                "approved": True,
                "approver": "system_auto",
                "risk_score": plan.risk_score,
                "task_type": subtask.domain.value,
            }
            logged = owner.log_approval_decision(
                approval_record["plan_id"],
                approval_record["subtask_id"],
                approved=True,
                approver=approval_record["approver"],
                reason=f"Auto-approved due to low risk (score: {plan.risk_score:.2f})",
                risk_score=plan.risk_score,
                task_type=approval_record["task_type"],
            )
            if not logged:
                account_evidence_drop(approval_record, "approval_log", logger=logger)
                return False
            return True

        return False

    # ------------------------------------------------------------------
    # Coding task execution
    # ------------------------------------------------------------------

    def execute_coding_task(self, plan: Plan, subtask: Subtask) -> dict[str, Any]:
        """Execute a coding subtask using the in-process coding agent.

        Args:
            plan: The parent plan.
            subtask: The coding subtask to execute.

        Returns:
            Dict with ``success`` (bool) and, on success, the artifact
            metadata produced by the coding agent. On failure, includes
            ``error`` with a descriptive message.
        """
        try:
            from vetinari.coding_agent import CodingTaskType, get_coding_agent, make_code_agent_task

            agent = get_coding_agent()

            if not agent.is_available():
                return {"success": False, "error": "Coding agent not available"}

            code_task = make_code_agent_task(
                subtask.description,
                task_type=CodingTaskType.IMPLEMENT,
                language="python",
                repo_path=".",
                target_files=[f"{subtask.subtask_id}.py"],
                constraints=subtask.definition_of_done.criteria if subtask.definition_of_done else [],
                plan_id=plan.plan_id,
                subtask_id=subtask.subtask_id,
            )

            # Execute task
            artifact = agent.run_task(code_task)

            # Log to memory
            try:
                from vetinari.memory import MemoryEntry, MemoryType, get_unified_memory_store

                store = get_unified_memory_store()
                entry = MemoryEntry(
                    agent="coding_agent",
                    entry_type=MemoryType.FEATURE,
                    content=json.dumps(artifact.to_dict()),
                    summary=f"Generated code artifact for {subtask.subtask_id}",
                    provenance=f"plan:{plan.plan_id},task:{subtask.subtask_id}",
                )
                store.remember(entry)
            except Exception as mem_err:
                logger.warning("Failed to log coding artifact to memory: %s", mem_err)

            return {"success": True, "artifact": artifact.to_dict(), "task_id": code_task.task_id}

        except ImportError as e:
            logger.error("Coding agent not available: %s", e)
            return {"success": False, "error": f"Coding agent not available: {e}"}
        except Exception:
            logger.exception("Coding task execution failed")
            return {"success": False, "error": "Coding task execution failed"}

    def execute_multi_step_coding(self, plan: Plan, subtasks: list[Subtask]) -> list[dict[str, Any]]:
        """Execute multiple coding tasks under ExecutionStrategy.SCAFFOLD_THEN_FILL.

        Execution continues even when individual subtasks fail.

        Args:
            plan: The parent plan.
            subtasks: Ordered list of subtasks to execute.

        Returns:
            List of result dicts, one per subtask in input order, each
            with the same structure as ``execute_coding_task`` returns.
        """
        if not subtasks:
            return []
        results = []

        for subtask in subtasks:
            result = self.execute_coding_task(plan, subtask)
            results.append(result)

            if not result.get("success", False):
                logger.warning("Subtask %s failed, continuing...", subtask.subtask_id)

        return results
