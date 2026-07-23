"""Fail-closed runtime boundary for Workbench improvement candidates."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.learning.atomic_writers import write_json_atomic
from vetinari.workbench.improvement_engine.contracts import (
    BLOCKER_STATE_CORRUPT,
    BLOCKER_STATE_UNAVAILABLE,
    SCHEMA_VERSION,
    ImprovementCandidate,
    ImprovementDecision,
    ImprovementEngineError,
    PromotionTarget,
    classify_promotion_target,
    evaluate_candidate,
    recovery_needed_decision,
)
from vetinari.workbench.spine_consumers import record_run_completed

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "workbench" / "improvement_engine.yaml"
STATE_FILENAME = "improvement_candidates.json"
RCG0021P05_SOURCE_REFS = (
    "FSA-3609",
    "FSA-3610",
    "FSA-3611",
    "FSA-4123",
    "FSA-4124",
    "FSA-4125",
    "FSA-4126",
    "FSA-4127",
    "FSA-4128",
    "FSA-4129",
    "FSA-4130",
    "FSA-5758",
    "FSA-6068",
    "FSA-6085",
    "FSA-6158",
    "FSA-6178",
    "FSA-6294",
    "FSA-6341",
    "FSA-6598",
    "FSA-7332",
    "FSA-7429",
    "FSA-7523",
    "FSA-7568",
    "FSA-7793",
    "FSA-7796",
)


@dataclass(frozen=True, slots=True)
class ImprovementEnginePolicy:
    """Loaded policy knobs for candidate classification and state handling."""

    schema_version: int
    default_state_dir: str
    allowed_targets: tuple[PromotionTarget, ...]
    required_lifecycle_order: tuple[str, ...]
    state_filename: str = STATE_FILENAME

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ImprovementEngineError("schema_version must match improvement engine schema")
        if not self.default_state_dir.strip():
            raise ImprovementEngineError("default_state_dir must be non-empty")
        if not self.allowed_targets:
            raise ImprovementEngineError("allowed_targets must be non-empty")
        if any(not isinstance(target, PromotionTarget) for target in self.allowed_targets):
            raise ImprovementEngineError("allowed_targets must contain PromotionTarget values")
        if self.required_lifecycle_order != ("shadow", "canary", "default"):
            raise ImprovementEngineError("required_lifecycle_order must be shadow, canary, default")
        if not self.state_filename.strip():
            raise ImprovementEngineError("state_filename must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ImprovementEnginePolicy(schema_version={self.schema_version!r}, default_state_dir={self.default_state_dir!r}, allowed_targets={self.allowed_targets!r})"


class ImprovementEngineStore:
    """Single service boundary for idempotent durable candidate records."""

    def __init__(self, root: Path | str, *, state_filename: str = STATE_FILENAME) -> None:
        self.root = Path(root)
        self.state_filename = state_filename
        self._lock = threading.RLock()

    def submit_candidate(self, candidate: ImprovementCandidate) -> ImprovementDecision:
        """Evaluate and persist one candidate decision without mutating target artifacts.

        Returns:
            ImprovementDecision value produced by submit_candidate().
        """
        decision = evaluate_candidate(candidate)
        record = {"candidate": candidate.to_dict(), "decision": decision.to_dict()}
        path = self._state_path()
        with self._lock:
            loaded = self._load_records(path, candidate.candidate_id)
            if isinstance(loaded, ImprovementDecision):
                return loaded
            records = loaded
            records[candidate.candidate_id] = record
            try:
                write_json_atomic(path, {"schema_version": SCHEMA_VERSION, "records": list(records.values())})
                # spine_consumers invokes get_spine() and absorbs observability failures.
                record_run_completed(
                    run_id=candidate.candidate_id,
                    kind="agent_run",
                    project_id="default",
                )
            except OSError:
                logger.warning("Handled recoverable failure before fallback.", exc_info=True)
                return recovery_needed_decision(
                    candidate.candidate_id,
                    BLOCKER_STATE_UNAVAILABLE,
                    f"improvement engine state unavailable: {path}",
                )
        return decision

    def list_records(self) -> tuple[dict[str, Any], ...]:
        """Read records or raise when recovery is required.

        Returns:
            Collection of records values.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._lock:
            loaded = self._load_records(self._state_path(), "list-records")
            if isinstance(loaded, ImprovementDecision):
                raise ImprovementEngineError(str(loaded.to_dict()))
            return tuple(loaded.values())

    def _state_path(self) -> Path:
        return self.root / self.state_filename

    @staticmethod
    def _load_records(path: Path, candidate_id: str) -> dict[str, dict[str, Any]] | ImprovementDecision:
        if path.parent.exists() and not path.parent.is_dir():
            return recovery_needed_decision(
                candidate_id,
                BLOCKER_STATE_UNAVAILABLE,
                f"improvement engine state parent is not a directory: {path.parent}",
            )
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return recovery_needed_decision(
                candidate_id,
                BLOCKER_STATE_CORRUPT,
                f"improvement engine state corrupt or unreadable: {path}: {exc}",
            )
        try:
            if payload["schema_version"] != SCHEMA_VERSION:
                raise ValueError("schema_version mismatch")
            records = payload["records"]
            if not isinstance(records, list):
                raise ValueError("records must be a list")
            by_id: dict[str, dict[str, Any]] = {}
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("record must be an object")
                stored_id = str(record["candidate"]["candidate_id"])
                by_id[stored_id] = record
            return by_id
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return recovery_needed_decision(
                candidate_id,
                BLOCKER_STATE_CORRUPT,
                f"improvement engine state malformed: {path}: {exc}",
            )


def load_improvement_engine_policy(path: Path | str = DEFAULT_CONFIG_PATH) -> ImprovementEnginePolicy:
    """Load the checked-in improvement-engine policy file.

    Returns:
        Resolved improvement engine policy value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise ImprovementEngineError(f"improvement engine config unreadable: {config_path}") from exc
    try:
        targets = tuple(PromotionTarget(value) for value in raw["allowed_targets"])
        return ImprovementEnginePolicy(
            schema_version=int(raw["schema_version"]),
            default_state_dir=str(raw["state"]["default_state_dir"]),
            state_filename=str(raw["state"].get("state_filename", STATE_FILENAME)),
            allowed_targets=targets,
            required_lifecycle_order=tuple(str(value) for value in raw["required_lifecycle_order"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ImprovementEngineError(f"improvement engine config malformed: {config_path}") from exc


def target_for_candidate(
    candidate: ImprovementCandidate, policy: ImprovementEnginePolicy | None = None
) -> PromotionTarget:
    """Return the candidate target after enforcing the policy allow-list.

    Args:
        candidate: Candidate value consumed by target_for_candidate().
        policy: Policy value consumed by target_for_candidate().

    Returns:
        PromotionTarget value produced by target_for_candidate().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    selected = candidate.target or classify_promotion_target(candidate.source_signals)
    active_policy = policy or load_improvement_engine_policy()
    if selected not in active_policy.allowed_targets:
        raise ImprovementEngineError(f"promotion target is not policy-allowed: {selected.value}")
    return selected


def build_rcg_0021_p05_closure_receipt(
    source_refs: tuple[str, ...] = RCG0021P05_SOURCE_REFS,
) -> dict[str, Any]:
    """Return the fail-closed closure receipt used by the P05 UI/runtime slice.

    Returns:
        Closure receipt payload proving the required source rows.

    Raises:
        ImprovementEngineError: If required source references are missing or unknown.
    """
    if not source_refs:
        raise ImprovementEngineError("RCG-0021-P05 closure receipt requires source_refs")
    unknown = [source_ref for source_ref in source_refs if source_ref not in RCG0021P05_SOURCE_REFS]
    if unknown:
        raise ImprovementEngineError(f"RCG-0021-P05 closure receipt has unknown source_refs: {unknown}")
    return {
        "receipt_id": "rcg-0021-p05:improvement-engine:closure",
        "status": "ready",
        "source_refs": list(source_refs),
        "fail_closed": True,
        "recovery": "block capability-product closure until source rows have direct evidence",
    }


def validate_rcg_0021_p05_closure_receipt(receipt: dict[str, Any]) -> None:
    """Fail closed when the P05 closure receipt is missing required evidence.

    Raises:
        ImprovementEngineError: If the receipt does not match the P05 closure contract.
    """
    if not isinstance(receipt, dict):
        raise ImprovementEngineError("RCG-0021-P05 closure receipt must be an object")
    if receipt.get("receipt_id") != "rcg-0021-p05:improvement-engine:closure":
        raise ImprovementEngineError("RCG-0021-P05 closure receipt id mismatch")
    if receipt.get("fail_closed") is not True:
        raise ImprovementEngineError("RCG-0021-P05 closure receipt must fail closed")
    if tuple(receipt.get("source_refs", ())) != RCG0021P05_SOURCE_REFS:
        raise ImprovementEngineError("RCG-0021-P05 closure receipt source_refs mismatch")


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "RCG0021P05_SOURCE_REFS",
    "STATE_FILENAME",
    "ImprovementEnginePolicy",
    "ImprovementEngineStore",
    "build_rcg_0021_p05_closure_receipt",
    "load_improvement_engine_policy",
    "target_for_candidate",
    "validate_rcg_0021_p05_closure_receipt",
]
