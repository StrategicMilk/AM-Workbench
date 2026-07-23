"""Plan persistence and retrieval methods for PlanModeEngine."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.exceptions import PlanningError
from vetinari.planning.plan_types import (
    Plan,
    PlanApprovalRequest,
    PlanStatus,
    StatusEnum,
    Subtask,
)
from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)


class _PlanStoreMixin:
    """Plan persistence, approval, history, and risk helper behavior."""

    if TYPE_CHECKING:
        dry_run_risk_threshold: Any
        memory: Any

    @staticmethod
    def _plan_from_history_data(plan_data: dict[str, Any]) -> Plan:
        """Build a Plan from memory history rows with optional embedded JSON.

        Args:
            plan_data: Serialized plan history row from the memory store.

        Returns:
            Plan object decoded from the row payload.
        """
        payload = plan_data
        plan_json = plan_data.get("plan_json")
        if isinstance(plan_json, str):
            try:
                decoded = json.loads(plan_json)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                payload = decoded

        if "plan_json" in payload:
            payload = dict(payload)
            payload.pop("plan_json", None)

        return Plan.from_dict(payload)

    def _persist_plan(self, plan: Plan) -> bool:
        """Persist plan and all its subtasks to memory store.

        Args:
            plan: The plan to persist.

        Returns:
            True if the write succeeded, False otherwise.
        """
        plan_data = plan.to_dict()
        plan_data["plan_json"] = json.dumps(plan.to_dict())

        success = self.memory.write_plan_history(plan_data)
        if not success:
            return False

        for subtask in plan.subtasks:
            subtask_ok = self.memory.write_subtask_memory(subtask.to_dict())
            if not subtask_ok:
                logger.warning(
                    "Failed to persist subtask %s for plan %s — plan header written but subtask lost",
                    subtask.subtask_id,
                    plan.plan_id,
                )
                success = False

        return success

    def approve_plan(self, request: PlanApprovalRequest) -> Plan:
        """Record an approval or rejection decision for a plan.

        Updates the plan's status, approver, and timestamp in memory. For
        rejections the plan transitions to REJECTED with the optional
        rejection reason attached.

        Args:
            request: Approval request containing plan ID, decision, approver
                identity, and optional rejection reason.

        Returns:
            The updated Plan object reflecting the new status.

        Raises:
            PlanningError: If no plan with the given ID exists in history,
                or if the plan is already in a terminal state.
        """
        plan_data_list = self.memory.query_plan_history(plan_id=request.plan_id)

        if not plan_data_list:
            raise PlanningError(f"Plan not found: {request.plan_id}")

        plan = self._plan_from_history_data(plan_data_list[0])

        # Guard: cannot approve/reject plans in terminal states
        _terminal = {PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED, PlanStatus.REJECTED}
        if plan.status in _terminal:
            raise PlanningError(
                f"Cannot {'approve' if request.approved else 'reject'} plan {plan.plan_id}: "
                f"already in terminal state '{plan.status.value}'"
            )

        approver = _require_approver_identity(request.approver)
        if request.approved:
            plan.status = PlanStatus.APPROVED
            plan.approved_by = approver
            plan.approved_at = datetime.now(timezone.utc).isoformat()
            plan.auto_approved = False
        else:
            plan.status = PlanStatus.REJECTED
            plan.plan_justification = request.reason

        plan.updated_at = datetime.now(timezone.utc).isoformat()

        self._persist_plan(plan)

        logger.info(
            "Plan %s %s by approver_ref=%s",
            plan.plan_id,
            "approved" if request.approved else "rejected",
            _approver_log_ref(approver),
        )

        return plan

    def get_plan(self, plan_id: str) -> Plan | None:
        """Retrieve a plan by ID from memory.

        Args:
            plan_id: The plan identifier to look up.

        Returns:
            The Plan object, or None if not found.
        """
        plan_data_list = self.memory.query_plan_history(plan_id=plan_id)

        if not plan_data_list:
            return None

        plan = self._plan_from_history_data(plan_data_list[0])

        # PlanHistory only stores the header; subtasks live in SubtaskMemory.
        # Reload them if the header round-trip produced an empty subtask list.
        if not plan.subtasks:
            subtask_dicts = self.memory.query_subtasks(plan_id=plan_id)
            plan.subtasks = [Subtask.from_dict(s) for s in subtask_dicts]

        return plan

    def get_plan_history(self, goal_contains: str | None = None, limit: int = 10) -> list[dict]:
        """Get plan history from memory, optionally filtered by goal text.

        Args:
            goal_contains: Optional substring to filter by goal text.
            limit: Maximum number of plans to return.

        Returns:
            List of serialized plan dicts.
        """
        return self.memory.query_plan_history(goal_contains=goal_contains, limit=limit)

    def get_subtasks(self, plan_id: str) -> list[Subtask]:
        """Get all subtasks for a plan.

        Args:
            plan_id: The parent plan ID.

        Returns:
            List of Subtask objects for this plan.
        """
        subtask_data = self.memory.query_subtasks(plan_id=plan_id)
        return [Subtask.from_dict(s) for s in subtask_data]

    def update_subtask_status(
        self,
        plan_id: str,
        subtask_id: str,
        status: StatusEnum,
        outcome: str | None = None,
    ) -> bool:
        """Update a subtask's status and optionally record its outcome.

        Args:
            plan_id: The parent plan ID (used for context only; not filtered).
            subtask_id: The subtask ID to update.
            status: The new status to set.
            outcome: Optional outcome description to record.

        Returns:
            True if the update succeeded, False if the subtask was not found.
        """
        subtask_data_list = self.memory.query_subtasks(subtask_id=subtask_id)

        if not subtask_data_list:
            return False

        subtask_data = subtask_data_list[0]
        subtask_data["status"] = status.value
        if outcome:
            subtask_data["outcome"] = outcome

        subtask_data["updated_at"] = datetime.now(timezone.utc).isoformat()

        return self.memory.write_subtask_memory(subtask_data)

    def calculate_plan_risk(self, plan: Plan) -> float:
        """Recalculate and return the risk score for a plan.

        Args:
            plan: The plan to score.

        Returns:
            Updated risk score (0.0 to 1.0).
        """
        return plan.calculate_risk_score()

    def is_low_risk(self, risk_score: float) -> bool:
        """Check if a risk score is below the threshold for auto-approval.

        Args:
            risk_score: The risk score to evaluate.

        Returns:
            True if the score is at or below the dry-run threshold.
        """
        return risk_score <= self.dry_run_risk_threshold


def _require_approver_identity(approver: str) -> str:
    if not isinstance(approver, str):
        raise PlanningError("Plan approval requires a string approver identity")
    value = approver.strip()
    if not value:
        raise PlanningError("Plan approval requires a non-empty approver identity")
    return value


def _approver_log_ref(approver: str) -> str:
    redacted = redact_text(approver)
    if redacted != approver:
        digest = hashlib.sha256(approver.encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"redacted:{digest}"
    return redacted
