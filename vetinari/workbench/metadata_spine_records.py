"""Record conversion helpers for the Workbench metadata spine."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.agents.contracts import AttestedArtifact, LLMJudgment, OutcomeSignal, Provenance, ToolEvidence
from vetinari.types import AgentType, ArtifactKind, EvidenceBasis, ShardKind
from vetinari.workbench.assets import AssetKind, AssetTaint, WorkbenchAsset
from vetinari.workbench.evals import EvalKind, EvalResult, EvalScore
from vetinari.workbench.leases import LeaseStatus, WorkbenchLease
from vetinari.workbench.proposals import (
    Promotion,
    ProposalGate,
    ProposalStatus,
    WorkbenchProposal,
    WorkbenchProposalKind,
)
from vetinari.workbench.runs import RunKind, RunMetric, RunStatus, WorkbenchRun
from vetinari.workbench.traces import TraceSpan, WorkbenchTrace

# SPINE_RECORD_SCHEMA_VERSION must be incremented whenever a new field is added to any record kind.
SPINE_RECORD_SCHEMA_VERSION: int = 1


class WorkbenchSpineCorrupt(Exception):
    """Raised when the spine cannot be trusted or safely mutated."""

    def __init__(self, reason: str, *, path: Path | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.path = path

    def __str__(self) -> str:
        if self.path is None:
            return f"WorkbenchSpineCorrupt: {self.reason}"
        return f"WorkbenchSpineCorrupt: {self.reason} (path={self.path})"


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    return value


def record_to_spine_entry(kind: str, record: Any) -> dict[str, Any]:
    """Convert a typed record into the JSONL spine entry envelope."""
    return {"kind": kind, "schema_version": SPINE_RECORD_SCHEMA_VERSION, "payload": _to_jsonable(record)}


def _payload_to_record(kind: str, payload: dict[str, Any]) -> Any:
    """Convert a raw payload dict to the corresponding typed record.

    Args:
        kind: Record kind string (e.g. ``"asset"``, ``"run"``).
        payload: Deserialized payload dict from the spine JSONL envelope.

    Returns:
        The typed record corresponding to ``kind``.

    Raises:
        WorkbenchSpineCorrupt: When a required key is absent from the payload
            or when ``kind`` is not recognised.  Raw ``KeyError`` from missing
            fields is caught at the boundary and re-raised as the typed error
            so callers always receive a predictable damaged-state exception.
    """
    try:
        return _payload_to_record_inner(kind, payload)
    except KeyError as exc:
        raise WorkbenchSpineCorrupt(f"payload for kind {kind!r} is missing required field {exc}") from exc


def _payload_to_record_inner(kind: str, payload: dict[str, Any]) -> Any:
    if kind == "asset":
        return WorkbenchAsset(
            asset_id=payload["asset_id"],
            kind=AssetKind(payload["kind"]),
            name=payload["name"],
            revision=payload["revision"],
            created_at_utc=payload["created_at_utc"],
            taints=tuple(_asset_taint_from_payload(row) for row in payload.get("taints", ())),
            provenance=dict(payload.get("provenance", {})),
        )
    if kind == "run":
        return WorkbenchRun(
            run_id=payload["run_id"],
            kind=RunKind(payload["kind"]),
            status=RunStatus(payload["status"]),
            started_at_utc=payload["started_at_utc"],
            finished_at_utc=payload["finished_at_utc"],
            actor_agent_type=AgentType(payload["actor_agent_type"]),
            asset_revisions=tuple(tuple(row) for row in payload.get("asset_revisions", ())),
            lease_id=payload.get("lease_id", ""),
            shard_kind=ShardKind(payload["shard_kind"]) if payload.get("shard_kind") else None,
            metrics=tuple(_run_metric_from_payload(row) for row in payload.get("metrics", ())),
            outcome=_outcome_from_payload(payload.get("outcome")),
            project_id=payload["project_id"],
        )
    if kind == "trace":
        return WorkbenchTrace(
            trace_id=payload["trace_id"],
            run_id=payload["run_id"],
            root_span_id=payload["root_span_id"],
            spans=tuple(_trace_span_from_payload(row) for row in payload["spans"]),
            captured_at_utc=payload["captured_at_utc"],
        )
    if kind == "eval":
        return _eval_from_payload(payload)
    if kind == "proposal":
        return WorkbenchProposal(
            proposal_id=payload["proposal_id"],
            kind=WorkbenchProposalKind(payload["kind"]),
            status=ProposalStatus(payload["status"]),
            affected_assets=tuple(payload["affected_assets"]),
            affected_revisions=tuple(tuple(row) for row in payload["affected_revisions"]),
            pre_promotion_evals=tuple(_eval_from_payload(row) for row in payload.get("pre_promotion_evals", ())),
            gate=_proposal_gate_from_payload(payload["gate"]),
            attached_outcome=None,
            opened_at_utc=payload["opened_at_utc"],
            closed_at_utc=payload["closed_at_utc"],
            notes=payload.get("notes", ""),
        )
    if kind == "lease":
        from vetinari.runtime.workbench_scheduler import Lane

        return WorkbenchLease(
            lease_id=payload["lease_id"],
            lane=Lane(payload["lane"]),
            status=LeaseStatus(payload["status"]),
            lease_handle=payload["lease_handle"],
            granted_at_utc=payload["granted_at_utc"],
            released_at_utc=payload["released_at_utc"],
            requested_for_run_id=payload["requested_for_run_id"],
            vram_share=float(payload["vram_share"]),
        )
    if kind == "promotion":
        return Promotion(
            promotion_id=payload["promotion_id"],
            proposal_id=payload["proposal_id"],
            accepted=bool(payload["accepted"]),
            decided_at_utc=payload["decided_at_utc"],
            decided_by=payload["decided_by"],
            rationale=payload["rationale"],
        )
    raise WorkbenchSpineCorrupt(f"unknown record kind {kind!r}")


def _asset_taint_from_payload(payload: dict[str, Any]) -> AssetTaint:
    return AssetTaint(
        taint_id=payload["taint_id"],
        severity=payload["severity"],
        reason=payload["reason"],
        attached_at_utc=payload["attached_at_utc"],
    )


def _run_metric_from_payload(payload: dict[str, Any]) -> RunMetric:
    return RunMetric(name=payload["name"], value=float(payload["value"]), unit=payload.get("unit", ""))


def _outcome_from_payload(payload: dict[str, Any] | None) -> OutcomeSignal | None:
    if not payload:
        return None
    provenance_payload = payload.get("provenance")
    return OutcomeSignal(
        passed=bool(payload.get("passed")),
        score=float(payload.get("score", 0.0)),
        basis=EvidenceBasis(payload.get("basis", EvidenceBasis.UNSUPPORTED.value)),
        tool_evidence=tuple(ToolEvidence(**row) for row in payload.get("tool_evidence", ())),
        llm_judgment=LLMJudgment(**payload["llm_judgment"]) if payload.get("llm_judgment") else None,
        attested_artifacts=tuple(_attested_from_payload(row) for row in payload.get("attested_artifacts", ())),
        provenance=Provenance(**provenance_payload) if provenance_payload else None,
        issues=tuple(payload.get("issues", ())),
        suggestions=tuple(payload.get("suggestions", ())),
        use_case=payload.get("use_case"),
        kind=ShardKind(payload.get("kind", ShardKind.STANDARD.value)),
    )


def _attested_from_payload(payload: dict[str, Any]) -> AttestedArtifact:
    return AttestedArtifact(
        kind=ArtifactKind(payload["kind"]),
        attested_by=payload["attested_by"],
        attested_at_utc=payload["attested_at_utc"],
        payload=dict(payload.get("payload", {})),
    )


def _trace_span_from_payload(payload: dict[str, Any]) -> TraceSpan:
    return TraceSpan(
        span_id=payload["span_id"],
        parent_span_id=payload.get("parent_span_id"),
        tool_name=payload["tool_name"],
        started_at_utc=payload["started_at_utc"],
        finished_at_utc=payload["finished_at_utc"],
        inputs_hash=payload.get("inputs_hash", ""),
        outputs_hash=payload.get("outputs_hash", ""),
        error=payload.get("error", ""),
        duration_ms=int(payload["duration_ms"]),
    )


def _eval_from_payload(payload: dict[str, Any]) -> EvalResult:
    return EvalResult(
        eval_id=payload["eval_id"],
        kind=EvalKind(payload["kind"]),
        run_id=payload["run_id"],
        asset_id=payload["asset_id"],
        asset_revision=payload["asset_revision"],
        scores=tuple(_eval_score_from_payload(row) for row in payload["scores"]),
        captured_at_utc=payload["captured_at_utc"],
        notes=payload.get("notes", ""),
    )


def _eval_score_from_payload(payload: dict[str, Any]) -> EvalScore:
    return EvalScore(
        metric_name=payload["metric_name"],
        value=float(payload["value"]),
        threshold=float(payload["threshold"]),
        passed=bool(payload["passed"]),
        unit=payload.get("unit", ""),
    )


def _proposal_gate_from_payload(payload: dict[str, Any]) -> ProposalGate:
    return ProposalGate(
        provenance_present=bool(payload["provenance_present"]),
        eval_present=bool(payload["eval_present"]),
        rollback_plan_present=bool(payload["rollback_plan_present"]),
        blockers=tuple(payload.get("blockers", ())),
    )
