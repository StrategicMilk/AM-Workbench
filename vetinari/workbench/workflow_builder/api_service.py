"""Service layer for workflow-builder API handlers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.runtime.workbench_scheduler_types import RecurringTask, WorkbenchSchedulerConfigError
from vetinari.workbench.workflow_builder.adapters import (
    workflow_to_approval_preview,
    workflow_to_automation_recipe,
    workflow_to_channel_preview,
)
from vetinari.workbench.workflow_builder.contracts import (
    WorkflowBuilderError,
    WorkflowConsoleSnapshot,
    runtime_settings_from_dict,
    workflow_graph_from_dict,
)
from vetinari.workbench.workflow_builder.persistence import WorkflowBuilderStore
from vetinari.workbench.workflow_builder.preview import build_workflow_preview
from vetinari.workbench.workflow_builder.scheduling import (
    SCHEDULED_WORKFLOW_CAPABILITY,
    SCHEDULED_WORKFLOW_SOURCE,
    WorkflowScheduleRecord,
    WorkflowScheduleRequest,
    normalize_workflow_schedule,
)
from vetinari.workbench.workflow_builder.validation import validate_workflow_graph

logger = logging.getLogger(__name__)


DEFAULT_WORKFLOW_BUILDER_CONFIG = PROJECT_ROOT / "config" / "workbench" / "workflow_builder.yaml"
WORKFLOW_BUILDER_RECURRING_TASKS_FILE = "recurring_tasks.json"


_SchedulerFactory = Callable[[], Any]


@dataclass(frozen=True, slots=True)
class WorkflowBuilderService:
    """Runtime contract for WorkflowBuilderService."""

    store: WorkflowBuilderStore
    config_path: Path = DEFAULT_WORKFLOW_BUILDER_CONFIG
    scheduler_factory: _SchedulerFactory | None = None

    def metadata(self) -> dict[str, Any]:
        """Execute the metadata operation.

        Returns:
            dict[str, Any] value produced by metadata().
        """
        config = _load_config(self.config_path)
        return {
            "schema_version": "workbench-workflow-builder-metadata.v1",
            "step_kinds": config.get("step_kinds", []),
            "safety_modes": config.get("safety_modes", []),
            "trigger_options": config.get("trigger_options", []),
            "schedule_options": config.get("schedule_options", {}),
            "default_runtime": config.get("default_runtime", {}),
        }

    def validate(self, raw_graph: dict[str, Any]) -> dict[str, Any]:
        """Execute the validate operation.

        Returns:
            dict[str, Any] value produced by validate().
        """
        graph = workflow_graph_from_dict(raw_graph)
        return validate_workflow_graph(graph).to_dict()

    def preview(self, raw_graph: dict[str, Any]) -> dict[str, Any]:
        """Execute the preview operation.

        Returns:
            dict[str, Any] value produced by preview().
        """
        graph = workflow_graph_from_dict(raw_graph)
        validation = validate_workflow_graph(graph)
        preview = build_workflow_preview(graph)
        return {
            "schema_version": "workbench-workflow-builder-preview.v1",
            "validation": validation.to_dict(),
            "preview": preview.to_dict(),
            "automation_recipe": workflow_to_automation_recipe(graph),
            "approval_preview": workflow_to_approval_preview(graph),
            "channel_preview": workflow_to_channel_preview(graph),
        }

    def save(self, project_id: str, raw_graph: dict[str, Any]) -> dict[str, Any]:
        """Execute the save operation.

        Args:
            project_id: Project identifier that scopes the operation.
            raw_graph: Raw graph value consumed by save().

        Returns:
            dict[str, Any] value produced by save().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        graph = workflow_graph_from_dict(raw_graph)
        validation = validate_workflow_graph(graph)
        if not validation.passed:
            raise WorkflowBuilderError("workflow-validation-failed", ",".join(validation.errors))
        path = self.store.save_graph(project_id, graph)
        return {
            "schema_version": "workbench-workflow-builder-save.v1",
            "saved": True,
            "path": str(path),
            "graph": graph.to_dict(),
        }

    def load_graph(self, project_id: str, graph_id: str) -> dict[str, Any]:
        """Execute the load graph operation.

        Args:
            project_id: Project identifier that scopes the operation.
            graph_id: Graph id value consumed by load_graph().

        Returns:
            Resolved graph value.
        """
        graph = self.store.load_graph(project_id, graph_id)
        return {"schema_version": "workbench-workflow-builder-graph.v1", "graph": graph.to_dict()}

    def list_graphs(self, project_id: str) -> dict[str, Any]:
        """Execute the list graphs operation.

        Returns:
            Collection of graphs values.
        """
        try:
            graphs = self.store.list_graphs(project_id)
        except WorkflowBuilderError as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return {
                "schema_version": "workbench-workflow-builder-graphs.v1",
                "state": "degraded",
                "reason": exc.reason,
                "graphs": [],
            }
        return {
            "schema_version": "workbench-workflow-builder-graphs.v1",
            "state": "ready",
            "graphs": [graph.to_dict() for graph in graphs],
        }

    def runtime_settings(self, project_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute the runtime settings operation.

        Args:
            project_id: Project identifier that scopes the operation.
            payload: Payload data validated or transformed by the operation.

        Returns:
            dict[str, Any] value produced by runtime_settings().
        """
        if payload is None:
            return self.store.load_runtime_settings(project_id).to_dict()
        settings = runtime_settings_from_dict({"project_id": project_id, **payload})
        self.store.save_runtime_settings(settings)
        return settings.to_dict()

    def console_snapshot(self, project_id: str) -> dict[str, Any]:
        """Execute the console snapshot operation.

        Returns:
            dict[str, Any] value produced by console_snapshot().
        """
        degraded_reason = ""
        try:
            graphs = self.store.list_graphs(project_id)
        except WorkflowBuilderError as exc:
            graphs = ()
            degraded_reason = exc.reason
        settings = self.store.load_runtime_settings(project_id)
        active_graph_id = graphs[-1].graph_id if graphs else None
        recent_events: tuple[dict[str, Any], ...] = ({"event": "builder-console-read", "project_id": project_id},)
        if degraded_reason:
            recent_events = (
                {"event": "builder-console-degraded", "project_id": project_id, "reason": degraded_reason},
                *recent_events,
            )
        return WorkflowConsoleSnapshot(
            project_id=project_id,
            saved_graph_count=len(graphs),
            active_graph_id=active_graph_id,
            runtime_settings=settings,
            recent_events=recent_events,
        ).to_dict()

    def schedule_workflow(self, request: WorkflowScheduleRequest) -> dict[str, Any]:
        """Register a saved workflow graph as a recurring scheduler task.

        Args:
            request: Validated schedule request naming a saved project graph
                and either a supported cron expression or fixed interval.

        Returns:
            JSON-compatible schedule creation response containing the recurring
            scheduler task record.

        Raises:
            WorkflowBuilderError: If the graph is missing, invalid, the trigger
                cannot be normalized, or the scheduler registry rejects the
                recurring task.
        """
        graph = self.store.load_graph(request.project_id, request.graph_id)
        validation = validate_workflow_graph(graph)
        if not validation.passed:
            raise WorkflowBuilderError("workflow-validation-failed", ",".join(validation.errors))
        normalized = normalize_workflow_schedule(request)
        payload: dict[str, Any] = {
            "schema_version": "workbench-workflow-builder-scheduled-payload.v1",
            "source": SCHEDULED_WORKFLOW_SOURCE,
            "project_id": request.project_id,
            "graph_id": request.graph_id,
            "graph_schema_version": graph.schema_version,
        }
        if normalized.cron_expression is not None:
            payload["cron_expression"] = normalized.cron_expression
        try:
            task = self._scheduler().create_recurring_task(
                task_id=normalized.schedule_id,
                name=normalized.name,
                capability=SCHEDULED_WORKFLOW_CAPABILITY,
                payload=payload,
                interval_seconds=normalized.interval_seconds,
                start_at=normalized.next_run_at,
            )
        except ValueError as exc:
            raise WorkflowBuilderError("workflow-schedule-create-failed", str(exc)) from exc
        return {
            "schema_version": "workbench-workflow-builder-schedule.v1",
            "scheduled": True,
            "schedule": asdict(_workflow_schedule_record_from_task(task)),
        }

    def list_schedules(self, project_id: str) -> dict[str, Any]:
        """List Workflow Builder schedules persisted in the scheduler registry.

        Args:
            project_id: Project id used to filter Workflow Builder recurring
                tasks without exposing unrelated scheduler entries.

        Returns:
            JSON-compatible schedule listing for the requested project.

        Raises:
            WorkflowBuilderError: If a Workflow Builder scheduler payload is
                corrupted or missing required graph references.
        """
        self.store.project_dir(project_id)
        scheduler = self._scheduler()
        records = [
            record
            for task in scheduler.list_recurring_tasks()
            if (record := _workflow_schedule_record_from_task(task)) is not None and record.project_id == project_id
        ]
        return {
            "schema_version": "workbench-workflow-builder-schedules.v1",
            "state": "ready",
            "schedules": [asdict(record) for record in records],
        }

    def delete_schedule(self, project_id: str, schedule_id: str) -> dict[str, Any]:
        """Delete one Workflow Builder recurring schedule for a project.

        Args:
            project_id: Project id that owns the schedule.
            schedule_id: Recurring scheduler task id to remove.

        Returns:
            JSON-compatible deletion result with ``removed`` set to true only
            when the schedule existed and belonged to the requested project.

        Raises:
            WorkflowBuilderError: If the project id is invalid or a Workflow
                Builder scheduler payload is corrupted.
        """
        self.store.project_dir(project_id)
        scheduler = self._scheduler()
        removed = False
        for task in scheduler.list_recurring_tasks():
            record = _workflow_schedule_record_from_task(task)
            if record is None or record.project_id != project_id or record.schedule_id != schedule_id:
                continue
            removed = scheduler.delete_recurring_task(schedule_id)
            break
        return {
            "schema_version": "workbench-workflow-builder-delete-schedule.v1",
            "removed": removed,
            "schedule_id": schedule_id,
        }

    def _scheduler(self) -> Any:
        factory = self.scheduler_factory or _default_scheduler_factory(
            self.store.state_root / WORKFLOW_BUILDER_RECURRING_TASKS_FILE
        )
        try:
            return factory()
        except WorkbenchSchedulerConfigError as exc:
            raise WorkflowBuilderError("workflow-schedule-registry-unavailable", type(exc).__name__) from exc


def create_workflow_builder_service(
    *, state_root: Path | str | None = None, scheduler_factory: _SchedulerFactory | None = None
) -> WorkflowBuilderService:
    """Execute the create workflow builder service operation.

    Args:
        state_root: Optional workflow-builder state root override.
        scheduler_factory: Optional recurring scheduler factory for tests or
            alternate runtime hosts.

    Returns:
        Newly constructed workflow builder service value.
    """
    store = WorkflowBuilderStore(state_root=state_root) if state_root is not None else WorkflowBuilderStore()
    return WorkflowBuilderService(store=store, scheduler_factory=scheduler_factory)


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise WorkflowBuilderError("workflow-builder-config-missing", str(path))
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise WorkflowBuilderError("workflow-builder-config-unreadable", type(exc).__name__) from exc
    if not isinstance(raw, dict):
        raise WorkflowBuilderError("workflow-builder-config-invalid")
    return raw


def _default_scheduler_factory(recurring_tasks_path: Path) -> _SchedulerFactory:
    def create_scheduler() -> Any:
        from vetinari.runtime.workbench_scheduler import WorkbenchScheduler

        return WorkbenchScheduler(recurring_tasks_path=recurring_tasks_path)

    return create_scheduler


def _workflow_schedule_record_from_task(task: RecurringTask) -> WorkflowScheduleRecord | None:
    if task.capability != SCHEDULED_WORKFLOW_CAPABILITY and task.payload.get("source") != SCHEDULED_WORKFLOW_SOURCE:
        return None
    if task.capability != SCHEDULED_WORKFLOW_CAPABILITY or task.payload.get("source") != SCHEDULED_WORKFLOW_SOURCE:
        raise WorkflowBuilderError("workflow-schedule-payload-invalid", task.task_id)
    try:
        project_id = str(task.payload["project_id"])
        graph_id = str(task.payload["graph_id"])
    except KeyError as exc:
        raise WorkflowBuilderError("workflow-schedule-payload-invalid", task.task_id) from exc
    return WorkflowScheduleRecord(
        schedule_id=task.task_id,
        project_id=project_id,
        graph_id=graph_id,
        name=task.name,
        capability=task.capability,
        trigger_kind="cron" if task.payload.get("cron_expression") else "interval",
        interval_seconds=task.interval_seconds,
        start_at=task.start_at,
        next_run_at=task.next_run_at,
        cron_expression=str(task.payload["cron_expression"]) if task.payload.get("cron_expression") else None,
        payload=dict(task.payload),
    )


__all__ = [
    "DEFAULT_WORKFLOW_BUILDER_CONFIG",
    "WORKFLOW_BUILDER_RECURRING_TASKS_FILE",
    "WorkflowBuilderService",
    "create_workflow_builder_service",
]
