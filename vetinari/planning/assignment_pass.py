"""Assignment Pass.

===============
Executes the model/agent assignment pass for all pending subtasks in a plan.
Uses the DynamicModelRouter to assign the best available model to each task.
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


def _build_assignment_record(
    *,
    subtask_id: str,
    description: str,
    agent_type: str,
    assigned_agent: str,
    model: str | None,
    action: str,
) -> dict[str, Any]:
    return {
        "subtask_id": subtask_id,
        "description": description,
        "agent_type": agent_type,
        "assigned_agent": assigned_agent,
        "model": model,
        "action": action,
    }


def _process_subtask(
    plan_id: str, st: Any, router: Any, subtask_tree: Any, auto_assign: bool
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    if st.assigned_agent and st.assigned_agent != "unassigned":
        return (
            _build_assignment_record(
                subtask_id=st.subtask_id,
                description=st.description,
                agent_type=st.agent_type,
                assigned_agent=st.assigned_agent,
                model=getattr(st, "assigned_model_id", None),
                action=StatusEnum.SKIPPED.value,
            ),
            None,
        )

    try:
        task_type = st.agent_type or "general"
        model_id = router.select_model(task_type=task_type.lower(), task_description=st.description)
        if auto_assign:
            subtask_tree.update_subtask(
                plan_id,
                st.subtask_id,
                {
                    "assigned_agent": task_type,
                    "assigned_model_id": model_id,
                    "status": StatusEnum.ASSIGNED.value,
                },
            )
        return (
            _build_assignment_record(
                subtask_id=st.subtask_id,
                description=st.description,
                agent_type=task_type,
                assigned_agent=task_type,
                model=model_id,
                action=StatusEnum.ASSIGNED.value if auto_assign else "recommended",
            ),
            None,
        )
    except Exception as e:
        logger.warning("Assignment failed for subtask %s: %s", st.subtask_id, e)
        return None, {"subtask_id": st.subtask_id, "error": str(e)}


def _build_assignment_result(
    *,
    plan_id: str,
    subtasks: list[Any],
    assignments: list[dict[str, Any]],
    errors: list[dict[str, str]],
    auto_assign: bool,
) -> dict[str, Any]:
    return {
        "plan_id": plan_id,
        "total": len(subtasks),
        "assigned": sum(1 for a in assignments if a.get("action") == StatusEnum.ASSIGNED.value),
        "skipped": sum(1 for a in assignments if a.get("action") == StatusEnum.SKIPPED.value),
        "errors": len(errors),
        "assignments": assignments,
        "error_details": errors,
        "auto_assign": auto_assign,
    }


def execute_assignment_pass(
    plan_id: str,
    auto_assign: bool = True,
) -> dict[str, Any]:
    """Execute the assignment pass for a plan.

    For each unassigned subtask in the plan, selects the best model+agent
    using DynamicModelRouter and updates the subtask record.

    Args:
        plan_id: The plan to process
        auto_assign: If True, automatically apply assignments. If False,
                     return recommendations without applying them.

    Returns:
        dict with assignment results per subtask

    Raises:
        Exception: Propagates failures from subtask loading, dynamic router
            access, or assignment persistence after recording an error log.
    """
    try:
        from vetinari.models.dynamic_model_router import get_dynamic_router
        from vetinari.planning.subtask_tree import subtask_tree

        subtasks = subtask_tree.get_all_subtasks(plan_id)
        router = get_dynamic_router()

        assignments = []
        errors = []

        for st in subtasks:
            assignment, error = _process_subtask(plan_id, st, router, subtask_tree, auto_assign)
            if assignment is not None:
                assignments.append(assignment)
            if error is not None:
                errors.append(error)

        return _build_assignment_result(
            plan_id=plan_id,
            subtasks=subtasks,
            assignments=assignments,
            errors=errors,
            auto_assign=auto_assign,
        )

    except Exception as e:
        raise RuntimeError(f"execute_assignment_pass failed for plan {plan_id}: {e}") from e
