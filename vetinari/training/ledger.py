"""Training ledger and promotion audit gates."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict

from vetinari.agents.contracts import OutcomeSignal, Provenance, ToolEvidence
from vetinari.constants import OUTPUTS_DIR
from vetinari.learning.atomic_writers import append_jsonl_atomic
from vetinari.models.capability import CapabilityMaturity, TaskProfileRequirement, can_promote
from vetinari.training.data_provenance import (
    ContaminationStatus,
    DataProvenance,
    LicenseClass,
    RedactionStatus,
)
from vetinari.types import EvidenceBasis

_CONTAMINATION_CHECK_REF_RE = re.compile(r"^contamination-scan:sha256:[0-9a-f]{64}$")


class TrainingHyperparameters(TypedDict, total=False):
    """Known hyperparameter keys recorded for a training run."""

    learning_rate: float
    batch_size: int
    epochs: int
    seed: int
    _is_fallback: bool
    _compute_tier_safe: bool


@dataclass(frozen=True, slots=True)
class TrainingLedgerEntry:
    """Durable ledger record for one training run."""

    run_id: str
    algorithm: str
    base_model_id: str
    output_model_id: str
    hyperparameters: TrainingHyperparameters
    dataset_fingerprint: str
    data_provenance: DataProvenance
    tokens_processed: int
    final_loss: float
    started_at_utc: str
    finished_at_utc: str
    outcome: OutcomeSignal
    work_receipt_ids: list[str]
    promotion_decision_id: str = ""

    def __repr__(self) -> str:
        """Return a compact debug representation keyed by run and model IDs."""
        return (
            "TrainingLedgerEntry("
            f"run_id={self.run_id!r}, algorithm={self.algorithm!r}, "
            f"output_model_id={self.output_model_id!r}, passed={self.outcome.passed!r})"
        )

    def __post_init__(self) -> None:
        for field_name in ("run_id", "algorithm", "base_model_id", "output_model_id", "dataset_fingerprint"):
            if not getattr(self, field_name):
                raise ValueError(f"{field_name} is required")
        if not isinstance(self.data_provenance, DataProvenance):
            raise ValueError("data_provenance is required")
        if (
            self.data_provenance.license_classification is LicenseClass.UNKNOWN
            and self.data_provenance.contamination_status is ContaminationStatus.UNCHECKED
        ):
            raise ValueError("license_classification UNKNOWN with contamination_status UNCHECKED is rejected")
        if self.data_provenance.contamination_status is ContaminationStatus.CHECKED_CLEAN:
            check_ref = self.data_provenance.contamination_check_ref or ""
            if not _CONTAMINATION_CHECK_REF_RE.fullmatch(check_ref):
                raise ValueError("contamination_status CHECKED_CLEAN requires contamination-scan evidence")
        if self.tokens_processed <= 0:
            raise ValueError("tokens_processed must be greater than 0")
        if self.final_loss is None:
            raise ValueError("final_loss is required")
        if self.hyperparameters.get("_is_fallback") is True:
            raise ValueError("fallback training records are rejected")
        if self.hyperparameters.get("_compute_tier_safe") is not True:
            raise ValueError("compute-tier-disqualified training records are rejected")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation.

        Returns:
            Dictionary suitable for ledger JSONL persistence.
        """
        data = asdict(self)
        data["data_provenance"] = self.data_provenance.to_dict()
        data["outcome"] = _outcome_to_dict(self.outcome)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrainingLedgerEntry:
        """Build a ledger entry from serialized data.

        Args:
            data: Serialized ledger entry.

        Returns:
            Reconstructed TrainingLedgerEntry.
        """
        payload = dict(data)
        payload["data_provenance"] = DataProvenance.from_dict(payload["data_provenance"])
        payload["outcome"] = _outcome_from_dict(payload["outcome"])
        return cls(**payload)


def default_ledger_path() -> Path:
    """Return the configured training ledger path.

    Returns:
        Training ledger JSONL path.
    """
    override = os.environ.get("VETINARI_TRAINING_LEDGER_PATH", "").strip()
    if override:
        return Path(override)
    return OUTPUTS_DIR / "training" / "ledger.jsonl"


def append_ledger_entry(entry: TrainingLedgerEntry, path: Path | None = None) -> Path:
    """Append a ledger entry using the shared atomic JSONL append helper.

    Args:
        entry: Ledger entry to append.
        path: Optional destination override.

    Returns:
        Path written.
    """
    target = path or default_ledger_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl_atomic(target, entry.to_dict())
    return target


def read_ledger(path: Path | None = None) -> list[TrainingLedgerEntry]:
    """Read ledger entries from JSONL.

    Args:
        path: Optional source path override.

    Returns:
        Parsed ledger entries. Missing files return an empty list.
    """
    target = path or default_ledger_path()
    if not target.exists():
        return []
    entries = []
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            entries.append(TrainingLedgerEntry.from_dict(json.loads(stripped)))
    return entries


def latest_training_entry(
    model_id: str,
    *,
    entries: list[TrainingLedgerEntry] | None = None,
    path: Path | None = None,
) -> TrainingLedgerEntry | None:
    """Return the newest ledger entry for an output model.

    Args:
        model_id: Output model identifier.
        entries: Optional preloaded ledger entries.
        path: Optional ledger path when entries are not provided.

    Returns:
        Latest matching entry, or None when no entry exists.
    """
    candidates = [
        entry for entry in (entries if entries is not None else read_ledger(path)) if entry.output_model_id == model_id
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda entry: _parse_utc(entry.finished_at_utc))


def audit_promotion(
    candidate_model_id: str,
    *,
    task_profile: str = "default",
    capability_records: list[CapabilityMaturity] | None = None,
    ledger_entries: list[TrainingLedgerEntry] | None = None,
    ledger_path: Path | None = None,
    max_age_days: int = 90,
    allow_unknown_license_internal: bool = False,
    internal_only: bool = False,
    now: datetime | None = None,
) -> OutcomeSignal:
    """Audit whether a candidate model can be promoted.

    Args:
        candidate_model_id: Candidate output model identifier.
        task_profile: Task profile the model would serve.
        capability_records: Optional capability evidence.
        ledger_entries: Optional training ledger records.
        ledger_path: Optional ledger path when records are not provided.
        max_age_days: Maximum age for training evidence.
        allow_unknown_license_internal: Whether UNKNOWN license is allowed for internal-only runs.
        internal_only: Whether this promotion remains internal-only.
        now: Optional clock override for tests.

    Returns:
        OutcomeSignal with blockers when promotion is unsafe.
    """
    blockers: list[str] = []
    now_utc = now or datetime.now(timezone.utc)

    capability = can_promote(
        candidate_model_id,
        task_profile,
        records=capability_records,
        requirement=TaskProfileRequirement(requires_tool_evidence=True),
        now=now_utc,
    )
    if not capability.passed:
        blockers.extend(f"capability: {issue}" for issue in capability.issues)

    entry = latest_training_entry(candidate_model_id, entries=ledger_entries, path=ledger_path)
    if entry is None:
        blockers.append("training_ledger: missing passed training ledger entry")
    else:
        if not entry.outcome.passed:
            blockers.append("training_ledger: latest training outcome did not pass")
        if now_utc - _parse_utc(entry.finished_at_utc) > timedelta(days=max_age_days):
            blockers.append(f"training_ledger: latest entry stale after {max_age_days} days")
        blockers.extend(
            _provenance_blockers(
                entry.data_provenance,
                allow_unknown_license_internal=allow_unknown_license_internal,
                internal_only=internal_only,
            )
        )

    return OutcomeSignal(
        passed=not blockers,
        score=1.0 if not blockers else 0.0,
        basis=EvidenceBasis.HYBRID if not blockers else EvidenceBasis.UNSUPPORTED,
        provenance=Provenance(
            source="vetinari.training.ledger",
            timestamp_utc=now_utc.isoformat(),
            tool_name="promotion-audit",
        ),
        issues=tuple(blockers),
    )


def _provenance_blockers(
    provenance: DataProvenance,
    *,
    allow_unknown_license_internal: bool,
    internal_only: bool,
) -> list[str]:
    blockers: list[str] = []
    if not provenance.source.strip():
        blockers.append("data_provenance.source: missing dataset source")
    if provenance.license_classification is LicenseClass.UNKNOWN and not (
        allow_unknown_license_internal and internal_only
    ):
        blockers.append("data_provenance.license_classification: UNKNOWN without internal-only opt-in")
    if provenance.contamination_status is ContaminationStatus.UNCHECKED:
        blockers.append("data_provenance.contamination_status: UNCHECKED")
    if provenance.redaction_status is RedactionStatus.NONE and _source_is_user_or_pii(provenance.source):
        blockers.append("data_provenance.redaction_status: NONE for user data or detected PII")
    return blockers


def _source_is_user_or_pii(source: str) -> bool:
    lowered = source.lower()
    if "user" in lowered or "pii" in lowered:
        return True
    return bool(re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", source))


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _outcome_to_dict(outcome: OutcomeSignal) -> dict[str, Any]:
    return {
        "passed": outcome.passed,
        "score": outcome.score,
        "basis": outcome.basis.value,
        "issues": list(outcome.issues),
        "suggestions": list(outcome.suggestions),
        "provenance": asdict(outcome.provenance) if outcome.provenance else None,
        "tool_evidence": [asdict(item) for item in outcome.tool_evidence],
    }


def _outcome_from_dict(data: dict[str, Any]) -> OutcomeSignal:
    provenance_data = data.get("provenance")
    return OutcomeSignal(
        passed=bool(data.get("passed")),
        score=float(data.get("score", 0.0)),
        basis=EvidenceBasis(data.get("basis", EvidenceBasis.UNSUPPORTED.value)),
        tool_evidence=tuple(ToolEvidence(**item) for item in data.get("tool_evidence", [])),
        provenance=Provenance(**provenance_data) if provenance_data else None,
        issues=tuple(data.get("issues", [])),
        suggestions=tuple(data.get("suggestions", [])),
    )


# Public alias matching the load_ verb convention used across this package.
# Internal callers may use read_ledger() directly; external callers should
# prefer load_training_ledger() for consistency with load_capability_records().
load_training_ledger = read_ledger
