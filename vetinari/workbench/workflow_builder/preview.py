"""Read-only workflow preview generation."""

from __future__ import annotations

from vetinari.workbench.workflow_builder.contracts import WorkflowGraph, WorkflowPreview
from vetinari.workbench.workflow_builder.validation import validate_workflow_graph


def build_workflow_preview(graph: WorkflowGraph) -> WorkflowPreview:
    """Build a simulation-only preview without invoking automation runtime.

    Returns:
        Newly constructed workflow preview value.
    """
    validation = validate_workflow_graph(graph)
    ordered = [
        {
            "step_id": step.step_id,
            "kind": step.kind.value,
            "label": step.label,
            "ready": validation.passed and step.step_id in validation.reachable_steps,
        }
        for step in graph.steps
    ]
    approval_points = [
        {"step_id": step.step_id, "policy": step.config.get("approval_policy", "default"), "caller_owned": True}
        for step in graph.steps
        if step.kind.value == "approval"
    ]
    deliveries = [
        {"step_id": step.step_id, "channel": step.config.get("channel_id", "preview"), "preview_only": True}
        for step in graph.steps
        if step.kind.value == "channel_delivery"
    ]
    return WorkflowPreview(
        graph_id=graph.graph_id,
        ordered_steps=tuple(ordered),
        approval_points=tuple(approval_points),
        channel_deliveries=tuple(deliveries),
        runtime_mode=graph.safety_mode,
        executable=False,
    )


__all__ = ["build_workflow_preview"]
