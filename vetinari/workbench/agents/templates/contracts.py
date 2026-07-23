"""Spawn and handoff contracts for Workbench agent templates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from vetinari.agents.contracts import AgentTask, OutcomeSignal
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.runtime.workbench_scheduler import Lane
from vetinari.types import AgentType, StatusEnum
from vetinari.workbench.agents.templates.cards import AgentTemplateCard
from vetinari.workbench.leases import LeaseStatus, WorkbenchLease
from vetinari.workbench.runs import RunKind, RunStatus, WorkbenchRun


class AgentTemplateContractError(ValueError):
    """Raised when a spawn or handoff contract would exceed the selected card."""


@dataclass(frozen=True, slots=True)
class AgentSpawnRequest:
    """A fail-closed request to spawn one selected Workbench agent template."""

    template_id: str
    project_id: str
    parent_run_id: str
    mode_template_id: str
    capability_pack_ids: tuple[str, ...]
    requested_tools: tuple[str, ...]
    requested_data_classes: tuple[str, ...]
    memory_scope: tuple[str, ...]
    model_policy: dict[str, object]
    cost_ceiling: str
    lease_id: str
    lease_intent: str
    cancellation_token: str
    receipt_correlation_id: str
    task_id: str
    run_id: str
    sandbox_profile: str
    isolation_profile: str
    approval_evidence_ref: str = ""
    review_evidence_ref: str = ""
    lane: str = Lane.HUB_AGENT.value

    def __post_init__(self) -> None:
        for field_name in (
            "template_id",
            "project_id",
            "parent_run_id",
            "mode_template_id",
            "cost_ceiling",
            "lease_id",
            "lease_intent",
            "cancellation_token",
            "receipt_correlation_id",
            "task_id",
            "run_id",
            "sandbox_profile",
            "isolation_profile",
            "lane",
        ):
            _require_non_empty(getattr(self, field_name), field_name)
        _require_non_empty_tuple(self.capability_pack_ids, "capability_pack_ids")
        _require_non_empty_tuple(self.requested_tools, "requested_tools")
        _require_non_empty_tuple(self.requested_data_classes, "requested_data_classes")
        _require_non_empty_tuple(self.memory_scope, "memory_scope")
        if not self.model_policy:
            raise AgentTemplateContractError("model_policy must be non-empty")

    @classmethod
    def from_card(
        cls,
        card: AgentTemplateCard,
        *,
        project_id: str,
        parent_run_id: str,
        mode_template_id: str,
        requested_tools: tuple[str, ...],
        requested_data_classes: tuple[str, ...],
        memory_scope: tuple[str, ...],
        cost_ceiling: str,
        lease_id: str,
        lease_intent: str,
        cancellation_token: str,
        receipt_correlation_id: str,
        task_id: str,
        run_id: str,
        approval_evidence_ref: str = "",
        review_evidence_ref: str = "",
        lane: str = Lane.HUB_AGENT.value,
    ) -> AgentSpawnRequest:
        """
        Build a spawn request after checking it does not exceed the card.

        Returns:
            The operation result.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if mode_template_id not in card.mode_template_ids:
            raise AgentTemplateContractError(f"mode template {mode_template_id!r} is not allowed by {card.template_id}")
        _ensure_subset(requested_tools, card.allowed_tools, "requested_tools", card.template_id)
        _ensure_subset(requested_data_classes, card.allowed_data_classes, "requested_data_classes", card.template_id)
        _ensure_subset(memory_scope, card.memory_scope, "memory_scope", card.template_id)
        if card.risk_posture.approval_required and not approval_evidence_ref.strip():
            raise AgentTemplateContractError(f"approval evidence is required before spawning {card.template_id}")
        if card.risk_posture.review_required and not review_evidence_ref.strip():
            raise AgentTemplateContractError(f"review evidence is required before spawning {card.template_id}")
        return cls(
            template_id=card.template_id,
            project_id=project_id,
            parent_run_id=parent_run_id,
            mode_template_id=mode_template_id,
            capability_pack_ids=card.capability_pack_ids,
            requested_tools=requested_tools,
            requested_data_classes=requested_data_classes,
            memory_scope=memory_scope,
            model_policy=dict(card.model_policy),
            cost_ceiling=cost_ceiling,
            lease_id=lease_id,
            lease_intent=lease_intent,
            cancellation_token=cancellation_token,
            receipt_correlation_id=receipt_correlation_id,
            task_id=task_id,
            run_id=run_id,
            sandbox_profile=card.sandbox_profile,
            isolation_profile=card.risk_posture.isolation_profile,
            approval_evidence_ref=approval_evidence_ref,
            review_evidence_ref=review_evidence_ref,
            lane=lane,
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AgentSpawnRequest(template_id={self.template_id!r}, project_id={self.project_id!r}, parent_run_id={self.parent_run_id!r})"


@dataclass(frozen=True, slots=True)
class AgentHandoffEnvelope:
    """Visible handoff envelope joining spawn, lease, cancellation, and receipt ids."""

    template: AgentTemplateCard
    spawn_request: AgentSpawnRequest
    handoff_id: str
    from_agent_id: str
    to_agent_id: str
    summary: str
    evidence_links: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.template.template_id != self.spawn_request.template_id:
            raise AgentTemplateContractError("handoff template must match spawn request")
        for field_name in ("handoff_id", "from_agent_id", "to_agent_id", "summary"):
            _require_non_empty(getattr(self, field_name), field_name)
        _require_non_empty_tuple(self.evidence_links, "evidence_links")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AgentHandoffEnvelope(template={self.template!r}, spawn_request={self.spawn_request!r}, handoff_id={self.handoff_id!r})"


@dataclass(frozen=True, slots=True)
class AgentSpawnProjection:
    """Projection into live agent, run, lease, and receipt contracts."""

    agent_task: AgentTask
    run: WorkbenchRun
    lease: WorkbenchLease
    receipt: WorkReceipt

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AgentSpawnProjection(agent_task={self.agent_task!r}, run={self.run!r}, lease={self.lease!r})"


def project_spawn_request(card: AgentTemplateCard, request: AgentSpawnRequest) -> AgentSpawnProjection:
    """
    Project a spawn request into read-only live contract objects.

    Args:
        card: Input value for this operation.
        request: Input value for this operation.

    Returns:
        The operation result.
    """
    _validate_request_against_card(card, request)
    now = _utc_now_iso()
    task = AgentTask(
        task_id=request.task_id,
        agent_type=AgentType.WORKBENCH,
        description=f"Spawn Workbench agent template {request.template_id}",
        prompt=f"Run {request.template_id} for project {request.project_id}",
        mode=request.mode_template_id,
        status=StatusEnum.READY,
        context={
            "template_id": request.template_id,
            "project_id": request.project_id,
            "parent_run_id": request.parent_run_id,
            "run_id": request.run_id,
            "lease_id": request.lease_id,
            "cancellation_token": request.cancellation_token,
            "receipt_correlation_id": request.receipt_correlation_id,
            "requested_tools": list(request.requested_tools),
            "requested_data_classes": list(request.requested_data_classes),
            "sandbox_profile": request.sandbox_profile,
            "isolation_profile": request.isolation_profile,
            "approval_evidence_ref": request.approval_evidence_ref,
            "review_evidence_ref": request.review_evidence_ref,
        },
    )
    run = WorkbenchRun(
        run_id=request.run_id,
        kind=RunKind.AGENT_RUN,
        status=RunStatus.PENDING,
        started_at_utc=now,
        finished_at_utc="",
        actor_agent_type=AgentType.WORKBENCH,
        asset_revisions=(("agent_template", card.revision), ("parent_run", request.parent_run_id)),
        lease_id=request.lease_id,
        shard_kind=None,
        project_id=request.project_id,
    )
    lease = WorkbenchLease(
        lease_id=request.lease_id,
        lane=Lane(request.lane),
        status=LeaseStatus.REQUESTED,
        lease_handle=request.lease_intent,
        granted_at_utc=now,
        released_at_utc="",
        requested_for_run_id=request.run_id,
        vram_share=0.0,
    )
    receipt = WorkReceipt(
        project_id=request.project_id,
        agent_id=request.template_id,
        agent_type=AgentType.WORKBENCH,
        kind=WorkReceiptKind.WORKER_TASK,
        outcome=OutcomeSignal(),
        receipt_id=request.receipt_correlation_id,
        started_at_utc=now,
        finished_at_utc=now,
        inputs_summary=f"spawn:{request.template_id}",
        outputs_summary=f"queued:{request.run_id}",
        linked_claim_ids=(request.lease_id, request.cancellation_token),
    )
    return AgentSpawnProjection(agent_task=task, run=run, lease=lease, receipt=receipt)


def render_mission_control_handoff_payload(envelope: AgentHandoffEnvelope) -> dict[str, object]:
    """
    Render a mission-control-compatible payload without registering routes.

    Returns:
        The operation result.
    """
    projection = project_spawn_request(envelope.template, envelope.spawn_request)
    request = envelope.spawn_request
    return {
        "project_id": request.project_id,
        "handoff_id": envelope.handoff_id,
        "template_id": request.template_id,
        "cancellation_token": request.cancellation_token,
        "receipt_correlation_id": request.receipt_correlation_id,
        "agent_task": {
            "run_id": projection.run.run_id,
            "task_id": projection.agent_task.task_id,
            "agent_type": projection.agent_task.agent_type.value,
            "status": projection.agent_task.status.value,
            "lane": projection.lease.lane.value,
            "escalated": False,
            "escalation_reason": None,
            "recursive_parent_run_id": request.parent_run_id,
            "blocker_summary": None,
            "retries": 0,
            "paused": False,
            "evidence_links": tuple(envelope.evidence_links),
            "started_at_utc": projection.run.started_at_utc,
            "finished_at_utc": None,
        },
        "queue_entry": {
            "lease_id": projection.lease.lease_id,
            "caller_subsystem": "workbench_agent_template_gallery",
            "target": projection.lease.lane.value,
            "state": "pending",
            "age_seconds": 0.0,
            "run_id": projection.run.run_id,
            "requested_at_utc": projection.lease.granted_at_utc,
        },
        "receipt": {
            "receipt_id": projection.receipt.receipt_id,
            "project_id": projection.receipt.project_id,
            "agent_id": projection.receipt.agent_id,
            "kind": projection.receipt.kind.value,
        },
    }


def _validate_request_against_card(card: AgentTemplateCard, request: AgentSpawnRequest) -> None:
    if card.template_id != request.template_id:
        raise AgentTemplateContractError("spawn request template does not match selected card")
    if request.mode_template_id not in card.mode_template_ids:
        raise AgentTemplateContractError(f"mode template {request.mode_template_id!r} is not allowed")
    _ensure_subset(request.capability_pack_ids, card.capability_pack_ids, "capability_pack_ids", card.template_id)
    _ensure_subset(request.requested_tools, card.allowed_tools, "requested_tools", card.template_id)
    _ensure_subset(request.requested_data_classes, card.allowed_data_classes, "requested_data_classes", card.template_id)
    _ensure_subset(request.memory_scope, card.memory_scope, "memory_scope", card.template_id)


def _ensure_subset(values: tuple[str, ...], allowed: tuple[str, ...], field_name: str, template_id: str) -> None:
    extra = tuple(sorted(set(values) - set(allowed)))
    if extra:
        raise AgentTemplateContractError(f"{field_name} exceeds {template_id}: {extra}")


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AgentTemplateContractError(f"{field_name} must be non-empty")


def _require_non_empty_tuple(value: tuple[str, ...], field_name: str) -> None:
    if not isinstance(value, tuple) or not value or any(not isinstance(item, str) or not item.strip() for item in value):
        raise AgentTemplateContractError(f"{field_name} must be a non-empty string tuple")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
