"""Fail-closed memory lineage and usage telemetry for AM Workbench.

Memory records are treated as derived Workbench assets. They must point back to
the run, trace spans, evidence assets, evals, receipts, policy, and prompt
injection context that justify their existence. Usage telemetry records recall
reason, use, outcome, helped/harmed classification, and the fact that memory is
weaker authority than code, tests, ADRs, and runtime evidence.

Side effects: importing this module opens no files, starts no threads,
registers no callbacks, and declares no mutable module-level caches.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any

from vetinari.workbench.assets import WorkbenchAsset
from vetinari.workbench.evals import EvalResult
from vetinari.workbench.evidence_assets import EvidenceAssetCard
from vetinari.workbench.runs import WorkbenchRun
from vetinari.workbench.traces import WorkbenchTrace

SCHEMA_VERSION = 1


class MemoryLineageError(Exception):
    """Raised when memory lineage cannot be trusted."""


class MemoryValidationState(str, Enum):
    """Fail-closed validation state for a memory record."""

    VERIFIED = "verified"
    BLOCKED = "blocked"
    DAMAGED = "damaged"


class MemoryUsageOutcome(str, Enum):
    """Observed effect of using a recalled memory."""

    HELPED = "helped"
    HARMED = "harmed"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class MemorySpineAuthorityTier(IntEnum):
    """Ordering from strongest authority to weakest authority."""

    CODE = 10
    TEST = 20
    ADR = 30
    RUNTIME_EVIDENCE = 40
    RECEIPT = 50
    TRACE = 60
    EVAL = 70
    MEMORY = 80
    USER_NOTE = 90


_STRONGER_THAN_MEMORY = (
    MemorySpineAuthorityTier.CODE,
    MemorySpineAuthorityTier.TEST,
    MemorySpineAuthorityTier.ADR,
    MemorySpineAuthorityTier.RUNTIME_EVIDENCE,
)


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MemoryLineageError(f"{field_name} must be non-empty")


def _require_non_empty_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple):
        raise MemoryLineageError(f"{field_name} must be a tuple")
    if not values or not all(isinstance(value, str) and value.strip() for value in values):
        raise MemoryLineageError(f"{field_name} must contain at least one non-empty string")


def _require_string_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple):
        raise MemoryLineageError(f"{field_name} must be a tuple")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise MemoryLineageError(f"{field_name} must contain only non-empty strings")


def _coerce_str_tuple(values: Any, field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise MemoryLineageError(f"{field_name} must be a list or tuple")
    result = tuple(str(value) for value in values)
    if allow_empty:
        _require_string_tuple(result, field_name)
    else:
        _require_non_empty_tuple(result, field_name)
    return result


@dataclass(frozen=True, slots=True)
class MemoryLineageRecord:
    """One memory asset with the proof needed to trust why it exists."""

    memory_id: str
    asset_id: str
    asset_revision: str
    source_run_id: str
    trace_id: str
    tool_call_ids: tuple[str, ...]
    evidence_asset_ids: tuple[str, ...]
    eval_ids: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    policy_ref: str
    validation_state: MemoryValidationState
    prompt_injection_ids: tuple[str, ...]
    created_at_utc: str
    provenance: dict[str, str]

    def __post_init__(self) -> None:
        validate_memory_lineage(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryLineageRecord(memory_id={self.memory_id!r}, asset_id={self.asset_id!r}, asset_revision={self.asset_revision!r})"


@dataclass(frozen=True, slots=True)
class MemoryUsageTelemetry:
    """One recall/use outcome for a memory record."""

    usage_id: str
    memory_id: str
    recalled_for_run_id: str
    recalled_at_utc: str
    recall_reason: str
    used: bool
    outcome: MemoryUsageOutcome
    helped_harmed_classification: str
    authority_tier: MemorySpineAuthorityTier
    evidence_refs: tuple[str, ...]
    notes: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.usage_id, "usage_id")
        _require_non_empty(self.memory_id, "memory_id")
        _require_non_empty(self.recalled_for_run_id, "recalled_for_run_id")
        _require_non_empty(self.recalled_at_utc, "recalled_at_utc")
        _require_non_empty(self.recall_reason, "recall_reason")
        _require_non_empty(self.helped_harmed_classification, "helped_harmed_classification")
        _require_non_empty_tuple(self.evidence_refs, "evidence_refs")
        if not isinstance(self.used, bool):
            raise MemoryLineageError("used must be bool")
        if not isinstance(self.outcome, MemoryUsageOutcome):
            raise MemoryLineageError("outcome must be MemoryUsageOutcome")
        if not isinstance(self.authority_tier, MemorySpineAuthorityTier):
            raise MemoryLineageError("authority_tier must be MemorySpineAuthorityTier")
        if self.authority_tier is not MemorySpineAuthorityTier.MEMORY:
            raise MemoryLineageError("memory usage must remain at MemorySpineAuthorityTier.MEMORY")
        if self.outcome is MemoryUsageOutcome.UNKNOWN and self.used:
            raise MemoryLineageError("used memory requires a helped, harmed, or neutral outcome")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryUsageTelemetry(usage_id={self.usage_id!r}, memory_id={self.memory_id!r}, recalled_for_run_id={self.recalled_for_run_id!r})"


def validate_memory_lineage(record: MemoryLineageRecord) -> None:
    """Raise unless a memory record carries complete trustworthy proof.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    _require_non_empty(record.memory_id, "memory_id")
    _require_non_empty(record.asset_id, "asset_id")
    _require_non_empty(record.asset_revision, "asset_revision")
    _require_non_empty(record.source_run_id, "source_run_id")
    _require_non_empty(record.trace_id, "trace_id")
    _require_non_empty_tuple(record.tool_call_ids, "tool_call_ids")
    _require_non_empty_tuple(record.evidence_asset_ids, "evidence_asset_ids")
    _require_non_empty_tuple(record.eval_ids, "eval_ids")
    _require_non_empty_tuple(record.receipt_ids, "receipt_ids")
    _require_non_empty(record.policy_ref, "policy_ref")
    _require_string_tuple(record.prompt_injection_ids, "prompt_injection_ids")
    _require_non_empty(record.created_at_utc, "created_at_utc")
    if not isinstance(record.validation_state, MemoryValidationState):
        raise MemoryLineageError("validation_state must be MemoryValidationState")
    if record.validation_state is not MemoryValidationState.VERIFIED:
        raise MemoryLineageError(f"memory lineage is not trusted: {record.validation_state.value}")
    if not isinstance(record.provenance, dict):
        raise MemoryLineageError("provenance must be a dict[str, str]")
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in record.provenance.items()):
        raise MemoryLineageError("provenance must contain string keys and values")
    if not record.provenance.get("source", "").strip():
        raise MemoryLineageError("provenance.source must be non-empty")
    if not record.provenance.get("reason", "").strip():
        raise MemoryLineageError("provenance.reason must explain why the memory exists")
    if not record.prompt_injection_ids and not record.provenance.get("prompt_injection_scan_id", "").strip():
        raise MemoryLineageError(
            "clean memory lineage requires provenance.prompt_injection_scan_id when prompt_injection_ids is empty"
        )


def build_memory_lineage(
    *,
    memory_id: str,
    asset: WorkbenchAsset,
    source_run: WorkbenchRun,
    trace: WorkbenchTrace,
    evidence_assets: tuple[EvidenceAssetCard, ...],
    eval_results: tuple[EvalResult, ...],
    receipt_ids: tuple[str, ...],
    policy_ref: str,
    prompt_injection_ids: tuple[str, ...],
    created_at_utc: str,
    provenance_reason: str,
) -> MemoryLineageRecord:
    """Build a verified memory lineage record from upstream Workbench proof.

    Returns:
        Newly constructed memory lineage value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if source_run.run_id != trace.run_id:
        raise MemoryLineageError("source_run and trace must reference the same run_id")
    if (asset.asset_id, asset.revision) not in source_run.asset_revisions:
        raise MemoryLineageError("source_run must reference the memory asset revision")
    evidence_ids = tuple(card.asset_card_id for card in evidence_assets)
    eval_ids = tuple(result.eval_id for result in eval_results)
    tool_call_ids = tuple(span.span_id for span in trace.spans if span.tool_name.strip())
    return MemoryLineageRecord(
        memory_id=memory_id,
        asset_id=asset.asset_id,
        asset_revision=asset.revision,
        source_run_id=source_run.run_id,
        trace_id=trace.trace_id,
        tool_call_ids=tool_call_ids,
        evidence_asset_ids=evidence_ids,
        eval_ids=eval_ids,
        receipt_ids=receipt_ids,
        policy_ref=policy_ref,
        validation_state=MemoryValidationState.VERIFIED,
        prompt_injection_ids=prompt_injection_ids,
        created_at_utc=created_at_utc,
        provenance={"source": asset.provenance["source"], "reason": provenance_reason},
    )


def memory_lineage_to_payload(
    record: MemoryLineageRecord,
    usages: tuple[MemoryUsageTelemetry, ...] = (),
) -> dict[str, Any]:
    """Return the schema-shaped JSON payload for a memory lineage record."""
    return {
        "schema_version": SCHEMA_VERSION,
        "memory_id": record.memory_id,
        "asset_ref": {"asset_id": record.asset_id, "revision": record.asset_revision},
        "source_run_id": record.source_run_id,
        "trace_id": record.trace_id,
        "tool_call_ids": list(record.tool_call_ids),
        "evidence_asset_ids": list(record.evidence_asset_ids),
        "eval_ids": list(record.eval_ids),
        "receipt_ids": list(record.receipt_ids),
        "policy_ref": record.policy_ref,
        "validation_state": record.validation_state.value,
        "prompt_injection_ids": list(record.prompt_injection_ids),
        "created_at_utc": record.created_at_utc,
        "provenance": dict(record.provenance),
        "authority": {
            "memory_tier": MemorySpineAuthorityTier.MEMORY.name.lower(),
            "memory_rank": int(MemorySpineAuthorityTier.MEMORY),
            "stronger_than_memory": [tier.name.lower() for tier in _STRONGER_THAN_MEMORY],
        },
        "usage_telemetry": [_usage_to_payload(usage) for usage in usages],
    }


def validate_memory_payload(payload: dict[str, Any]) -> MemoryLineageRecord:
    """Parse and validate a schema-shaped payload, failing closed on damage.

    Returns:
        Validation outcome for memory payload.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(payload, dict):
        raise MemoryLineageError("payload must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise MemoryLineageError("schema_version must be 1")
    asset_ref = payload.get("asset_ref")
    if not isinstance(asset_ref, dict):
        raise MemoryLineageError("asset_ref must be an object")
    try:
        record = MemoryLineageRecord(
            memory_id=str(payload["memory_id"]),
            asset_id=str(asset_ref["asset_id"]),
            asset_revision=str(asset_ref["revision"]),
            source_run_id=str(payload["source_run_id"]),
            trace_id=str(payload["trace_id"]),
            tool_call_ids=_coerce_str_tuple(payload["tool_call_ids"], "tool_call_ids"),
            evidence_asset_ids=_coerce_str_tuple(payload["evidence_asset_ids"], "evidence_asset_ids"),
            eval_ids=_coerce_str_tuple(payload["eval_ids"], "eval_ids"),
            receipt_ids=_coerce_str_tuple(payload["receipt_ids"], "receipt_ids"),
            policy_ref=str(payload["policy_ref"]),
            validation_state=MemoryValidationState(str(payload["validation_state"])),
            prompt_injection_ids=_coerce_str_tuple(
                payload["prompt_injection_ids"],
                "prompt_injection_ids",
                allow_empty=True,
            ),
            created_at_utc=str(payload["created_at_utc"]),
            provenance=dict(payload["provenance"]),
        )
    except KeyError as exc:
        raise MemoryLineageError(f"missing required payload key: {exc.args[0]}") from exc
    except ValueError as exc:
        raise MemoryLineageError(str(exc)) from exc
    usages = payload.get("usage_telemetry", [])
    if not isinstance(usages, list):
        raise MemoryLineageError("usage_telemetry must be a list")
    for usage_payload in usages:
        _usage_from_payload(usage_payload, expected_memory_id=record.memory_id)
    return record


class MemoryLineageInspector:
    """Read-only helper for answering why a memory exists and whether it helped."""

    def explain(
        self,
        record: MemoryLineageRecord,
        usages: tuple[MemoryUsageTelemetry, ...],
    ) -> dict[str, Any]:
        """Execute the explain operation.

        Args:
            record: Typed record consumed by the operation.
            usages: Usages value consumed by explain().

        Returns:
            dict[str, Any] value produced by explain().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        validate_memory_lineage(record)
        for usage in usages:
            if usage.memory_id != record.memory_id:
                raise MemoryLineageError("usage telemetry references a different memory_id")
        return {
            "why_memory_exists": (
                f"{record.memory_id} exists because {record.provenance['reason']} "
                f"from run {record.source_run_id}, trace {record.trace_id}, "
                f"evidence {', '.join(record.evidence_asset_ids)}."
            ),
            "why_recalled": [
                {
                    "usage_id": usage.usage_id,
                    "recalled_for_run_id": usage.recalled_for_run_id,
                    "reason": usage.recall_reason,
                }
                for usage in usages
            ],
            "benefit": [
                {
                    "usage_id": usage.usage_id,
                    "outcome": usage.outcome.value,
                    "classification": usage.helped_harmed_classification,
                    "evidence_refs": list(usage.evidence_refs),
                }
                for usage in usages
            ],
            "authority": {
                "memory_rank": int(MemorySpineAuthorityTier.MEMORY),
                "stronger_than_memory": [tier.name.lower() for tier in _STRONGER_THAN_MEMORY],
            },
        }


def _usage_to_payload(usage: MemoryUsageTelemetry) -> dict[str, Any]:
    return {
        "usage_id": usage.usage_id,
        "memory_id": usage.memory_id,
        "recalled_for_run_id": usage.recalled_for_run_id,
        "recalled_at_utc": usage.recalled_at_utc,
        "recall_reason": usage.recall_reason,
        "used": usage.used,
        "outcome": usage.outcome.value,
        "helped_harmed_classification": usage.helped_harmed_classification,
        "authority_tier": usage.authority_tier.name.lower(),
        "evidence_refs": list(usage.evidence_refs),
        "notes": usage.notes,
    }


def _usage_from_payload(payload: Any, *, expected_memory_id: str) -> MemoryUsageTelemetry:
    if not isinstance(payload, dict):
        raise MemoryLineageError("usage telemetry rows must be objects")
    usage = MemoryUsageTelemetry(
        usage_id=str(payload["usage_id"]),
        memory_id=str(payload["memory_id"]),
        recalled_for_run_id=str(payload["recalled_for_run_id"]),
        recalled_at_utc=str(payload["recalled_at_utc"]),
        recall_reason=str(payload["recall_reason"]),
        used=bool(payload["used"]),
        outcome=MemoryUsageOutcome(str(payload["outcome"])),
        helped_harmed_classification=str(payload["helped_harmed_classification"]),
        authority_tier=MemorySpineAuthorityTier[str(payload["authority_tier"]).upper()],
        evidence_refs=_coerce_str_tuple(payload["evidence_refs"], "evidence_refs"),
        notes=str(payload.get("notes", "")),
    )
    if usage.memory_id != expected_memory_id:
        raise MemoryLineageError("usage telemetry references a different memory_id")
    return usage


__all__ = [
    "MemoryLineageError",
    "MemoryLineageInspector",
    "MemoryLineageRecord",
    "MemorySpineAuthorityTier",
    "MemoryUsageOutcome",
    "MemoryUsageTelemetry",
    "MemoryValidationState",
    "build_memory_lineage",
    "memory_lineage_to_payload",
    "validate_memory_lineage",
    "validate_memory_payload",
]
