"""JSONL serialization helpers for work receipts."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from vetinari.agents.contracts import AttestedArtifact, LLMJudgment, OutcomeSignal, Provenance, ToolEvidence
from vetinari.receipts.record import WorkReceipt, WorkReceiptKind
from vetinari.types import AgentType, ArtifactKind, EvidenceBasis, ShardKind

WORK_RECEIPT_SCHEMA_VERSION = 1


def receipt_to_jsonl(receipt: WorkReceipt) -> str:
    """Serialise a WorkReceipt to a single-line JSON string.

    Args:
        receipt: The receipt to serialise.

    Returns:
        Single-line JSON string without a trailing newline.
    """
    raw: dict[str, Any] = {
        "schema_version": WORK_RECEIPT_SCHEMA_VERSION,
        "receipt_id": receipt.receipt_id,
        "project_id": receipt.project_id,
        "agent_id": receipt.agent_id,
        "agent_type": receipt.agent_type.value,
        "kind": receipt.kind.value,
        "started_at_utc": receipt.started_at_utc,
        "finished_at_utc": receipt.finished_at_utc,
        "inputs_summary": receipt.inputs_summary,
        "outputs_summary": receipt.outputs_summary,
        "outcome": outcome_to_dict(receipt.outcome),
        "awaiting_user": receipt.awaiting_user,
        "awaiting_reason": receipt.awaiting_reason,
        "linked_claim_ids": list(receipt.linked_claim_ids),
    }
    return json.dumps(raw, separators=(",", ":"))


def outcome_to_dict(outcome: OutcomeSignal) -> dict[str, Any]:
    """Convert an OutcomeSignal with nested frozen dataclasses to a dict.

    Args:
        outcome: Signal to serialize.

    Returns:
        JSON-friendly signal mapping.
    """
    return {
        "passed": outcome.passed,
        "score": outcome.score,
        "basis": outcome.basis.value,
        "tool_evidence": [asdict(te) for te in outcome.tool_evidence],
        "llm_judgment": asdict(outcome.llm_judgment) if outcome.llm_judgment is not None else None,
        "attested_artifacts": [attested_to_dict(a) for a in outcome.attested_artifacts],
        "provenance": asdict(outcome.provenance) if outcome.provenance is not None else None,
        "issues": list(outcome.issues),
        "suggestions": list(outcome.suggestions),
        "use_case": outcome.use_case,
        "kind": outcome.kind.value,
    }


def attested_to_dict(artifact: AttestedArtifact) -> dict[str, Any]:
    """Convert an AttestedArtifact to a JSON-friendly dict.

    Args:
        artifact: Artifact to serialize.

    Returns:
        JSON-friendly artifact mapping.
    """
    return {
        "kind": artifact.kind.value,
        "attested_by": artifact.attested_by,
        "attested_at_utc": artifact.attested_at_utc,
        "payload": dict(artifact.payload),
    }


def receipt_from_jsonl(line: str) -> WorkReceipt:
    """Deserialise one JSONL line into a WorkReceipt.

    Args:
    line: The raw JSON line without a trailing newline.

    Returns:
    The reconstructed WorkReceipt.

    Raises:
        ValueError: Propagated when validation, persistence, or execution fails.
    """
    raw: dict[str, Any] = json.loads(line)
    schema_version = int(raw.get("schema_version", WORK_RECEIPT_SCHEMA_VERSION))
    if schema_version != WORK_RECEIPT_SCHEMA_VERSION:
        raise ValueError(f"unsupported WorkReceipt schema_version {schema_version!r}")
    return WorkReceipt(
        receipt_id=raw["receipt_id"],
        project_id=raw["project_id"],
        agent_id=raw["agent_id"],
        agent_type=AgentType(raw["agent_type"]),
        kind=WorkReceiptKind(raw["kind"]),
        started_at_utc=raw["started_at_utc"],
        finished_at_utc=raw["finished_at_utc"],
        inputs_summary=raw.get("inputs_summary", ""),
        outputs_summary=raw.get("outputs_summary", ""),
        outcome=outcome_from_dict(raw["outcome"]),
        awaiting_user=bool(raw.get("awaiting_user")),
        awaiting_reason=raw.get("awaiting_reason"),
        linked_claim_ids=tuple(raw.get("linked_claim_ids", ())),
    )


def outcome_from_dict(raw: dict[str, Any]) -> OutcomeSignal:
    """Reconstruct an OutcomeSignal from a JSON dict.

    Args:
        raw: JSON-friendly signal mapping.

    Returns:
        OutcomeSignal instance.
    """
    tool_evidence = tuple(ToolEvidence(**te) for te in raw.get("tool_evidence", ()))
    llm_raw = raw.get("llm_judgment")
    llm_judgment = LLMJudgment(**llm_raw) if llm_raw is not None else None
    attested = tuple(attested_from_dict(a) for a in raw.get("attested_artifacts", ()))
    prov_raw = raw.get("provenance")
    provenance = Provenance(**prov_raw) if prov_raw is not None else None
    return OutcomeSignal(
        passed=bool(raw.get("passed")),
        score=float(raw.get("score", 0.0)),
        basis=EvidenceBasis(raw.get("basis", EvidenceBasis.UNSUPPORTED.value)),
        tool_evidence=tool_evidence,
        llm_judgment=llm_judgment,
        attested_artifacts=attested,
        provenance=provenance,
        issues=tuple(raw.get("issues", ())),
        suggestions=tuple(raw.get("suggestions", ())),
        use_case=raw.get("use_case"),
        kind=ShardKind(raw.get("kind", ShardKind.STANDARD.value)),
    )


def attested_from_dict(raw: dict[str, Any]) -> AttestedArtifact:
    """Reconstruct an AttestedArtifact from a JSON dict.

    Args:
        raw: JSON-friendly artifact mapping.

    Returns:
        AttestedArtifact instance.
    """
    return AttestedArtifact(
        kind=ArtifactKind(raw["kind"]),
        attested_by=raw["attested_by"],
        attested_at_utc=raw["attested_at_utc"],
        payload=dict(raw.get("payload", {})),
    )


_receipt_to_jsonl = receipt_to_jsonl
_receipt_from_jsonl = receipt_from_jsonl

__all__ = [
    "WORK_RECEIPT_SCHEMA_VERSION",
    "_receipt_from_jsonl",
    "_receipt_to_jsonl",
    "receipt_from_jsonl",
    "receipt_to_jsonl",
]
