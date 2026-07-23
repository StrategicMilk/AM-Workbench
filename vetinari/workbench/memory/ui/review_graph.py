"""Fail-closed memory review graph for AM Workbench.

The graph is a read-only product surface over memory lineage payloads. It
requires verified lineage plus explicit review state so the UI cannot present a
memory as trusted when provenance, confidence, authority, or export-boundary
state is absent.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from vetinari.ux import display_label, display_label_or_humanize
from vetinari.workbench.memory.spine import (
    MemoryLineageError,
    MemoryLineageInspector,
    MemorySpineAuthorityTier,
    MemoryUsageOutcome,
    MemoryUsageTelemetry,
    memory_lineage_to_payload,
    validate_memory_payload,
)


class MemoryReviewGraphError(ValueError):
    """Raised when the memory review graph cannot be trusted."""


@dataclass(frozen=True, slots=True)
class ReviewGraphFilters:
    """Filters applied by the UI and API."""

    min_confidence: float = 0.0
    authority: tuple[str, ...] = ()
    include_quarantined: bool = False
    queue: str = "all"

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_confidence <= 1.0:
            raise MemoryReviewGraphError("min_confidence must be between 0.0 and 1.0")
        if self.queue not in {"all", "stale", "conflict", "quarantine", "export_blocked"}:
            raise MemoryReviewGraphError("queue must be all, stale, conflict, quarantine, or export_blocked")

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_confidence": self.min_confidence,
            "authority": list(self.authority),
            "include_quarantined": self.include_quarantined,
            "queue": self.queue,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ReviewGraphFilters(min_confidence={self.min_confidence!r}, authority={self.authority!r}, include_quarantined={self.include_quarantined!r})"


@dataclass(frozen=True, slots=True)
class MemoryReviewNode:
    """One inspectable memory node."""

    memory_id: str
    label: str
    validation_state: str
    authority_tier: str
    confidence: float
    stale: bool
    conflicts: tuple[str, ...]
    quarantined: bool
    export_boundary: dict[str, Any]
    why_memory_exists: str
    why_recalled: tuple[dict[str, Any], ...]
    where_used: tuple[dict[str, Any], ...]
    actions: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "label": self.label,
            "validation_state": self.validation_state,
            "validation_state_label": display_label_or_humanize(self.validation_state),
            "authority_tier": self.authority_tier,
            "authority_tier_label": display_label_or_humanize(self.authority_tier),
            "confidence": self.confidence,
            "stale": self.stale,
            "conflicts": list(self.conflicts),
            "quarantined": self.quarantined,
            "export_boundary": dict(self.export_boundary),
            "why_memory_exists": self.why_memory_exists,
            "why_recalled": [dict(row) for row in self.why_recalled],
            "where_used": [dict(row) for row in self.where_used],
            "actions": list(self.actions),
            "action_labels": [display_label(action) for action in self.actions],
            "evidence_refs": list(self.evidence_refs),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryReviewNode(memory_id={self.memory_id!r}, label={self.label!r}, validation_state={self.validation_state!r})"


@dataclass(frozen=True, slots=True)
class MemoryReviewEdge:
    """One graph edge from memory to proof, use, conflict, or boundary state."""

    edge_id: str
    source: str
    target: str
    kind: str
    label: str
    confidence: float
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "kind_label": display_label(self.kind),
            "label": self.label,
            "confidence": self.confidence,
            "evidence_refs": list(self.evidence_refs),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryReviewEdge(edge_id={self.edge_id!r}, source={self.source!r}, target={self.target!r})"


@dataclass(frozen=True, slots=True)
class MemoryReviewQueues:
    """Review queues derived from node state."""

    stale: tuple[str, ...]
    conflict: tuple[str, ...]
    quarantine: tuple[str, ...]
    export_blocked: tuple[str, ...]

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "stale": list(self.stale),
            "conflict": list(self.conflict),
            "quarantine": list(self.quarantine),
            "export_blocked": list(self.export_blocked),
            "labels": {
                "stale": display_label("stale"),
                "conflict": display_label("conflict"),
                "quarantine": display_label("quarantine"),
                "export_blocked": display_label("export_blocked"),
            },
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryReviewQueues(stale={self.stale!r}, conflict={self.conflict!r}, quarantine={self.quarantine!r})"


@dataclass(frozen=True, slots=True)
class MemoryReviewGraph:
    """Validated graph payload consumed by the Workbench UI."""

    project_id: str
    filters: ReviewGraphFilters
    nodes: tuple[MemoryReviewNode, ...]
    edges: tuple[MemoryReviewEdge, ...]
    queues: MemoryReviewQueues
    fail_closed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "filters": self.filters.to_dict(),
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
            "queues": self.queues.to_dict(),
            "fail_closed": self.fail_closed,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryReviewGraph(project_id={self.project_id!r}, filters={self.filters!r}, nodes={self.nodes!r})"


_REQUIRED_REVIEW_KEYS = frozenset({
    "confidence",
    "stale",
    "conflicts",
    "quarantined",
    "export_boundary",
    "actions",
})
_ALLOWED_ACTIONS = frozenset({"correct", "supersede", "delete", "quarantine", "release_export"})


def build_memory_review_graph(
    payloads: Iterable[dict[str, Any]],
    *,
    project_id: str = "default",
    filters: ReviewGraphFilters | None = None,
) -> MemoryReviewGraph:
    """Build a memory review graph from verified lineage payloads.

    Raises:
            MemoryReviewGraphError: If no verified payloads are available or any
                payload lacks required review, authority, confidence, or export
                boundary state.

    Returns:
        Newly constructed memory review graph value.
    """
    resolved_filters = filters or ReviewGraphFilters()
    nodes: list[MemoryReviewNode] = []
    edges: list[MemoryReviewEdge] = []

    for payload in payloads:
        node, node_edges = _node_from_payload(payload, project_id=project_id)
        if not _matches_filters(node, resolved_filters):
            continue
        nodes.append(node)
        edges.extend(node_edges)

    if not nodes:
        raise MemoryReviewGraphError("no verified memory review nodes matched the requested filters")

    queues = MemoryReviewQueues(
        stale=tuple(node.memory_id for node in nodes if node.stale),
        conflict=tuple(node.memory_id for node in nodes if node.conflicts),
        quarantine=tuple(node.memory_id for node in nodes if node.quarantined),
        export_blocked=tuple(node.memory_id for node in nodes if node.export_boundary.get("allowed") is not True),
    )
    return MemoryReviewGraph(
        project_id=project_id,
        filters=resolved_filters,
        nodes=tuple(nodes),
        edges=tuple(edges),
        queues=queues,
    )


def _node_from_payload(
    payload: dict[str, Any], *, project_id: str
) -> tuple[MemoryReviewNode, tuple[MemoryReviewEdge, ...]]:
    try:
        record = validate_memory_payload(payload)
    except MemoryLineageError as exc:
        raise MemoryReviewGraphError(f"memory lineage unavailable: {exc}") from exc
    if record.provenance.get("project_id") != project_id:
        raise MemoryReviewGraphError("memory lineage project scope does not match requested project")

    usages = _usage_rows(payload, expected_memory_id=record.memory_id)
    review_state = _review_state(payload)
    confidence = _confidence(review_state)
    export_boundary = _export_boundary(review_state)
    actions = _actions(review_state)
    conflicts = _string_tuple(review_state["conflicts"], "review_state.conflicts")

    explanation = MemoryLineageInspector().explain(record, usages)
    lineage_payload = memory_lineage_to_payload(record, usages)
    authority = lineage_payload.get("authority")
    if not isinstance(authority, dict) or authority.get("memory_tier") != "memory":
        raise MemoryReviewGraphError("authority state must keep memory at the memory tier")

    evidence_refs = _evidence_refs(lineage_payload, usages)
    node = MemoryReviewNode(
        memory_id=record.memory_id,
        label=str(review_state.get("label") or record.provenance["reason"]),
        validation_state=record.validation_state.value,
        authority_tier="memory",
        confidence=confidence,
        stale=_bool(review_state["stale"], "review_state.stale"),
        conflicts=conflicts,
        quarantined=_bool(review_state["quarantined"], "review_state.quarantined"),
        export_boundary=export_boundary,
        why_memory_exists=str(explanation["why_memory_exists"]),
        why_recalled=tuple(dict(row) for row in explanation["why_recalled"]),
        where_used=tuple(_where_used(usages)),
        actions=actions,
        evidence_refs=evidence_refs,
    )
    return node, _edges_for(record.memory_id, usages, evidence_refs, conflicts, export_boundary, confidence)


def _usage_rows(payload: dict[str, Any], *, expected_memory_id: str) -> tuple[MemoryUsageTelemetry, ...]:
    usage_payloads = payload.get("usage_telemetry")
    if not isinstance(usage_payloads, list) or not usage_payloads:
        raise MemoryReviewGraphError("usage_telemetry is required for why-recalled and where-used review")
    rows: list[MemoryUsageTelemetry] = []
    for row in usage_payloads:
        if not isinstance(row, dict):
            raise MemoryReviewGraphError("usage_telemetry rows must be objects")
        try:
            usage = MemoryUsageTelemetry(
                usage_id=str(row["usage_id"]),
                memory_id=str(row["memory_id"]),
                recalled_for_run_id=str(row["recalled_for_run_id"]),
                recalled_at_utc=str(row["recalled_at_utc"]),
                recall_reason=str(row["recall_reason"]),
                used=_bool(row["used"], "usage_telemetry.used"),
                outcome=MemoryUsageOutcome(str(row["outcome"])),
                helped_harmed_classification=str(row["helped_harmed_classification"]),
                authority_tier=MemorySpineAuthorityTier[str(row["authority_tier"]).upper()],
                evidence_refs=_string_tuple(row["evidence_refs"], "usage_telemetry.evidence_refs"),
                notes=str(row.get("notes", "")),
            )
        except (KeyError, ValueError, MemoryLineageError) as exc:
            raise MemoryReviewGraphError(f"invalid usage telemetry: {exc}") from exc
        if usage.memory_id != expected_memory_id:
            raise MemoryReviewGraphError("usage telemetry references a different memory_id")
        rows.append(usage)
    return tuple(rows)


def _review_state(payload: dict[str, Any]) -> dict[str, Any]:
    review_state = payload.get("review_state")
    if not isinstance(review_state, dict):
        raise MemoryReviewGraphError("review_state is required")
    missing = sorted(_REQUIRED_REVIEW_KEYS - set(review_state))
    if missing:
        raise MemoryReviewGraphError(f"review_state missing required keys: {missing}")
    return review_state


def _confidence(review_state: dict[str, Any]) -> float:
    raw = review_state["confidence"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise MemoryReviewGraphError("review_state.confidence must be a number")
    confidence = float(raw)
    if not 0.0 <= confidence <= 1.0:
        raise MemoryReviewGraphError("review_state.confidence must be between 0.0 and 1.0")
    return confidence


def _export_boundary(review_state: dict[str, Any]) -> dict[str, Any]:
    boundary = review_state["export_boundary"]
    if not isinstance(boundary, dict):
        raise MemoryReviewGraphError("review_state.export_boundary must be an object")
    if not isinstance(boundary.get("allowed"), bool):
        raise MemoryReviewGraphError("review_state.export_boundary.allowed must be bool")
    reasons = _string_tuple(boundary.get("reasons"), "review_state.export_boundary.reasons")
    return {"allowed": boundary["allowed"], "reasons": list(reasons)}


def _actions(review_state: dict[str, Any]) -> tuple[str, ...]:
    actions = _string_tuple(review_state["actions"], "review_state.actions")
    unknown = sorted(set(actions) - _ALLOWED_ACTIONS)
    if unknown:
        raise MemoryReviewGraphError(f"unknown review actions: {unknown}")
    return actions


def _edges_for(
    memory_id: str,
    usages: tuple[MemoryUsageTelemetry, ...],
    evidence_refs: tuple[str, ...],
    conflicts: tuple[str, ...],
    export_boundary: dict[str, Any],
    confidence: float,
) -> tuple[MemoryReviewEdge, ...]:
    edges = [
        MemoryReviewEdge(
            edge_id=f"{memory_id}->evidence:{ref}",
            source=memory_id,
            target=ref,
            kind="evidenced_by",
            label="evidence",
            confidence=confidence,
            evidence_refs=(ref,),
        )
        for ref in evidence_refs
    ]
    edges.extend(
        MemoryReviewEdge(
            edge_id=f"{memory_id}->usage:{usage.usage_id}",
            source=memory_id,
            target=usage.recalled_for_run_id,
            kind="used_by_run" if usage.used else "recalled_by_run",
            label=usage.outcome.value,
            confidence=confidence,
            evidence_refs=usage.evidence_refs,
        )
        for usage in usages
    )
    edges.extend(
        MemoryReviewEdge(
            edge_id=f"{memory_id}->conflict:{conflict}",
            source=memory_id,
            target=conflict,
            kind="conflicts_with",
            label="conflict",
            confidence=confidence,
            evidence_refs=evidence_refs,
        )
        for conflict in conflicts
    )
    if export_boundary.get("allowed") is not True:
        edges.append(
            MemoryReviewEdge(
                edge_id=f"{memory_id}->export-boundary",
                source=memory_id,
                target="export-boundary",
                kind="blocked_by_export_boundary",
                label="export blocked",
                confidence=confidence,
                evidence_refs=evidence_refs,
            )
        )
    return tuple(edges)


def _where_used(usages: tuple[MemoryUsageTelemetry, ...]) -> list[dict[str, Any]]:
    return [
        {
            "usage_id": usage.usage_id,
            "run_id": usage.recalled_for_run_id,
            "used": usage.used,
            "outcome": usage.outcome.value,
            "classification": usage.helped_harmed_classification,
            "evidence_refs": list(usage.evidence_refs),
        }
        for usage in usages
    ]


def _evidence_refs(lineage_payload: dict[str, Any], usages: tuple[MemoryUsageTelemetry, ...]) -> tuple[str, ...]:
    refs: list[str] = []
    for key in ("evidence_asset_ids", "eval_ids", "receipt_ids", "prompt_injection_ids"):
        refs.extend(str(ref) for ref in lineage_payload.get(key, ()))
    for usage in usages:
        refs.extend(usage.evidence_refs)
    deduped = tuple(dict.fromkeys(ref for ref in refs if ref.strip()))
    if not deduped:
        raise MemoryReviewGraphError("at least one evidence reference is required")
    return deduped


def _matches_filters(node: MemoryReviewNode, filters: ReviewGraphFilters) -> bool:
    if node.confidence < filters.min_confidence:
        return False
    if filters.authority and node.authority_tier not in filters.authority:
        return False
    if filters.queue == "stale":
        return node.stale
    if filters.queue == "conflict":
        return bool(node.conflicts)
    if filters.queue == "quarantine":
        return node.quarantined
    if filters.queue == "export_blocked":
        return node.export_boundary.get("allowed") is not True
    return not (node.quarantined and not filters.include_quarantined)


def _string_tuple(values: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise MemoryReviewGraphError(f"{field_name} must be a list")
    result = tuple(str(value) for value in values if str(value).strip())
    if not result:
        raise MemoryReviewGraphError(f"{field_name} must include at least one non-empty string")
    return result


def _bool(value: Any, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise MemoryReviewGraphError(f"{field_name} must be bool")
    return value
