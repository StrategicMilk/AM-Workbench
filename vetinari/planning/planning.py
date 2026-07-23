"""Planning module — LEGACY wave-based plan management.

.. deprecated::
    This module is deprecated. Use ``vetinari.plan_mode.PlanModeEngine``
    for plan generation and ``vetinari.orchestration`` for execution.

# CANONICAL: vetinari.planning.plan_mode
# This file is the DEPRECATED legacy implementation. New code must NOT import
# from here. The authoritative modules are:
#   - vetinari/planning/plan_mode.py    — plan generation (PlanModeEngine)
#   - vetinari/planning/plan_types.py   — planning domain types (Plan, Subtask, etc.)
#   - vetinari/planning/plan_api.py     — REST endpoints for plan operations
#
# This file is retained only for backward-compatibility of existing callers.
# It will be removed once all call sites have migrated.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir
from vetinari.planning.planning_models import (
    Plan as Plan,
)
from vetinari.planning.planning_models import (
    PlanningExecutionPlan,
    PlanTask,
    Wave,
    WaveStatus,
)
from vetinari.types import PlanStatus, StatusEnum  # canonical source

logger = logging.getLogger(__name__)


def _warn_plan_manager_deprecated(method_name: str) -> None:
    logger.debug("PlanManager.%s is deprecated; use vetinari.planner instead.", method_name)


class PlanManager:
    """Plan manager."""

    _instance = None
    _instance_lock = threading.RLock()

    @classmethod
    def get_instance(cls, storage_path: str | None = None) -> PlanManager:
        """Get instance.

        Returns:
            The PlanManager result.
        """
        _warn_plan_manager_deprecated("get_instance")
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(storage_path)
            return cls._instance

    @classmethod
    def reset_instance_for_test(cls) -> None:
        """Reset the process-wide PlanManager singleton for isolated tests."""
        with cls._instance_lock:
            cls._instance = None

    def __init__(self, storage_path: str | None = None):
        if storage_path is None:
            storage_path = get_user_dir() / "plans"

        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.plans: dict[str, PlanningExecutionPlan] = {}
        self._load_plans()

    def _load_plans(self):
        for file in self.storage_path.glob("*.json"):
            try:
                with Path(file).open(encoding="utf-8") as f:
                    data = json.load(f)
                    plan = PlanningExecutionPlan.from_dict(data)
                    self.plans[plan.plan_id] = plan
            except Exception as e:
                logger.error("Error loading plan %s: %s", file, e)

    def _save_plan(self, plan: PlanningExecutionPlan) -> None:
        """Persist a plan to its JSON file.

        Args:
            plan: The Plan to save.

        Raises:
            ValueError: If the plan ID contains path traversal sequences that
                would place the file outside the configured storage directory.
        """
        target = (self.storage_path / f"{plan.plan_id}.json").resolve()
        if not target.is_relative_to(self.storage_path.resolve()):
            raise ValueError(f"Plan ID contains path traversal: {plan.plan_id}")
        with target.open("w", encoding="utf-8") as f:
            json.dump(plan.to_dict(), f, indent=2)

    def create_plan(
        self,
        title: str,
        prompt: str,
        created_by: str = "user",
        waves_data: list[dict] | None = None,
    ) -> PlanningExecutionPlan:
        """Create plan.

        Args:
            title: The title.
            prompt: The prompt.
            created_by: The created by.
            waves_data: The waves data.

        Returns:
            The Plan result.
        """
        _warn_plan_manager_deprecated("create_plan")
        plan_id = f"plan_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc).isoformat()

        waves = []
        if waves_data:
            for i, wave_data in enumerate(waves_data):
                wave_id = f"wave_{i + 1}"
                tasks = []
                for j, task_data in enumerate(wave_data.get("tasks", [])):
                    task_id = f"task_{i + 1}_{j + 1}"
                    task = PlanTask(
                        task_id=task_id,
                        agent_type=task_data.get("agent_type", "builder"),
                        description=task_data.get("description", ""),
                        prompt=task_data.get("prompt", ""),
                        dependencies=task_data.get("dependencies", []),
                        priority=task_data.get("priority", 5),
                    )
                    tasks.append(task)

                wave = Wave(
                    wave_id=wave_id,
                    milestone=wave_data.get("milestone", f"Wave {i + 1}"),
                    description=wave_data.get("description", ""),
                    order=i + 1,
                    tasks=tasks,
                    dependencies=wave_data.get("dependencies", []),
                )
                waves.append(wave)

        plan = PlanningExecutionPlan(
            plan_id=plan_id,
            title=title,
            prompt=prompt,
            created_by=created_by,
            created_at=now,
            updated_at=now,
            waves=waves,
        )

        self.plans[plan_id] = plan
        self._save_plan(plan)

        return plan

    def get_plan(self, plan_id: str) -> PlanningExecutionPlan | None:
        """Retrieve a plan by its unique identifier.

        Args:
            plan_id: The unique identifier of the plan to retrieve.

        Returns:
            The matching Plan instance, or None if no plan exists with that id.
        """
        _warn_plan_manager_deprecated("get_plan")
        return self.plans.get(plan_id)

    def list_plans(self, status: str | None = None, limit: int = 50, offset: int = 0) -> list[PlanningExecutionPlan]:
        """List plans.

        Args:
            status: The status.
            limit: The limit.
            offset: The offset.

        Returns:
            List of results.
        """
        _warn_plan_manager_deprecated("list_plans")
        plans = list(self.plans.values())
        if status:
            plans = [p for p in plans if p.status == status]
        plans.sort(key=lambda p: p.created_at, reverse=True)
        return plans[offset : offset + limit]

    def update_plan(self, plan_id: str, updates: dict) -> PlanningExecutionPlan | None:
        """Update plan.

        Args:
            plan_id: The plan id.
            updates: The updates.

        Returns:
            The Plan | None result.
        """
        _warn_plan_manager_deprecated("update_plan")
        plan = self.plans.get(plan_id)
        if not plan:
            return None

        if "title" in updates:
            plan.title = updates["title"]
        if "status" in updates:
            plan.status = updates["status"]

        plan.updated_at = datetime.now(timezone.utc).isoformat()
        self._save_plan(plan)
        return plan

    def delete_plan(self, plan_id: str) -> bool:
        """Delete plan.

        Args:
            plan_id: The plan id to delete.

        Returns:
            True if successful, False otherwise.

        Raises:
            ValueError: If the plan ID contains path traversal sequences that
                would place the file outside the configured storage directory.
        """
        _warn_plan_manager_deprecated("delete_plan")
        target = (self.storage_path / f"{plan_id}.json").resolve()
        if not target.is_relative_to(self.storage_path.resolve()):
            raise ValueError(f"Plan ID contains path traversal: {plan_id}")
        if plan_id in self.plans:
            del self.plans[plan_id]
            if target.exists():
                target.unlink()

            # Also clean up any associated subtask tree so orphaned subtask
            # JSON files don't accumulate on disk.
            try:
                from vetinari.planning.subtask_tree import subtask_tree

                subtask_tree.delete_tree(plan_id)
            except Exception:
                logger.warning(
                    "SubtaskTree.delete_tree skipped for plan %s — subtask data may need manual cleanup",
                    plan_id,
                )

            return True
        return False

    def start_plan(self, plan_id: str) -> PlanningExecutionPlan | None:
        """Start plan.

        Returns:
            The Plan | None result.
        """
        _warn_plan_manager_deprecated("start_plan")
        plan = self.plans.get(plan_id)
        if not plan:
            return None

        plan.status = PlanStatus.EXECUTING.value
        plan.updated_at = datetime.now(timezone.utc).isoformat()

        if plan.waves:
            plan.waves[0].status = WaveStatus.RUNNING.value
            for task in plan.waves[0].tasks:
                task.status = StatusEnum.PENDING.value

        self._save_plan(plan)
        return plan

    def pause_plan(self, plan_id: str) -> PlanningExecutionPlan | None:
        """Pause plan.

        Returns:
            The Plan | None result.
        """
        _warn_plan_manager_deprecated("pause_plan")
        plan = self.plans.get(plan_id)
        if not plan or plan.status != PlanStatus.EXECUTING.value:
            return None

        plan.status = PlanStatus.PAUSED.value
        plan.updated_at = datetime.now(timezone.utc).isoformat()

        for wave in plan.waves:
            if wave.status == WaveStatus.RUNNING.value:
                wave.status = WaveStatus.BLOCKED.value

        self._save_plan(plan)
        return plan

    def resume_plan(self, plan_id: str) -> PlanningExecutionPlan | None:
        """Resume plan.

        Returns:
            The Plan | None result.
        """
        _warn_plan_manager_deprecated("resume_plan")
        plan = self.plans.get(plan_id)
        if not plan or plan.status != PlanStatus.PAUSED.value:
            return None

        plan.status = PlanStatus.EXECUTING.value
        plan.updated_at = datetime.now(timezone.utc).isoformat()

        for wave in plan.waves:
            if wave.status == WaveStatus.BLOCKED.value:
                wave.status = WaveStatus.RUNNING.value

        self._save_plan(plan)
        return plan

    def cancel_plan(self, plan_id: str) -> PlanningExecutionPlan | None:
        """Cancel plan.

        Returns:
            The Plan | None result.
        """
        _warn_plan_manager_deprecated("cancel_plan")
        plan = self.plans.get(plan_id)
        if not plan:
            return None

        plan.status = PlanStatus.CANCELLED.value
        plan.updated_at = datetime.now(timezone.utc).isoformat()

        for wave in plan.waves:
            if wave.status in [WaveStatus.PENDING.value, WaveStatus.RUNNING.value]:
                wave.status = WaveStatus.BLOCKED.value
            for task in wave.tasks:
                if task.status in [StatusEnum.PENDING.value, StatusEnum.RUNNING.value]:
                    task.status = StatusEnum.BLOCKED.value

        self._save_plan(plan)
        return plan

    def update_task_status(
        self,
        plan_id: str,
        wave_id: str,
        task_id: str,
        status: str,
        result: Any = None,
        error: str = "",
    ) -> PlanningExecutionPlan | None:
        """Update task status.

        Args:
            plan_id: The plan id.
            wave_id: The wave id.
            task_id: The task id.
            status: The status.
            result: The result.
            error: The error.

        Returns:
            The Plan | None result.
        """
        _warn_plan_manager_deprecated("update_task_status")
        plan = self.plans.get(plan_id)
        if not plan:
            return None

        for wave in plan.waves:
            if wave.wave_id == wave_id:
                for task in wave.tasks:
                    if task.task_id == task_id:
                        task.status = status
                        if status == StatusEnum.RUNNING.value:
                            task.actual_start = datetime.now(timezone.utc).isoformat()
                        elif status == StatusEnum.COMPLETED.value:
                            task.actual_end = datetime.now(timezone.utc).isoformat()
                            task.result = result
                        elif status == StatusEnum.FAILED.value:
                            task.actual_end = datetime.now(timezone.utc).isoformat()
                            task.error = error
                        break
                break

        self._check_wave_completion(plan, wave_id)
        self._save_plan(plan)
        return plan

    @staticmethod
    def _check_wave_completion(plan: PlanningExecutionPlan, completed_wave_id: str):
        for wave in plan.waves:
            if wave.wave_id == completed_wave_id:
                all_completed = all(t.status == StatusEnum.COMPLETED.value for t in wave.tasks)
                if all_completed:
                    wave.status = WaveStatus.COMPLETED.value

                    next_wave_idx = wave.order
                    if next_wave_idx < len(plan.waves):
                        next_wave = plan.waves[next_wave_idx]
                        if next_wave.status == WaveStatus.PENDING.value:
                            next_wave.status = WaveStatus.RUNNING.value
                            for task in next_wave.tasks:
                                if task.status == StatusEnum.PENDING.value:
                                    task.status = StatusEnum.PENDING.value
                    else:
                        plan.status = PlanStatus.COMPLETED.value

        plan.updated_at = datetime.now(timezone.utc).isoformat()


def get_plan_manager() -> PlanManager:
    """Lazily return the singleton PlanManager. Use this instead of module-level plan_manager."""
    return PlanManager.get_instance()


# Backward-compatible alias — resolved lazily so importing this module does NOT
# trigger filesystem I/O at import time.
class _LazyPlanManager:
    """Proxy that resolves the PlanManager singleton on first attribute access.

    Deprecated: Use ``vetinari.planning.plan_mode.PlanModeEngine`` directly.
    """

    def __getattr__(self, name):
        warnings.warn(
            "_LazyPlanManager is deprecated. Use vetinari.planning.plan_mode.PlanModeEngine directly.",
            DeprecationWarning,
            stacklevel=2,
        )
        return getattr(PlanManager.get_instance(), name)

    def __repr__(self):
        return repr(PlanManager.get_instance())


plan_manager = _LazyPlanManager()
