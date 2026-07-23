"""Runtime service for Workbench graph queries and saved views."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from vetinari.workbench.metadata_spine import WorkbenchSpine, WorkbenchSpineCorrupt, get_workbench_spine
from vetinari.workbench.query.models import (
    CrossObjectDiff,
    CrossObjectDiffRequest,
    DiffChange,
    GraphEdge,
    GraphNode,
    GraphQueryResult,
    GraphSnapshot,
    GraphViewId,
    QueryObjectKind,
    QueryRuntimeObject,
    SavedGraphView,
)
from vetinari.workbench.query.view_handlers import (
    _automation_churn_without_adoption,
    _failure_shared_source_revision,
    _impact_for,
    _node_presence_change,
    _node_value,
    _route_cost_without_quality_gain,
    _stale_evidence_blocked_promotions,
    _to_plain,
)
from vetinari.workbench.spine import validate_project_id

logger = logging.getLogger(__name__)


_SPINE_PROVENANCE = {"source": "vetinari.workbench.metadata_spine"}
_QUERY_PROVENANCE = {"source": "vetinari.workbench.query"}
_OBJECT_KINDS_REQUIRING_EXTRA_SOURCES = {
    QueryObjectKind.DATASET,
    QueryObjectKind.ANNOTATION,
    QueryObjectKind.DIAGNOSIS,
    QueryObjectKind.RECEIPT,
    QueryObjectKind.AUTOMATION,
    QueryObjectKind.MONITOR_SIGNAL,
}
_DIFF_DIMENSION_FIELDS = {
    "prompt": ("kind", "revision", "status", "metadata.prompt_hash"),
    "model": ("kind", "revision", "metadata.model_id", "metadata.model_version"),
    "dataset": ("kind", "revision", "metadata.dataset_revision"),
    "route": ("status", "metadata.route", "metadata.adapter", "metadata.cost_usd", "metadata.quality_score"),
    "policy": ("status", "metadata.policy_ref", "metadata.gate_blockers"),
    "recipe": ("revision", "metadata.recipe_id", "metadata.steps"),
}


class WorkbenchGraphQueryRejected(RuntimeError):
    """Raised when graph-query evidence, provenance, or authority is unavailable."""


def default_saved_views() -> tuple[SavedGraphView, ...]:
    """Return built-in saved views without reading or writing state."""
    return (
        SavedGraphView(
            view_id=GraphViewId.FAILURE_SHARED_SOURCE_REVISION,
            name="Failures sharing source revision",
            description="Failed or blocked runs grouped by common asset revision.",
            required_kinds=(QueryObjectKind.ASSET, QueryObjectKind.RUN),
            requires_authority=True,
        ),
        SavedGraphView(
            view_id=GraphViewId.STALE_EVIDENCE_BLOCKED_PROMOTIONS,
            name="Promotions blocked by stale evidence",
            description="Open or blocked proposals whose gate carries stale evidence blockers.",
            required_kinds=(QueryObjectKind.PROPOSAL, QueryObjectKind.EVAL),
            requires_authority=True,
        ),
        SavedGraphView(
            view_id=GraphViewId.ROUTE_COST_WITHOUT_QUALITY_GAIN,
            name="Routes costing more without quality gain",
            description="Comparable routes where cost rises but quality does not improve.",
            required_kinds=(QueryObjectKind.RUN,),
            requires_authority=True,
        ),
        SavedGraphView(
            view_id=GraphViewId.AUTOMATION_CHURN_WITHOUT_ADOPTION,
            name="Automations churning without adoption",
            description="Automation records with churn signals and weak adoption.",
            required_kinds=(QueryObjectKind.AUTOMATION,),
            requires_authority=True,
        ),
        SavedGraphView(
            view_id=GraphViewId.FULL_CROSS_OBJECT_GRAPH,
            name="Full cross-object graph",
            description="Every collected object and relationship in one operator graph.",
            required_kinds=(QueryObjectKind.ASSET, QueryObjectKind.RUN),
            requires_authority=True,
        ),
    )


class WorkbenchRuntimeGraphQueryService:
    """Read-only graph query service over WorkbenchSpine and runtime objects."""

    def __init__(
        self,
        *,
        spine: WorkbenchSpine | None = None,
        runtime_objects: Iterable[QueryRuntimeObject] = (),
        authority_ref: str = "workbench-spine",
    ) -> None:
        if not authority_ref or not authority_ref.strip():
            raise WorkbenchGraphQueryRejected("authority_ref is required")
        self._spine = spine
        self._runtime_objects = tuple(runtime_objects)
        self._authority_ref = authority_ref

    def snapshot(self, project_id: str = "default") -> GraphSnapshot:
        """Collect graph nodes and edges, failing closed on unreadable state.

        Returns:
            GraphSnapshot value produced by snapshot().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        project_id = validate_project_id(project_id)
        spine = self._spine if self._spine is not None else get_workbench_spine()
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []
        diagnostics: list[str] = []
        try:
            self._collect_spine_records(spine, project_id, nodes, edges)
        except (WorkbenchSpineCorrupt, OSError, ValueError) as exc:
            raise WorkbenchGraphQueryRejected(f"workbench graph source unavailable: {exc}") from exc

        for obj in self._runtime_objects:
            nodes.append(_runtime_node(obj))
            for relation, target_id in obj.relations:
                edge_id = f"{obj.kind.value}:{obj.object_id}->{relation}->{target_id}"
                edges.append(
                    GraphEdge(
                        edge_id=edge_id,
                        source_id=f"{obj.kind.value}:{obj.object_id}",
                        target_id=target_id,
                        relation=relation,
                        provenance=obj.provenance,
                        confidence=obj.confidence,
                    )
                )

        node_ids = {node.node_id for node in nodes}
        edges = [edge for edge in edges if edge.source_id in node_ids and edge.target_id in node_ids]
        if not nodes:
            raise WorkbenchGraphQueryRejected("workbench graph has no trusted objects")
        diagnostics.extend(
            f"{required_kind.value} source unavailable"
            for required_kind in _OBJECT_KINDS_REQUIRING_EXTRA_SOURCES
            if not any(node.kind is required_kind for node in nodes)
        )
        return GraphSnapshot(
            project_id=project_id,
            nodes=tuple(nodes),
            edges=tuple(edges),
            saved_views=default_saved_views(),
            authority_ref=self._authority_ref,
            generated_at_utc=datetime.now(timezone.utc).isoformat(),
            diagnostics=tuple(diagnostics),
        )

    def run_saved_view(self, view_id: GraphViewId | str, project_id: str = "default") -> GraphQueryResult:
        """Execute a built-in saved view over the current graph snapshot.

        Args:
            view_id: View id value consumed by run_saved_view().
            project_id: Project identifier that scopes the operation.

        Returns:
            Outcome produced by run_saved_view().
        """
        view = _resolve_view(view_id)
        snapshot = self.snapshot(project_id)
        self._assert_view_ready(snapshot, view)
        if view.view_id is GraphViewId.FAILURE_SHARED_SOURCE_REVISION:
            return _failure_shared_source_revision(snapshot)
        if view.view_id is GraphViewId.STALE_EVIDENCE_BLOCKED_PROMOTIONS:
            return _stale_evidence_blocked_promotions(snapshot)
        if view.view_id is GraphViewId.ROUTE_COST_WITHOUT_QUALITY_GAIN:
            return _route_cost_without_quality_gain(snapshot)
        if view.view_id is GraphViewId.AUTOMATION_CHURN_WITHOUT_ADOPTION:
            return _automation_churn_without_adoption(snapshot)
        return GraphQueryResult(
            view_id=view.view_id,
            matched_node_ids=tuple(node.node_id for node in snapshot.nodes),
            matched_edge_ids=tuple(edge.edge_id for edge in snapshot.edges),
            summary=f"{len(snapshot.nodes)} trusted objects across {len(snapshot.edges)} relationships.",
            requires_operator_review=bool(snapshot.diagnostics),
            blockers=snapshot.diagnostics,
        )

    def diff(self, request: CrossObjectDiffRequest) -> CrossObjectDiff:
        """Diff two trusted graph snapshots across named Workbench dimensions.

        Returns:
            CrossObjectDiff value produced by diff().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if request.before.authority_ref != request.after.authority_ref:
            raise WorkbenchGraphQueryRejected("diff snapshots must share one authority_ref")
        before_nodes = {node.node_id: node for node in request.before.nodes}
        after_nodes = {node.node_id: node for node in request.after.nodes}
        changes: list[DiffChange] = []
        for node_id in sorted(set(before_nodes) | set(after_nodes)):
            before = before_nodes.get(node_id)
            after = after_nodes.get(node_id)
            if before is None and after is not None:
                changes.append(_node_presence_change(after, None, "added"))
                continue
            if before is not None and after is None:
                changes.append(_node_presence_change(before, "removed", None))
                continue
            if before is None or after is None:
                continue
            for dimension in request.dimensions:
                fields = _DIFF_DIMENSION_FIELDS.get(dimension)
                if fields is None:
                    raise WorkbenchGraphQueryRejected(f"unsupported diff dimension {dimension!r}")
                for field_path in fields:
                    before_value = _node_value(before, field_path)
                    after_value = _node_value(after, field_path)
                    if before_value != after_value:
                        changes.append(
                            DiffChange(
                                node_id=node_id,
                                kind=after.kind,
                                field_path=field_path,
                                before=before_value,
                                after=after_value,
                                dimension=dimension,
                                impact=_impact_for(before, after, field_path),
                            )
                        )
        summary = f"{len(changes)} cross-object changes across {len(request.dimensions)} dimensions."
        return CrossObjectDiff(
            changes=tuple(changes),
            summary=summary,
            authority_ref=request.authority_ref,
            provenance=request.provenance,
            requires_operator_review=any(change.impact in {"blocker", "review"} for change in changes),
        )

    @staticmethod
    def _collect_spine_records(
        spine: WorkbenchSpine,
        project_id: str,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        assets = spine.list_assets()
        runs = [run for run in spine.list_runs() if run.project_id == project_id]
        project_run_ids = {run.run_id for run in runs}
        evals = [eval_result for eval_result in spine.list_evals() if eval_result.run_id in project_run_ids]
        proposals = [
            proposal
            for proposal in spine.list_proposals()
            if any(eval_result.run_id in project_run_ids for eval_result in proposal.pre_promotion_evals)
        ]
        leases = [lease for lease in spine.list_leases() if lease.requested_for_run_id in project_run_ids]
        traces = [trace for run in runs for trace in spine.list_traces_for_run(run.run_id)]
        _append_asset_nodes(nodes, assets)
        _append_run_nodes_and_edges(nodes, edges, runs)
        _append_trace_nodes_and_edges(nodes, edges, traces)
        _append_eval_nodes_and_edges(nodes, edges, evals)
        _append_proposal_nodes_and_edges(nodes, edges, proposals)
        _append_lease_nodes(nodes, leases)

    @staticmethod
    def _assert_view_ready(snapshot: GraphSnapshot, view: SavedGraphView) -> None:
        if view.requires_authority and not snapshot.authority_ref.strip():
            raise WorkbenchGraphQueryRejected("saved view requires authority_ref")
        kinds = {node.kind for node in snapshot.nodes}
        missing = [kind.value for kind in view.required_kinds if kind not in kinds]
        if missing:
            raise WorkbenchGraphQueryRejected(f"saved view {view.view_id.value} missing required sources: {missing}")
        weak = [node.node_id for node in snapshot.nodes if node.confidence < view.minimum_confidence]
        if weak:
            raise WorkbenchGraphQueryRejected(f"saved view {view.view_id.value} has weak-confidence nodes: {weak}")


WorkbenchGraphQueryService = WorkbenchRuntimeGraphQueryService


def _append_asset_nodes(nodes: list[GraphNode], assets: list[Any]) -> None:
    nodes.extend(
        GraphNode(
            node_id=f"asset:{asset.asset_id}",
            kind=QueryObjectKind.ASSET,
            label=asset.name,
            revision=asset.revision,
            status="blocked" if any(taint.severity == "blocker" for taint in asset.taints) else "active",
            metadata={
                "asset_id": asset.asset_id,
                "kind": asset.kind.value,
                "taints": [_to_plain(taint) for taint in asset.taints],
            },
            provenance=asset.provenance,
            confidence=1.0,
        )
        for asset in assets
    )


def _append_run_nodes_and_edges(nodes: list[GraphNode], edges: list[GraphEdge], runs: list[Any]) -> None:
    for run in runs:
        metrics = {metric.name: metric.value for metric in run.metrics}
        nodes.append(
            GraphNode(
                node_id=f"run:{run.run_id}",
                kind=QueryObjectKind.RUN,
                label=run.run_id,
                revision="",
                status=run.status.value,
                metadata={
                    "run_id": run.run_id,
                    "kind": run.kind.value,
                    "actor_agent_type": run.actor_agent_type.value,
                    "asset_revisions": [list(pair) for pair in run.asset_revisions],
                    "lease_id": run.lease_id,
                    "route": metrics.get("route"),
                    "cost_usd": metrics.get("cost_usd"),
                    "quality_score": metrics.get("quality_score"),
                    "metrics": metrics,
                },
                provenance=_SPINE_PROVENANCE,
                confidence=1.0,
            )
        )
        for asset_id, revision in run.asset_revisions:
            edges.append(_edge(f"run:{run.run_id}", f"asset:{asset_id}", f"uses_revision:{revision}"))
        if run.lease_id:
            edges.append(_edge(f"run:{run.run_id}", f"lease:{run.lease_id}", "uses_lease"))


def _append_trace_nodes_and_edges(nodes: list[GraphNode], edges: list[GraphEdge], traces: list[Any]) -> None:
    for trace in traces:
        error_count = sum(1 for span in trace.spans if span.error)
        nodes.append(
            GraphNode(
                node_id=f"trace:{trace.trace_id}",
                kind=QueryObjectKind.TRACE,
                label=trace.trace_id,
                revision="",
                status="failed" if error_count else "captured",
                metadata={
                    "run_id": trace.run_id,
                    "root_span_id": trace.root_span_id,
                    "span_count": len(trace.spans),
                    "error_count": error_count,
                },
                provenance=_SPINE_PROVENANCE,
                confidence=1.0,
            )
        )
        edges.append(_edge(f"trace:{trace.trace_id}", f"run:{trace.run_id}", "observes_run"))


def _append_eval_nodes_and_edges(nodes: list[GraphNode], edges: list[GraphEdge], evals: list[Any]) -> None:
    for eval_result in evals:
        nodes.append(
            GraphNode(
                node_id=f"eval:{eval_result.eval_id}",
                kind=QueryObjectKind.EVAL,
                label=eval_result.eval_id,
                revision=eval_result.asset_revision,
                status="passed" if all(score.passed for score in eval_result.scores) else "failed",
                metadata={"scores": [_to_plain(score) for score in eval_result.scores], "notes": eval_result.notes},
                provenance=_SPINE_PROVENANCE,
                confidence=1.0,
            )
        )
        edges.extend((
            _edge(f"eval:{eval_result.eval_id}", f"run:{eval_result.run_id}", "evaluates_run"),
            _edge(f"eval:{eval_result.eval_id}", f"asset:{eval_result.asset_id}", "evaluates_asset"),
        ))


def _append_proposal_nodes_and_edges(nodes: list[GraphNode], edges: list[GraphEdge], proposals: list[Any]) -> None:
    for proposal in proposals:
        nodes.append(
            GraphNode(
                node_id=f"proposal:{proposal.proposal_id}",
                kind=QueryObjectKind.PROPOSAL,
                label=proposal.proposal_id,
                revision=",".join(revision for _asset_id, revision in proposal.affected_revisions),
                status=proposal.status.value,
                metadata={
                    "kind": proposal.kind.value,
                    "affected_assets": list(proposal.affected_assets),
                    "gate_blockers": list(proposal.gate.blockers),
                    "gate": _to_plain(proposal.gate),
                    "notes": proposal.notes,
                },
                provenance=_SPINE_PROVENANCE,
                confidence=1.0,
            )
        )
        for asset_id, revision in proposal.affected_revisions:
            edges.append(_edge(f"proposal:{proposal.proposal_id}", f"asset:{asset_id}", f"changes_revision:{revision}"))
        edges.extend(
            _edge(f"proposal:{proposal.proposal_id}", f"eval:{eval_result.eval_id}", "gated_by_eval")
            for eval_result in proposal.pre_promotion_evals
        )


def _append_lease_nodes(nodes: list[GraphNode], leases: list[Any]) -> None:
    nodes.extend(
        GraphNode(
            node_id=f"lease:{lease.lease_id}",
            kind=QueryObjectKind.LEASE,
            label=lease.lease_id,
            revision="",
            status=lease.status.value,
            metadata=_to_plain(lease),
            provenance=_SPINE_PROVENANCE,
            confidence=1.0,
        )
        for lease in leases
    )


def build_workbench_graph_query_snapshot(
    project_id: str = "default",
    *,
    runtime_objects: Iterable[QueryRuntimeObject] = (),
    spine: WorkbenchSpine | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable graph snapshot for API/client callers.

    Returns:
        Newly constructed workbench graph query snapshot value.
    """
    service = WorkbenchGraphQueryService(spine=spine, runtime_objects=runtime_objects)
    return _to_plain(service.snapshot(project_id))


def _resolve_view(view_id: GraphViewId | str) -> SavedGraphView:
    view_enum = view_id if isinstance(view_id, GraphViewId) else GraphViewId(str(view_id))
    for view in default_saved_views():
        if view.view_id is view_enum:
            return view
    raise WorkbenchGraphQueryRejected(f"unknown saved view {view_id!r}")


def _runtime_node(obj: QueryRuntimeObject) -> GraphNode:
    return GraphNode(
        node_id=f"{obj.kind.value}:{obj.object_id}",
        kind=obj.kind,
        label=obj.label,
        revision=obj.revision,
        status=obj.status,
        metadata=dict(obj.metadata),
        provenance=obj.provenance,
        confidence=obj.confidence,
    )


def _edge(source_id: str, target_id: str, relation: str) -> GraphEdge:
    return GraphEdge(
        edge_id=f"{source_id}->{relation}->{target_id}",
        source_id=source_id,
        target_id=target_id,
        relation=relation,
        provenance=_QUERY_PROVENANCE,
        confidence=1.0,
    )
