"""Private saved-view handlers for Workbench graph queries."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from vetinari.workbench.query.models import (
    DiffChange,
    GraphNode,
    GraphQueryResult,
    GraphSnapshot,
    GraphViewId,
    QueryObjectKind,
)

logger = logging.getLogger(__name__)


def _failure_shared_source_revision(snapshot: GraphSnapshot) -> GraphQueryResult:
    by_revision: dict[tuple[str, str], list[str]] = {}
    for node in snapshot.nodes:
        if node.kind is not QueryObjectKind.RUN or node.status not in {"failed", "blocked"}:
            continue
        for asset_id, revision in node.metadata.get("asset_revisions", ()):
            by_revision.setdefault((str(asset_id), str(revision)), []).append(node.node_id)
    matched_nodes = sorted({node_id for runs in by_revision.values() if len(runs) > 1 for node_id in runs})
    return GraphQueryResult(
        GraphViewId.FAILURE_SHARED_SOURCE_REVISION,
        tuple(matched_nodes),
        _edge_ids_touching(snapshot, matched_nodes),
        f"{len(matched_nodes)} failed or blocked runs share a source revision.",
        bool(matched_nodes),
    )


def _stale_evidence_blocked_promotions(snapshot: GraphSnapshot) -> GraphQueryResult:
    matched = []
    blockers = []
    for node in snapshot.nodes:
        if node.kind is not QueryObjectKind.PROPOSAL:
            continue
        gate_blockers = [str(item) for item in node.metadata.get("gate_blockers", ())]
        stale = [item for item in gate_blockers if "stale" in item.lower() or "evidence" in item.lower()]
        if node.status in {"open", "blocked"} and stale:
            matched.append(node.node_id)
            blockers.extend(stale)
    return GraphQueryResult(
        GraphViewId.STALE_EVIDENCE_BLOCKED_PROMOTIONS,
        tuple(sorted(matched)),
        _edge_ids_touching(snapshot, matched),
        f"{len(matched)} promotions are blocked by stale or missing evidence.",
        bool(matched),
        blockers=tuple(sorted(set(blockers))),
    )


def _route_cost_without_quality_gain(snapshot: GraphSnapshot) -> GraphQueryResult:
    runs = [node for node in snapshot.nodes if node.kind is QueryObjectKind.RUN]
    matched: set[str] = set()
    for left in runs:
        for right in runs:
            if left.node_id == right.node_id:
                continue
            left_cost = _optional_float(left.metadata.get("cost_usd"))
            right_cost = _optional_float(right.metadata.get("cost_usd"))
            left_quality = _optional_float(left.metadata.get("quality_score"))
            right_quality = _optional_float(right.metadata.get("quality_score"))
            if (
                None not in {left_cost, right_cost, left_quality, right_quality}
                and right_cost > left_cost
                and right_quality <= left_quality
            ):
                matched.add(right.node_id)
    return GraphQueryResult(
        GraphViewId.ROUTE_COST_WITHOUT_QUALITY_GAIN,
        tuple(sorted(matched)),
        _edge_ids_touching(snapshot, matched),
        f"{len(matched)} route runs cost more without quality gain.",
        bool(matched),
    )


def _automation_churn_without_adoption(snapshot: GraphSnapshot) -> GraphQueryResult:
    matched = []
    for node in snapshot.nodes:
        if node.kind is not QueryObjectKind.AUTOMATION:
            continue
        churn = _optional_int(node.metadata.get("churn_count", 0))
        adoption = _optional_float(node.metadata.get("adoption_rate", 0.0))
        if churn is not None and adoption is not None and churn >= 3 and adoption < 0.2:
            matched.append(node.node_id)
    return GraphQueryResult(
        GraphViewId.AUTOMATION_CHURN_WITHOUT_ADOPTION,
        tuple(sorted(matched)),
        _edge_ids_touching(snapshot, matched),
        f"{len(matched)} automations show churn without adoption.",
        bool(matched),
    )


def _edge_ids_touching(snapshot: GraphSnapshot, node_ids: Iterable[str]) -> tuple[str, ...]:
    selected = set(node_ids)
    return tuple(edge.edge_id for edge in snapshot.edges if edge.source_id in selected or edge.target_id in selected)


def _node_presence_change(node: GraphNode, before: Any, after: Any) -> DiffChange:
    status = "added" if before is None else "removed"
    return DiffChange(
        node.node_id, node.kind, "$presence", before, after, "presence", "review" if status == "added" else "blocker"
    )


def _node_value(node: GraphNode, field_path: str) -> Any:
    if field_path == "kind":
        return node.kind.value
    if field_path == "revision":
        return node.revision
    if field_path == "status":
        return node.status
    if field_path.startswith("metadata."):
        value: Any = node.metadata
        for part in field_path.split(".")[1:]:
            if not isinstance(value, dict):
                return None
            value = value.get(part)
        return value
    return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None


def _impact_for(before: GraphNode, after: GraphNode, field_path: str) -> str:
    if after.status in {"blocked", "failed", "rejected"} and before.status != after.status:
        return "blocker"
    if field_path in {"metadata.policy_ref", "metadata.gate_blockers", "metadata.route"}:
        return "review"
    return "info"


def _to_plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _to_plain(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _to_plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_to_plain(item) for item in value]
    if isinstance(value, list):
        return [_to_plain(item) for item in value]
    return value
