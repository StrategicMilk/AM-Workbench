"""Read-only Workbench desktop shell contract.

The shell is an object-centered projection over the existing Workbench spine.
It does not create a second persistence path and it fails closed when the
selected action lacks cost, risk, or provenance context.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.workbench import (
    EvalResult,
    ProposalStatus,
    WorkbenchAsset,
    WorkbenchProposal,
    WorkbenchRun,
    WorkbenchSpine,
    WorkbenchSpineCorrupt,
    get_workbench_spine,
)
from vetinari.workbench.shell_models import (
    RiskLevel,
    ShellCommand,
    ShellNavigationItem,
    ShellObjectSummary,
    ShellQueueSummary,
    ShellRiskControl,
    ShellSplitComparison,
    ShellStatus,
    ShellTimelineEvent,
    WorkbenchShellSnapshot,
)
from vetinari.workbench.spine import validate_project_id


class WorkbenchShellError(RuntimeError):
    """Raised when the shell cannot safely build a snapshot."""


def build_workbench_shell_snapshot(
    project_id: str = "default",
    *,
    spine: WorkbenchSpine | None = None,
) -> WorkbenchShellSnapshot:
    """Build a read-only shell snapshot from Workbench spine objects.

    Returns:
        Newly constructed workbench shell snapshot value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    canonical_project_id = validate_project_id(project_id)
    try:
        resolved_spine = spine if spine is not None else get_workbench_spine()
        runs = [row for row in resolved_spine.list_runs() if row.project_id == canonical_project_id]
        assets = list(resolved_spine.list_assets())
        evals = list(resolved_spine.list_evals())
        proposals = list(resolved_spine.list_proposals())
        leases = list(resolved_spine.list_leases())
    except WorkbenchSpineCorrupt as exc:
        raise WorkbenchShellError(f"workbench spine unavailable: {exc}") from exc
    except Exception as exc:
        raise WorkbenchShellError(f"workbench shell snapshot unavailable: {exc}") from exc

    project_asset_ids = {asset_id for run in runs for asset_id, _revision in run.asset_revisions}
    visible_assets = [asset for asset in assets if asset.asset_id in project_asset_ids]
    project_run_ids = {run.run_id for run in runs}
    visible_evals = [row for row in evals if row.run_id in project_run_ids]
    visible_proposals = _filter_proposals(proposals, project_asset_ids)
    objects = _object_summaries(runs, visible_assets, visible_evals, visible_proposals)
    queue = _queue_summary(runs, leases)
    timeline = _timeline(runs, visible_assets, visible_evals, visible_proposals)
    comparison = _split_comparison(runs)
    risk_control = _risk_control(runs, visible_assets, visible_proposals)
    commands = _commands(objects, risk_control)
    navigation = _navigation(runs, visible_assets, visible_evals, visible_proposals)
    status, degraded_reason = _snapshot_status(objects, risk_control, comparison)

    return WorkbenchShellSnapshot(
        project_id=canonical_project_id,
        generated_at_utc=_utc_now_iso(),
        status=status,
        degraded=status != "ok",
        degraded_reason=degraded_reason,
        navigation=navigation,
        commands=commands,
        objects=tuple(objects[:24]),
        queue=queue,
        timeline=tuple(timeline[:30]),
        split_comparison=comparison,
        risk_control=risk_control,
        next_actions=tuple(command for command in commands if command.enabled)[:6],
    )


def _filter_proposals(
    proposals: list[WorkbenchProposal],
    project_asset_ids: set[str],
) -> list[WorkbenchProposal]:
    if not project_asset_ids:
        return []
    return [proposal for proposal in proposals if set(proposal.affected_assets) & project_asset_ids]


def _object_summaries(
    runs: list[WorkbenchRun],
    assets: list[WorkbenchAsset],
    evals: list[EvalResult],
    proposals: list[WorkbenchProposal],
) -> list[ShellObjectSummary]:
    rows = [
        ShellObjectSummary(
            object_id=run.run_id,
            object_kind="run",
            title=f"{run.kind.value} / {run.actor_agent_type.value}",
            status=run.status.value,
            view="mission-control",
            provenance_state="linked" if run.asset_revisions else "missing",
            risk_level=_run_risk(run),
            updated_at_utc=run.finished_at_utc or run.started_at_utc,
            why="Run is on the shell because it anchors queue, trace, eval, and asset continuity.",
        )
        for run in sorted(runs, key=lambda row: row.started_at_utc, reverse=True)
    ]
    rows.extend([
        ShellObjectSummary(
            object_id=asset.asset_id,
            object_kind="artifact",
            title=asset.name,
            status=asset.kind.value,
            view="evidence-notebooks",
            provenance_state="linked" if asset.provenance else "missing",
            risk_level="high" if asset.taints else "low",
            updated_at_utc=asset.created_at_utc,
            why="Artifact is shown with provenance so follow-up work keeps source context attached.",
        )
        for asset in sorted(assets, key=lambda row: row.created_at_utc, reverse=True)
    ])
    for eval_result in sorted(evals, key=lambda row: row.captured_at_utc, reverse=True):
        passed = all(score.passed for score in eval_result.scores)
        rows.append(
            ShellObjectSummary(
                object_id=eval_result.eval_id,
                object_kind="eval",
                title=eval_result.kind.value,
                status="passed" if passed else "failed",
                view="workbench-console",
                provenance_state="linked",
                risk_level="low" if passed else "high",
                updated_at_utc=eval_result.captured_at_utc,
                why="Eval is shown beside runs and artifacts to keep quality proof visible.",
            )
        )
    rows.extend([
        ShellObjectSummary(
            object_id=proposal.proposal_id,
            object_kind="proposal",
            title=proposal.kind.value,
            status=proposal.status.value,
            view="promotion-inbox",
            provenance_state="linked" if proposal.gate.provenance_present else "missing",
            risk_level=_proposal_risk(proposal),
            updated_at_utc=proposal.closed_at_utc or proposal.opened_at_utc,
            why="Proposal is surfaced because promotion choices need explicit proof and rollback context.",
        )
        for proposal in sorted(proposals, key=lambda row: row.opened_at_utc, reverse=True)
    ])
    return rows


def _queue_summary(runs: list[WorkbenchRun], leases: list[Any]) -> ShellQueueSummary:
    run_ids = {run.run_id for run in runs}
    project_leases = [lease for lease in leases if getattr(lease, "requested_for_run_id", "") in run_ids]
    active = [lease for lease in project_leases if _enum_value(getattr(lease, "status", "")) == "granted"]
    queued = [lease for lease in project_leases if _enum_value(getattr(lease, "status", "")) == "requested"]
    blocked = [run for run in runs if run.status.value == "blocked"]
    pressure = "red" if queued or blocked else ("amber" if active else "green")
    return ShellQueueSummary(
        active_count=len(active),
        queued_count=len(queued),
        blocked_count=len(blocked),
        lane_pressure=pressure,
        why="Queue state is derived from Workbench leases and blocked runs, not from client-side guesses.",
    )


def _timeline(
    runs: list[WorkbenchRun],
    assets: list[WorkbenchAsset],
    evals: list[EvalResult],
    proposals: list[WorkbenchProposal],
) -> list[ShellTimelineEvent]:
    events = [
        ShellTimelineEvent(
            event_id=f"run:{run.run_id}",
            object_kind="run",
            object_id=run.run_id,
            label=f"{run.kind.value} {run.status.value}",
            occurred_at_utc=run.finished_at_utc or run.started_at_utc,
            severity="error" if run.status.value in {"failed", "blocked"} else "info",
            why="Run lifecycle event from the Workbench spine.",
        )
        for run in runs
    ]
    events.extend([
        ShellTimelineEvent(
            event_id=f"asset:{asset.asset_id}",
            object_kind="artifact",
            object_id=asset.asset_id,
            label=f"{asset.kind.value} artifact captured",
            occurred_at_utc=asset.created_at_utc,
            severity="warning" if asset.taints else "info",
            why="Artifact event keeps provenance changes visible in time order.",
        )
        for asset in assets
    ])
    for eval_result in evals:
        passed = all(score.passed for score in eval_result.scores)
        events.append(
            ShellTimelineEvent(
                event_id=f"eval:{eval_result.eval_id}",
                object_kind="eval",
                object_id=eval_result.eval_id,
                label=f"{eval_result.kind.value} {'passed' if passed else 'failed'}",
                occurred_at_utc=eval_result.captured_at_utc,
                severity="info" if passed else "error",
                why="Eval event shows when proof changed the project state.",
            )
        )
    events.extend([
        ShellTimelineEvent(
            event_id=f"proposal:{proposal.proposal_id}",
            object_kind="proposal",
            object_id=proposal.proposal_id,
            label=f"{proposal.kind.value} proposal {proposal.status.value}",
            occurred_at_utc=proposal.closed_at_utc or proposal.opened_at_utc,
            severity="warning" if proposal.gate.blockers else "info",
            why="Promotion events keep approval context visible.",
        )
        for proposal in proposals
    ])
    return sorted(events, key=lambda row: row.occurred_at_utc, reverse=True)


def _split_comparison(runs: list[WorkbenchRun]) -> ShellSplitComparison:
    comparable = sorted(runs, key=lambda row: row.started_at_utc, reverse=True)[:2]
    if len(comparable) < 2:
        return ShellSplitComparison(
            left_object_id=comparable[0].run_id if comparable else None,
            right_object_id=None,
            basis="latest two Workbench runs",
            degraded=True,
            degraded_reason="At least two runs are required for split comparison.",
        )
    return ShellSplitComparison(
        left_object_id=comparable[0].run_id,
        right_object_id=comparable[1].run_id,
        basis="latest two Workbench runs",
        degraded=False,
        degraded_reason=None,
    )


def _risk_control(
    runs: list[WorkbenchRun],
    assets: list[WorkbenchAsset],
    proposals: list[WorkbenchProposal],
) -> ShellRiskControl:
    latest_run = max(runs, key=lambda row: row.started_at_utc, default=None)
    missing: list[str] = []
    cost_context = _cost_context(latest_run)
    if cost_context == "missing":
        missing.append("cost")
    provenance_context = "linked" if any(asset.provenance for asset in assets) else "missing"
    if provenance_context == "missing":
        missing.append("provenance")
    policy_context = _policy_context(proposals)
    if policy_context == "missing":
        missing.append("policy")
    hard_blockers = [proposal for proposal in proposals if proposal.gate.blockers]
    if hard_blockers:
        missing.append("policy_blockers")
    risk_level: RiskLevel = "blocked" if hard_blockers else ("high" if missing else "low")
    return ShellRiskControl(
        risk_level=risk_level,
        cost_context=cost_context,
        provenance_context=provenance_context,
        policy_context=policy_context,
        can_execute=not missing and not hard_blockers,
        approval_required=True,
        why=(
            "Actions are disabled until cost, provenance, policy context, and proposal gates are all clear."
            if missing
            else "Actions require explicit approval even when context is complete."
        ),
        missing=tuple(missing),
    )


def _commands(objects: list[ShellObjectSummary], risk: ShellRiskControl) -> tuple[ShellCommand, ...]:
    selected = objects[0] if objects else None
    return (
        ShellCommand(
            command_id="shell.open-object",
            label="Open selected Workbench object",
            view=selected.view if selected else "workbench-shell",
            object_kind=selected.object_kind if selected else "project",
            object_id=selected.object_id if selected else None,
            shortcut="Enter",
            enabled=selected is not None,
            requires_approval=False,
            why="Navigates to the current object without mutating Workbench state.",
            blocked_reason=None if selected is not None else "No Workbench object is available.",
        ),
        ShellCommand(
            command_id="shell.compare-runs",
            label="Compare latest runs",
            view="workbench-shell",
            object_kind="run",
            object_id=selected.object_id if selected and selected.object_kind == "run" else None,
            shortcut="C",
            enabled=len([row for row in objects if row.object_kind == "run"]) >= 2,
            requires_approval=False,
            why="Opens split comparison using the two latest run records.",
            blocked_reason=None
            if len([row for row in objects if row.object_kind == "run"]) >= 2
            else "At least two runs are required.",
        ),
        ShellCommand(
            command_id="shell.explain-risk",
            label="Explain cost, risk, and provenance",
            view="policy-explainability",
            object_kind=selected.object_kind if selected else "project",
            object_id=selected.object_id if selected else None,
            shortcut="?",
            enabled=True,
            requires_approval=False,
            why="Shows why the current action is allowed, blocked, or approval-gated.",
        ),
        ShellCommand(
            command_id="shell.promote-with-proof",
            label="Promote selected artifact with proof",
            view="promotion-inbox",
            object_kind=selected.object_kind if selected else "artifact",
            object_id=selected.object_id if selected else None,
            shortcut="P",
            enabled=risk.can_execute and selected is not None,
            requires_approval=True,
            why=risk.why,
            blocked_reason=None if risk.can_execute and selected is not None else risk.why,
        ),
    )


def _navigation(
    runs: list[WorkbenchRun],
    assets: list[WorkbenchAsset],
    evals: list[EvalResult],
    proposals: list[WorkbenchProposal],
) -> tuple[ShellNavigationItem, ...]:
    return (
        ShellNavigationItem("workbench-shell", "Shell", "project", 1, True, "Object-centered desktop overview."),
        ShellNavigationItem("mission-control", "Queue", "run", len(runs), False, "Scheduler and run queue."),
        ShellNavigationItem(
            "workbench-console", "Timeline", "trace", len(runs) + len(evals), False, "Run, trace, and eval history."
        ),
        ShellNavigationItem(
            "evidence-notebooks", "Artifacts", "artifact", len(assets), False, "Proof-backed artifacts."
        ),
        ShellNavigationItem(
            "promotion-inbox", "Proposals", "proposal", len(proposals), False, "Approval-sensitive promotion decisions."
        ),
    )


def _snapshot_status(
    objects: list[ShellObjectSummary],
    risk_control: ShellRiskControl,
    split_comparison: ShellSplitComparison,
) -> tuple[ShellStatus, str | None]:
    if not objects:
        return "empty", "No Workbench spine objects exist for this project yet."
    if risk_control.missing:
        return "degraded", f"Missing required context: {', '.join(risk_control.missing)}."
    if split_comparison.degraded:
        return "degraded", split_comparison.degraded_reason
    return "ok", None


def _run_risk(run: WorkbenchRun) -> RiskLevel:
    if run.status.value in {"failed", "blocked"}:
        return "high"
    if run.status.value in {"pending", "running"}:
        return "medium"
    return "low"


def _proposal_risk(proposal: WorkbenchProposal) -> RiskLevel:
    if proposal.gate.blockers:
        return "blocked"
    if proposal.status in {ProposalStatus.BLOCKED, ProposalStatus.REJECTED}:
        return "high"
    if not proposal.gate.rollback_plan_present or not proposal.gate.eval_present:
        return "high"
    return "low"


def _cost_context(run: WorkbenchRun | None) -> str:
    if run is None:
        return "missing"
    for metric in run.metrics:
        name = metric.name.lower()
        if any(token in name for token in ("cost", "token", "latency", "duration")):
            return f"{metric.name}={metric.value:g}{metric.unit}"
    return "missing"


def _policy_context(proposals: list[WorkbenchProposal]) -> str:
    if not proposals:
        return "missing"
    blocked = sum(1 for proposal in proposals if proposal.gate.blockers)
    if blocked:
        return f"{blocked} proposal gate blocker(s)"
    return "proposal gates linked"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _enum_value(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(getattr(value, "value", value))


def _jsonify(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    return value


__all__ = [
    "ShellCommand",
    "ShellNavigationItem",
    "ShellObjectSummary",
    "ShellQueueSummary",
    "ShellRiskControl",
    "ShellSplitComparison",
    "ShellTimelineEvent",
    "WorkbenchShellError",
    "WorkbenchShellSnapshot",
    "build_workbench_shell_snapshot",
]
