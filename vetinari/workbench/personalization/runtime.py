"""Fail-closed runtime boundary for Workbench user personalization."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.personalization.anti_sycophancy import AntiSycophancyGateDecision
from vetinari.workbench.personalization.contracts import (
    BLOCKER_MISSING_DEPENDENCY,
    SCHEMA_VERSION,
    CandidateInputKind,
    PersonalizationContractError,
    PersonalizationDecision,
    PersonalizationDecisionStatus,
    ProfileCard,
    TrainingCandidate,
    TrainingPromotionTarget,
    evaluate_profile_card,
    evaluate_training_candidate,
    recovery_needed_decision,
)
from vetinari.workbench.spine_consumers import record_asset_written

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "workbench" / "user_personalization.yaml"
STATE_FILENAME = "personalization_profile_store.json"
BLOCKER_STATE_CORRUPT = "state_corrupt"
BLOCKER_STATE_UNAVAILABLE = "state_unavailable"
BLOCKER_POLICY_UNAVAILABLE = "policy_unavailable"
BLOCKER_POLICY_MALFORMED = "policy_malformed"


class PersonalizationRuntimeError(PersonalizationContractError):
    """Raised when policy or state cannot be trusted."""


@dataclass(frozen=True, slots=True)
class UserPersonalizationPolicy:
    """Loaded data-only policy for personalization governance."""

    schema_version: int
    default_state_dir: str
    state_filename: str
    allowed_training_targets: tuple[TrainingPromotionTarget, ...]
    blocked_input_kinds: tuple[CandidateInputKind, ...]
    min_candidate_confidence: float
    required_dependency_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise PersonalizationRuntimeError("schema_version must match personalization schema")
        if not self.default_state_dir.strip():
            raise PersonalizationRuntimeError("default_state_dir must be non-empty")
        if not self.state_filename.strip():
            raise PersonalizationRuntimeError("state_filename must be non-empty")
        if not self.allowed_training_targets:
            raise PersonalizationRuntimeError("allowed_training_targets must be non-empty")
        if any(not isinstance(target, TrainingPromotionTarget) for target in self.allowed_training_targets):
            raise PersonalizationRuntimeError("allowed_training_targets must contain TrainingPromotionTarget values")
        if any(not isinstance(kind, CandidateInputKind) for kind in self.blocked_input_kinds):
            raise PersonalizationRuntimeError("blocked_input_kinds must contain CandidateInputKind values")
        if not 0.0 < self.min_candidate_confidence <= 1.0:
            raise PersonalizationRuntimeError("min_candidate_confidence must be > 0.0 and <= 1.0")
        if not self.required_dependency_refs or any(not item.strip() for item in self.required_dependency_refs):
            raise PersonalizationRuntimeError("required_dependency_refs must be non-empty strings")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"UserPersonalizationPolicy(schema_version={self.schema_version!r}, default_state_dir={self.default_state_dir!r}, state_filename={self.state_filename!r})"


class PersonalizationProfileStore:
    """Single locked boundary for durable profile cards and candidate decisions."""

    def __init__(self, root: Path | str, *, state_filename: str = STATE_FILENAME) -> None:
        self.root = Path(root)
        self.state_filename = state_filename
        self._lock = threading.RLock()

    def submit_profile_card(self, card: ProfileCard) -> PersonalizationDecision:
        """Evaluate and atomically persist one profile-card decision.

        Returns:
            PersonalizationDecision value produced by submit_profile_card().
        """
        decision = evaluate_profile_card(card)
        if not decision.approved:
            return decision
        record = {"profile_card": card.to_dict(), "decision": decision.to_dict()}
        path = self._state_path()
        with self._lock:
            loaded = self._load_snapshot(path, card.card_id)
            if isinstance(loaded, PersonalizationDecision):
                return loaded
            profile_cards = loaded["profile_cards"]
            decisions = loaded["decisions"]
            conflicts = loaded["conflicts"]
            existing = profile_cards.get(card.card_id)
            if existing == record:
                return decision
            if existing is not None and existing != record:
                conflict_decision = PersonalizationDecision(
                    subject_id=card.card_id,
                    status=PersonalizationDecisionStatus.CONFLICT_NEEDED,
                    approved=False,
                    blockers=("duplicate_profile_card_conflict",),
                    audit_trail=card.audit_trail,
                    evidence={"schema_version": SCHEMA_VERSION, "existing_card_id": card.card_id},
                )
                conflicts[card.card_id] = {
                    "incoming": record,
                    "existing": existing,
                    "decision": conflict_decision.to_dict(),
                }
                write_result = self._write_snapshot(path, profile_cards, decisions, conflicts, subject_id=card.card_id)
                return write_result or conflict_decision
            profile_cards[card.card_id] = record
            decisions[f"profile:{card.card_id}"] = decision.to_dict()
            write_result = self._write_snapshot(path, profile_cards, decisions, conflicts, subject_id=card.card_id)
            return write_result or decision

    def submit_training_candidate(
        self,
        candidate: TrainingCandidate,
        *,
        policy: UserPersonalizationPolicy | None = None,
        anti_sycophancy_decision: AntiSycophancyGateDecision | None = None,
    ) -> PersonalizationDecision:
        """Evaluate and persist a candidate decision without mutating downstream artifacts.

        Returns:
            PersonalizationDecision value produced by submit_training_candidate().
        """
        active_policy = policy or load_personalization_policy()
        decision = evaluate_candidate_with_policy(
            candidate,
            active_policy,
            anti_sycophancy_decision=anti_sycophancy_decision,
        )
        path = self._state_path()
        with self._lock:
            loaded = self._load_snapshot(path, candidate.candidate_id)
            if isinstance(loaded, PersonalizationDecision):
                return loaded
            loaded["decisions"][f"candidate:{candidate.candidate_id}"] = decision.to_dict()
            write_result = self._write_snapshot(
                path,
                loaded["profile_cards"],
                loaded["decisions"],
                loaded["conflicts"],
                subject_id=candidate.candidate_id,
            )
            return write_result or decision

    def list_records(self) -> tuple[dict[str, Any], ...]:
        """Return durable profile records or raise on recovery-needed state.

        Returns:
            Collection of records values.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._lock:
            loaded = self._load_snapshot(self._state_path(), "list-records")
            if isinstance(loaded, PersonalizationDecision):
                raise PersonalizationRuntimeError(str(loaded.to_dict()))
            return tuple(loaded["profile_cards"].values())

    def check_state(self) -> PersonalizationDecision:
        """Return a typed recovery-needed result for malformed state.

        Returns:
            Validation outcome for state.
        """
        with self._lock:
            loaded = self._load_snapshot(self._state_path(), "check-state")
            if isinstance(loaded, PersonalizationDecision):
                return loaded
            return PersonalizationDecision(
                subject_id="check-state",
                status=PersonalizationDecisionStatus.PROFILE_CARD_APPROVED,
                approved=True,
                blockers=(),
                audit_trail=(),
                evidence={"schema_version": SCHEMA_VERSION, "record_count": len(loaded["profile_cards"])},
            )

    def _state_path(self) -> Path:
        return self.root / self.state_filename

    def _load_snapshot(self, path: Path, subject_id: str) -> dict[str, dict[str, Any]] | PersonalizationDecision:
        if path.parent.exists() and not path.parent.is_dir():
            return recovery_needed_decision(
                subject_id,
                BLOCKER_STATE_UNAVAILABLE,
                f"personalization state parent is not a directory: {path.parent}",
            )
        if not path.exists():
            return {"profile_cards": {}, "decisions": {}, "conflicts": {}}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return recovery_needed_decision(
                subject_id,
                BLOCKER_STATE_CORRUPT,
                f"personalization state corrupt or unreadable: {path}: {exc}",
            )
        try:
            if payload["schema_version"] != SCHEMA_VERSION:
                raise ValueError("schema_version mismatch")
            profile_cards = _records_by_id(payload["profile_cards"], "profile_card", "card_id")
            decisions = {
                str(index): item for index, item in enumerate(_require_list(payload.get("decisions", []), "decisions"))
            }
            conflicts = {
                str(index): item for index, item in enumerate(_require_list(payload.get("conflicts", []), "conflicts"))
            }
            return {"profile_cards": profile_cards, "decisions": decisions, "conflicts": conflicts}
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return recovery_needed_decision(
                subject_id,
                BLOCKER_STATE_CORRUPT,
                f"personalization state malformed: {path}: {exc}",
            )

    def _write_snapshot(
        self,
        path: Path,
        profile_cards: dict[str, dict[str, Any]],
        decisions: dict[str, dict[str, Any]],
        conflicts: dict[str, dict[str, Any]],
        *,
        subject_id: str,
    ) -> PersonalizationDecision | None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": SCHEMA_VERSION,
                "profile_cards": list(profile_cards.values()),
                "decisions": list(decisions.values()),
                "conflicts": list(conflicts.values()),
            }
            temp_path = path.with_name(f".{path.name}.tmp")
            temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temp_path.replace(path)
            # spine_consumers invokes get_spine() and absorbs observability failures.
            record_asset_written(
                asset_id=f"personalization-{subject_id}",
                kind="tool",
                project_id="default",
                path=str(path),
                redact_fields=["path"],
            )
        except OSError as exc:
            logger.warning("Handled recoverable failure before fallback.", exc_info=True)
            return recovery_needed_decision(
                subject_id,
                BLOCKER_STATE_UNAVAILABLE,
                f"personalization state unavailable: {path}: {exc}",
            )
        return None


def load_personalization_policy(path: Path | str = DEFAULT_CONFIG_PATH) -> UserPersonalizationPolicy:
    """Load the checked-in data-only policy and fail closed on drift.

    Returns:
        Resolved personalization policy value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except OSError as exc:
        raise PersonalizationRuntimeError(f"personalization config unreadable: {config_path}") from exc
    try:
        allowed_keys = {
            "schema_version",
            "state",
            "allowed_training_targets",
            "blocked_input_kinds",
            "min_candidate_confidence",
            "required_dependency_refs",
        }
        unknown = set(raw) - allowed_keys
        if unknown:
            raise ValueError(f"unknown policy keys: {sorted(unknown)}")
        return UserPersonalizationPolicy(
            schema_version=int(raw["schema_version"]),
            default_state_dir=str(raw["state"]["default_state_dir"]),
            state_filename=str(raw["state"].get("state_filename", STATE_FILENAME)),
            allowed_training_targets=tuple(TrainingPromotionTarget(value) for value in raw["allowed_training_targets"]),
            blocked_input_kinds=tuple(CandidateInputKind(value) for value in raw["blocked_input_kinds"]),
            min_candidate_confidence=float(raw["min_candidate_confidence"]),
            required_dependency_refs=tuple(str(value) for value in raw["required_dependency_refs"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PersonalizationRuntimeError(f"personalization config malformed: {config_path}") from exc


def evaluate_candidate_with_policy(
    candidate: TrainingCandidate,
    policy: UserPersonalizationPolicy,
    *,
    anti_sycophancy_decision: AntiSycophancyGateDecision | None = None,
) -> PersonalizationDecision:
    """Evaluate through runtime policy, not only contract shape.

    Args:
        candidate: Candidate value consumed by evaluate_candidate_with_policy().
        policy: Policy value consumed by evaluate_candidate_with_policy().
        anti_sycophancy_decision: Anti sycophancy decision value consumed by evaluate_candidate_with_policy().

    Returns:
        PersonalizationDecision value produced by evaluate_candidate_with_policy().
    """
    blockers: list[str] = []
    if candidate.target not in policy.allowed_training_targets:
        blockers.append(f"target_not_policy_allowed:{candidate.target.value}")
    if candidate.input_kind in policy.blocked_input_kinds:
        blockers.append(f"input_kind_policy_blocked:{candidate.input_kind.value}")
    dependency_refs = candidate.governance.dependency_refs
    missing_dependency_names = [
        name for name in policy.required_dependency_refs if not str(getattr(dependency_refs, name, "")).strip()
    ]
    if missing_dependency_names:
        blockers.append(f"{BLOCKER_MISSING_DEPENDENCY}:{','.join(sorted(missing_dependency_names))}")

    contract_decision = evaluate_training_candidate(
        candidate,
        min_confidence=policy.min_candidate_confidence,
        anti_sycophancy_decision=anti_sycophancy_decision,
    )
    blockers.extend(contract_decision.blockers)
    unique_blockers = tuple(dict.fromkeys(blockers))
    if not unique_blockers:
        return contract_decision
    return PersonalizationDecision(
        subject_id=candidate.candidate_id,
        status=PersonalizationDecisionStatus.BLOCKED,
        approved=False,
        blockers=unique_blockers,
        audit_trail=candidate.audit_trail,
        evidence={**contract_decision.evidence, "runtime_policy_path": True},
    )


def _records_by_id(records: object, record_key: str, id_key: str) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for record in _require_list(records, record_key):
        if not isinstance(record, dict):
            raise ValueError(f"{record_key} record must be an object")
        item = record.get(record_key)
        if not isinstance(item, dict):
            raise ValueError(f"{record_key} payload must be an object")
        record_id = str(item[id_key])
        by_id[record_id] = record
    return by_id


def _require_list(value: object, field_name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    return value


__all__ = [
    "BLOCKER_POLICY_MALFORMED",
    "BLOCKER_POLICY_UNAVAILABLE",
    "BLOCKER_STATE_CORRUPT",
    "BLOCKER_STATE_UNAVAILABLE",
    "DEFAULT_CONFIG_PATH",
    "STATE_FILENAME",
    "PersonalizationProfileStore",
    "PersonalizationRuntimeError",
    "UserPersonalizationPolicy",
    "evaluate_candidate_with_policy",
    "load_personalization_policy",
]
