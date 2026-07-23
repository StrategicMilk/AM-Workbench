"""Workflow graph validation."""

from __future__ import annotations

from vetinari.workbench.workflow_builder.contracts import WorkflowGraph, WorkflowValidationResult


def validate_workflow_graph(graph: WorkflowGraph) -> WorkflowValidationResult:
    """Validate reachability, references, and dangerous runtime affordances.

    Returns:
        Validation outcome for workflow graph.
    """
    step_ids = {step.step_id for step in graph.steps}
    errors: list[str] = []
    warnings: list[str] = []
    for edge in graph.edges:
        if edge.source not in step_ids:
            errors.append(f"edge-source-missing:{edge.source}")
        if edge.target not in step_ids:
            errors.append(f"edge-target-missing:{edge.target}")
    incoming = {edge.target for edge in graph.edges}
    roots = [step.step_id for step in graph.steps if step.step_id not in incoming]
    if not roots:
        errors.append("root-step-missing")
    reachable = _reachable(roots, graph.edges)
    connected_steps = {edge.source for edge in graph.edges} | {edge.target for edge in graph.edges}
    isolated_roots = sorted(step_id for step_id in roots if step_id not in connected_steps)
    if len(roots) > 1 and isolated_roots:
        errors.append(f"unreachable-steps:{','.join(isolated_roots)}")
    unreachable = sorted(step_ids - reachable)
    if unreachable:
        errors.append(f"unreachable-steps:{','.join(unreachable)}")
    for step in graph.steps:
        if step.config.get("execute") or step.config.get("start") or step.config.get("stop"):
            errors.append(f"runtime-execution-forbidden:{step.step_id}")
        if step.kind.value == "channel_delivery" and not step.config.get("preview_only", True):
            errors.append(f"channel-delivery-must-preview:{step.step_id}")
        if step.kind.value == "approval" and not step.config.get("approval_policy"):
            warnings.append(f"approval-policy-missing:{step.step_id}")
    return WorkflowValidationResult(
        passed=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        reachable_steps=tuple(sorted(reachable)),
    )


def _reachable(roots: list[str], edges: tuple[object, ...]) -> set[str]:
    graph: dict[str, list[str]] = {}
    for edge in edges:
        graph.setdefault(edge.source, []).append(edge.target)
    seen: set[str] = set()
    pending = list(roots)
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(graph.get(current, ()))
    return seen


__all__ = ["validate_workflow_graph"]
