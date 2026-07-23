"""Read-only projections to automation, approval, and channel surfaces."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from vetinari.workbench.channels import ChannelDeliveryRequest, build_channel_delivery_envelope
from vetinari.workbench.channels.approvals import route_channel_approval_request
from vetinari.workbench.workflow_builder.contracts import WorkflowGraph
from vetinari.workbench.workflow_builder.preview import build_workflow_preview

ApprovalResolverProvider = Callable[[], Any]


def workflow_to_automation_recipe(graph: WorkflowGraph) -> dict[str, Any]:
    """Project a graph to an automation recipe preview without saving it there.

    Returns:
        dict[str, Any] value produced by workflow_to_automation_recipe().
    """
    preview = build_workflow_preview(graph)
    return {
        "schema_version": "workbench-workflow-builder-recipe-preview.v1",
        "graph_id": graph.graph_id,
        "name": graph.name,
        "simulation_only": True,
        "steps": list(preview.ordered_steps),
    }


def workflow_to_channel_preview(graph: WorkflowGraph) -> dict[str, Any]:
    """Project channel delivery steps through Channel Hub envelope contracts.

    Returns:
        dict[str, Any] value produced by workflow_to_channel_preview().
    """
    preview = build_workflow_preview(graph)
    deliveries = []
    for item in preview.channel_deliveries:
        step_id = str(item["step_id"])
        channel_id = str(item.get("channel_id", item.get("channel", "desktop")))
        request = ChannelDeliveryRequest(
            channel_id=channel_id,
            run_id=f"workflow-preview:{graph.graph_id}",
            actor_id="workflow-builder",
            action_id=step_id,
            action_type="workflow_channel_preview",
            summary=str(item.get("label", f"Workflow channel preview {step_id}")),
            payload={"graph_id": graph.graph_id, "step_id": item["step_id"], "preview_only": True},
            approval_decision_id=str(item.get("approval_decision_id", "")),
            action_fingerprint=_fingerprint(graph.graph_id, step_id, "channel_delivery"),
        )
        deliveries.append(build_channel_delivery_envelope(request).to_dict())
    return {
        "schema_version": "workbench-workflow-builder-channel-preview.v1",
        "graph_id": graph.graph_id,
        "deliveries": deliveries,
        "delivered": False,
    }


def workflow_to_approval_preview(
    graph: WorkflowGraph,
    *,
    resolver_provider: ApprovalResolverProvider | None = None,
) -> dict[str, Any]:
    """Project approval steps through the ordered Approval Chain contract.

    Returns:
        dict[str, Any] value produced by workflow_to_approval_preview().
    """
    preview = build_workflow_preview(graph)
    approvals = []
    provider = resolver_provider
    for item in preview.approval_points:
        step_id = str(item["step_id"])
        kwargs = {
            "channel_id": str(item.get("channel_id", "desktop")),
            "run_id": f"workflow-preview:{graph.graph_id}",
            "actor_id": "workflow-builder",
            "action_id": step_id,
            "action_type": "workflow_approval_preview",
            "action_fingerprint": _fingerprint(graph.graph_id, step_id, "approval"),
            "summary": str(item.get("label", f"Workflow approval preview {step_id}")),
            "project_id": str(graph.metadata.get("project_id", "default")),
            "session_id": "workflow-builder-preview",
            "approval_sources": ("workflow_builder",),
        }
        if provider is None:
            approvals.append(route_channel_approval_request(**kwargs))
        else:
            approvals.append(route_channel_approval_request(**kwargs, resolver_provider=provider))
    return {
        "schema_version": "workbench-workflow-builder-approval-preview.v1",
        "graph_id": graph.graph_id,
        "approvals": approvals,
    }


def _fingerprint(graph_id: str, step_id: str, purpose: str) -> str:
    material = {"graph_id": graph_id, "purpose": purpose, "step_id": step_id}
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["workflow_to_approval_preview", "workflow_to_automation_recipe", "workflow_to_channel_preview"]
