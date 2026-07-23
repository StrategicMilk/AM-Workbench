"""Contracts and lifecycle records for the workbench model registry."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.workbench.model_registry_support import (
    _require_non_empty,
    _require_string_dict,
    _require_string_tuple,
)

BLOCKER_MISSING_PROVENANCE = "missing_provenance"
BLOCKER_MISSING_POLICY = "missing_policy"
BLOCKER_MISSING_EVIDENCE = "missing_evidence"
BLOCKER_FAILED_EVAL = "failed_eval"
BLOCKER_JUDGE_ONLY_EVIDENCE = "judge_only_evidence"
BLOCKER_MISSING_ROLLBACK = "missing_rollback_target"
BLOCKER_UNREACHABLE_ROLLBACK = "rollback_target_unreachable"
BLOCKER_PROPOSAL_NOT_OPEN = "proposal_not_open"
BLOCKER_INVALID_STAGE_TRANSITION = "invalid_stage_transition"


class WorkbenchModelRegistryError(RuntimeError):
    """Raised when registry state or a requested lifecycle change is unsafe."""

    def __init__(self, reason: str, *, blockers: tuple[str, ...] = (), path: Path | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.blockers = blockers
        self.path = path

    def __str__(self) -> str:
        parts = [f"WorkbenchModelRegistryError: {self.reason}"]
        if self.blockers:
            parts.append(f"blockers={list(self.blockers)}")
        if self.path is not None:
            parts.append(f"path={self.path}")
        return " ".join(parts)


class ModelStage(str, Enum):
    """Lifecycle stage for one model version."""

    CANDIDATE = "candidate"
    CANARY = "canary"
    SERVING = "serving"
    DEPRECATED = "deprecated"
    ROLLED_BACK = "rolled_back"


_ALLOWED_STAGE_TRANSITIONS: dict[ModelStage, frozenset[ModelStage]] = {
    ModelStage.CANDIDATE: frozenset({ModelStage.CANARY, ModelStage.SERVING, ModelStage.DEPRECATED}),
    ModelStage.CANARY: frozenset({ModelStage.SERVING, ModelStage.ROLLED_BACK, ModelStage.DEPRECATED}),
    ModelStage.SERVING: frozenset({ModelStage.ROLLED_BACK, ModelStage.DEPRECATED}),
    ModelStage.DEPRECATED: frozenset(),
    ModelStage.ROLLED_BACK: frozenset(),
}


class DeprecationState(str, Enum):
    """Deprecation state independent from the active serving stage."""

    ACTIVE = "active"
    SCHEDULED = "scheduled"
    DEPRECATED = "deprecated"


@dataclass(frozen=True, slots=True)
class ModelCard:
    """Human and machine readable evidence card for a model version."""

    card_id: str
    model_id: str
    display_name: str
    provider: str
    capabilities: tuple[str, ...]
    provenance: dict[str, str]
    evidence_ids: tuple[str, ...]
    policy_ref: str
    license_spdx: str
    artifact_sha256: str | None = None
    compatibility_ids: tuple[str, ...] = ()
    # SPDX license identifier for this model, e.g. "Apache-2.0", "MIT",
    # "AGPL-3.0". Model licenses vary by upstream and must be explicit.

    def __post_init__(self) -> None:
        _require_non_empty(self.card_id, "card_id")
        _require_non_empty(self.model_id, "model_id")
        _require_non_empty(self.display_name, "display_name")
        _require_non_empty(self.provider, "provider")
        _require_string_tuple(self.capabilities, "capabilities")
        _require_string_dict(self.provenance, "provenance")
        _require_string_tuple(self.evidence_ids, "evidence_ids")
        _require_non_empty(self.policy_ref, "policy_ref")
        _require_non_empty(self.license_spdx, "license_spdx")
        if self.artifact_sha256 is not None:
            _require_non_empty(self.artifact_sha256, "artifact_sha256")
        if not isinstance(self.compatibility_ids, tuple):
            raise ValueError("compatibility_ids must be a tuple")

    def __repr__(self) -> str:
        return f"ModelCard(card_id={self.card_id!r}, model_id={self.model_id!r}, display_name={self.display_name!r})"


@dataclass(frozen=True, slots=True)
class CompatibilityRecord:
    """Runtime compatibility proof for one model version."""

    compatibility_id: str
    version_id: str
    runtime_kind: str
    backend: str
    min_runtime_version: str
    policy_ref: str
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.compatibility_id, "compatibility_id")
        _require_non_empty(self.version_id, "version_id")
        _require_non_empty(self.runtime_kind, "runtime_kind")
        _require_non_empty(self.backend, "backend")
        _require_non_empty(self.min_runtime_version, "min_runtime_version")
        _require_non_empty(self.policy_ref, "policy_ref")
        _require_string_tuple(self.evidence_ids, "evidence_ids")

    def __repr__(self) -> str:
        return f"CompatibilityRecord(compatibility_id={self.compatibility_id!r}, version_id={self.version_id!r}, runtime_kind={self.runtime_kind!r})"


@dataclass(frozen=True, slots=True)
class ModelVersion:
    """One immutable model artifact version."""

    version_id: str
    model_id: str
    artifact_ref: str
    card_id: str
    stage: ModelStage
    created_at_utc: str
    aliases: tuple[str, ...] = ()
    rollback_version_id: str | None = None
    deprecation_state: DeprecationState = DeprecationState.ACTIVE

    def __post_init__(self) -> None:
        _require_non_empty(self.version_id, "version_id")
        _require_non_empty(self.model_id, "model_id")
        _require_non_empty(self.artifact_ref, "artifact_ref")
        _require_non_empty(self.card_id, "card_id")
        if not isinstance(self.stage, ModelStage):
            raise ValueError("stage must be a ModelStage")
        if not isinstance(self.deprecation_state, DeprecationState):
            raise ValueError("deprecation_state must be a DeprecationState")
        _require_non_empty(self.created_at_utc, "created_at_utc")
        if not isinstance(self.aliases, tuple):
            raise ValueError("aliases must be a tuple")
        if self.rollback_version_id is not None:
            _require_non_empty(self.rollback_version_id, "rollback_version_id")

    def __repr__(self) -> str:
        return f"ModelVersion(version_id={self.version_id!r}, model_id={self.model_id!r}, artifact_ref={self.artifact_ref!r})"


@dataclass(frozen=True, slots=True)
class AliasBinding:
    """Alias pointer for routing and serving surfaces."""

    alias: str
    version_id: str
    evidence_ids: tuple[str, ...]
    updated_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.alias, "alias")
        _require_non_empty(self.version_id, "version_id")
        _require_string_tuple(self.evidence_ids, "evidence_ids")
        _require_non_empty(self.updated_at_utc, "updated_at_utc")

    def __repr__(self) -> str:
        return f"AliasBinding(alias={self.alias!r}, version_id={self.version_id!r}, evidence_ids={self.evidence_ids!r})"


@dataclass(frozen=True, slots=True)
class StageTransition:
    """Append-only lifecycle transition proof."""

    transition_id: str
    version_id: str
    from_stage: ModelStage
    to_stage: ModelStage
    proposal_id: str
    evidence_ids: tuple[str, ...]
    rollback_version_id: str | None
    created_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.transition_id, "transition_id")
        _require_non_empty(self.version_id, "version_id")
        if not isinstance(self.from_stage, ModelStage):
            raise ValueError("from_stage must be a ModelStage")
        if not isinstance(self.to_stage, ModelStage):
            raise ValueError("to_stage must be a ModelStage")
        _require_non_empty(self.proposal_id, "proposal_id")
        _require_string_tuple(self.evidence_ids, "evidence_ids")
        if self.rollback_version_id is not None:
            _require_non_empty(self.rollback_version_id, "rollback_version_id")
        _require_non_empty(self.created_at_utc, "created_at_utc")

    def __repr__(self) -> str:
        return f"StageTransition(transition_id={self.transition_id!r}, version_id={self.version_id!r}, from_stage={self.from_stage!r})"


@dataclass(frozen=True, slots=True)
class RegistryGateOutcome:
    """Deterministic promotion gate result."""

    passed: bool
    blockers: tuple[str, ...]
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    """Current registry projection rebuilt from the append-only log."""

    versions: tuple[ModelVersion, ...]
    cards: tuple[ModelCard, ...]
    compatibility: tuple[CompatibilityRecord, ...]
    aliases: tuple[AliasBinding, ...]
    transitions: tuple[StageTransition, ...]

    def __repr__(self) -> str:
        return (
            f"RegistrySnapshot(versions={self.versions!r}, cards={self.cards!r}, compatibility={self.compatibility!r})"
        )


__all__ = [
    "BLOCKER_FAILED_EVAL",
    "BLOCKER_INVALID_STAGE_TRANSITION",
    "BLOCKER_JUDGE_ONLY_EVIDENCE",
    "BLOCKER_MISSING_EVIDENCE",
    "BLOCKER_MISSING_POLICY",
    "BLOCKER_MISSING_PROVENANCE",
    "BLOCKER_MISSING_ROLLBACK",
    "BLOCKER_PROPOSAL_NOT_OPEN",
    "BLOCKER_UNREACHABLE_ROLLBACK",
    "_ALLOWED_STAGE_TRANSITIONS",
    "AliasBinding",
    "CompatibilityRecord",
    "DeprecationState",
    "ModelCard",
    "ModelStage",
    "ModelVersion",
    "RegistryGateOutcome",
    "RegistrySnapshot",
    "StageTransition",
    "WorkbenchModelRegistryError",
]
