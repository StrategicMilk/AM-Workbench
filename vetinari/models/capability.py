"""Capability maturity records and fail-closed promotion gating."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from vetinari.agents.contracts import OutcomeSignal, Provenance
from vetinari.constants import OUTPUTS_DIR
from vetinari.security.redaction import redact_text
from vetinari.types import EvidenceBasis

# Default JSONL path for persisted capability maturity records.
DEFAULT_CAPABILITY_JSONL = OUTPUTS_DIR / "models" / "capability.jsonl"


@dataclass(frozen=True, slots=True)
class CapabilityMaturity:
    """Evidence-backed maturity record for one model/task-profile pair."""

    model_id: str
    task_profile: str
    basis: EvidenceBasis
    evidence: OutcomeSignal
    samples: int
    pass_rate: float
    last_validated_at_utc: str

    def __repr__(self) -> str:
        """Return a compact debug representation keyed by promotion identity."""
        return (
            "CapabilityMaturity("
            f"model_id={self.model_id!r}, task_profile={self.task_profile!r}, "
            f"basis={self.basis.value!r}, samples={self.samples!r}, pass_rate={self.pass_rate!r})"
        )

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ValueError("model_id is required")
        if not self.task_profile:
            raise ValueError("task_profile is required")
        if self.samples < 0:
            raise ValueError("samples must be non-negative")
        if not 0.0 <= self.pass_rate <= 1.0:
            raise ValueError("pass_rate must be between 0.0 and 1.0")

    def to_dict(self) -> dict:
        """Return a JSON-compatible representation.

        Returns:
            Dictionary suitable for JSON persistence.
        """
        data = asdict(self)
        data["basis"] = self.basis.value
        data["evidence"] = _minimized_outcome_signal(self.evidence)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> CapabilityMaturity:
        """Reconstruct a CapabilityMaturity from serialized JSONL data.

        Args:
            data: Serialized maturity record.

        Returns:
            Reconstructed CapabilityMaturity.
        """
        from vetinari.agents.contracts import Provenance, ToolEvidence  # local to avoid circular at module-load

        raw = dict(data)
        basis = EvidenceBasis(raw.pop("basis"))

        evidence_raw = dict(raw.pop("evidence"))
        evidence_basis = EvidenceBasis(evidence_raw.get("basis", EvidenceBasis.UNSUPPORTED.value))
        provenance_raw = evidence_raw.get("provenance")
        provenance = Provenance(**provenance_raw) if provenance_raw else None
        tool_evidence_raw = evidence_raw.get("tool_evidence") or []
        tool_evidence = tuple(ToolEvidence(**te) for te in tool_evidence_raw) if tool_evidence_raw else ()
        evidence = OutcomeSignal(
            passed=bool(evidence_raw.get("passed")),
            score=float(evidence_raw.get("score", 0.0)),
            basis=evidence_basis,
            tool_evidence=tool_evidence,
            provenance=provenance,
            issues=tuple(evidence_raw.get("issues", [])),
            suggestions=tuple(evidence_raw.get("suggestions", [])),
        )

        return cls(
            model_id=raw["model_id"],
            task_profile=raw["task_profile"],
            basis=basis,
            evidence=evidence,
            samples=int(raw["samples"]),
            pass_rate=float(raw["pass_rate"]),
            last_validated_at_utc=raw["last_validated_at_utc"],
        )


@dataclass(frozen=True, slots=True)
class TaskProfileRequirement:
    """Promotion requirements for a task profile."""

    requires_tool_evidence: bool = False
    pass_rate_floor: float = 0.8
    staleness_days: int = 30


class CapabilityMaturityStore:
    """In-memory lookup wrapper for capability maturity records.

    Keyed by (model_id, task_profile). Use load_capability_records() to
    populate from the persisted JSONL file, and append_capability_record()
    to persist new records.
    """

    def __init__(self, records: list[CapabilityMaturity] | None = None):
        initial_records = records if records is not None else []
        self._records = {(record.model_id, record.task_profile): record for record in initial_records}

    def add(self, record: CapabilityMaturity) -> None:
        """Add or replace one maturity record."""
        self._records[record.model_id, record.task_profile] = record

    def get(self, model_id: str, task_profile: str) -> CapabilityMaturity | None:
        """Return the maturity record for a model/profile pair, or None."""
        return self._records.get((model_id, task_profile))

    def find_for_model(self, model_id: str) -> CapabilityMaturity | None:
        """Return the most-recently-validated record for a model across all task profiles.

        Args:
            model_id: The model identifier to look up.

        Returns:
            The record with the latest last_validated_at_utc for that model_id,
            or None if no records exist for the model.
        """
        candidates = [r for (mid, _), r in self._records.items() if mid == model_id]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.last_validated_at_utc)


def _default_capability_path() -> Path:
    """Return the configured capability JSONL path."""
    override = os.environ.get("VETINARI_CAPABILITY_JSONL_PATH", "").strip()
    if override:
        return Path(override)
    return DEFAULT_CAPABILITY_JSONL


def append_capability_record(record: CapabilityMaturity, path: Path | None = None) -> Path:
    """Persist a capability record via atomic temp+rename append.

    Mirrors append_ledger_entry in vetinari/training/ledger.py.

    Args:
        record: The CapabilityMaturity record to persist.
        path: Override for the JSONL file path; defaults to DEFAULT_CAPABILITY_JSONL.

    Returns:
        The path of the JSONL file written.
    """
    target = path or _default_capability_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    line = json.dumps(record.to_dict(), sort_keys=True)
    payload = existing + ("" if existing.endswith("\n") or not existing else "\n") + line + "\n"
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(target)
    return target


def load_capability_records(path: Path | None = None) -> CapabilityMaturityStore:
    """Load all persisted capability records from JSONL into a store.

    Returns an empty store if the file does not yet exist.

    Args:
        path: Override for the JSONL file path; defaults to DEFAULT_CAPABILITY_JSONL.

    Returns:
        A CapabilityMaturityStore populated with all records from the file.
    """
    target = path or _default_capability_path()
    if not target.exists():
        return CapabilityMaturityStore()
    records: list[CapabilityMaturity] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            records.append(CapabilityMaturity.from_dict(json.loads(stripped)))
    return CapabilityMaturityStore(records)


def can_promote(
    model_id: str,
    task_profile: str,
    *,
    records: list[CapabilityMaturity] | CapabilityMaturityStore | None = None,
    requirement: TaskProfileRequirement | None = None,
    now: datetime | None = None,
) -> OutcomeSignal:
    """Return a fail-closed promotion signal for a model/profile pair.

    Args:
        model_id: Model identifier under promotion review.
        task_profile: Task profile the model would serve.
        records: Optional maturity records or prebuilt store.
        requirement: Optional task-profile requirement override.
        now: Optional clock override for tests.

    Returns:
        OutcomeSignal that approves only fresh, passing evidence.
    """
    requirement = requirement or TaskProfileRequirement()
    store = records if isinstance(records, CapabilityMaturityStore) else CapabilityMaturityStore(records)
    record = store.get(model_id, task_profile)
    if record is None:
        return _failed(f"missing capability evidence for {model_id}/{task_profile}")
    if requirement.requires_tool_evidence and record.basis is EvidenceBasis.LLM_JUDGMENT:
        return _failed(f"task profile {task_profile} requires tool evidence")
    if record.pass_rate < requirement.pass_rate_floor:
        return _failed(f"pass_rate {record.pass_rate:.3f} below floor {requirement.pass_rate_floor:.3f}")
    validated_at = _parse_utc(record.last_validated_at_utc)
    now_utc = now or datetime.now(timezone.utc)
    if now_utc - validated_at > timedelta(days=requirement.staleness_days):
        return _failed(f"capability evidence stale after {requirement.staleness_days} days")
    if not record.evidence.passed:
        return _failed("capability evidence outcome did not pass")
    return OutcomeSignal(
        passed=True,
        score=record.pass_rate,
        basis=record.basis,
        tool_evidence=record.evidence.tool_evidence,
        llm_judgment=record.evidence.llm_judgment,
        provenance=record.evidence.provenance or _provenance(),
        issues=(),
        suggestions=(),
    )


def _failed(issue: str) -> OutcomeSignal:
    return OutcomeSignal(
        passed=False,
        score=0.0,
        basis=EvidenceBasis.UNSUPPORTED,
        provenance=_provenance(),
        issues=(issue,),
    )


def _snippet_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _minimized_outcome_signal(evidence: OutcomeSignal) -> dict[str, Any]:
    tool_evidence = []
    for item in evidence.tool_evidence:
        stdout_hash = item.stdout_hash or (_snippet_hash(item.stdout_snippet) if item.stdout_snippet else "")
        tool_evidence.append({
            "tool_name": item.tool_name,
            "command": redact_text(item.command),
            "exit_code": item.exit_code,
            "stdout_snippet": "",
            "stdout_hash": stdout_hash,
            "passed": item.passed,
        })

    outcome: dict[str, Any] = {
        "passed": evidence.passed,
        "score": evidence.score,
        "basis": evidence.basis.value,
        "tool_evidence": tool_evidence,
        "issues": tuple(redact_text(issue) for issue in evidence.issues),
        "suggestions": tuple(redact_text(suggestion) for suggestion in evidence.suggestions),
    }
    if evidence.llm_judgment is not None:
        outcome["llm_judgment"] = {
            "model_id": evidence.llm_judgment.model_id,
            "summary": redact_text(evidence.llm_judgment.summary),
            "score": evidence.llm_judgment.score,
            "reasoning": "",
        }
    if evidence.provenance:
        outcome["provenance"] = {
            "source": redact_text(evidence.provenance.source),
            "timestamp_utc": evidence.provenance.timestamp_utc,
            "model_id": evidence.provenance.model_id,
            "tool_name": evidence.provenance.tool_name,
            "tool_version": evidence.provenance.tool_version,
            "attested_by": evidence.provenance.attested_by,
        }
    return outcome


def _provenance() -> Provenance:
    return Provenance(
        source="vetinari.models.capability",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        tool_name="capability-promotion-gate",
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
