"""Typed proposal and promotion records consumed by the metadata spine."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from vetinari.agents.contracts import OutcomeSignal
from vetinari.utils.serialization import dataclass_to_dict
from vetinari.workbench.evals import EvalResult


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


class WorkbenchProposalKind(str, Enum):
    """Kinds of promotion candidates tracked by the workbench."""

    PROMPT_VERSION = "prompt_version"
    MODEL_DEFAULT = "model_default"
    DATASET_REVISION = "dataset_revision"
    ADAPTER_VERSION = "adapter_version"
    PIPELINE_ACTIVATION = "pipeline_activation"
    POLICY_CHANGE = "policy_change"


class ProposalStatus(str, Enum):
    """Lifecycle states for workbench proposals."""

    OPEN = "open"
    BLOCKED = "blocked"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class ProposalGate:
    """Gate summary for a proposal candidate."""

    provenance_present: bool
    eval_present: bool
    rollback_plan_present: bool
    blockers: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ProposalGate(provenance_present={self.provenance_present!r}, eval_present={self.eval_present!r}, rollback_plan_present={self.rollback_plan_present!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the operator-console JSON contract for this gate."""
        return {
            "provenance_present": self.provenance_present,
            "eval_present": self.eval_present,
            "rollback_plan_present": self.rollback_plan_present,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True, slots=True)
class WorkbenchProposal:
    """A prompt/model/dataset/adapter/pipeline/policy promotion candidate."""

    proposal_id: str
    kind: WorkbenchProposalKind
    status: ProposalStatus
    affected_assets: tuple[str, ...]
    affected_revisions: tuple[tuple[str, str], ...]
    pre_promotion_evals: tuple[EvalResult, ...]
    gate: ProposalGate
    attached_outcome: OutcomeSignal | None
    opened_at_utc: str
    closed_at_utc: str
    notes: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.proposal_id, "proposal_id")
        _require_non_empty(self.opened_at_utc, "opened_at_utc")
        if not self.affected_assets:
            raise ValueError("affected_assets must be non-empty")
        if not self.affected_revisions:
            raise ValueError("affected_revisions must be non-empty")
        for asset_id in self.affected_assets:
            _require_non_empty(asset_id, "affected_assets entry")
        for pair in self.affected_revisions:
            if len(pair) != 2 or not pair[0].strip() or not pair[1].strip():
                raise ValueError("affected_revisions entries must be non-empty (asset_id, revision) pairs")
        if self.status is ProposalStatus.OPEN and self.closed_at_utc:
            raise ValueError("OPEN proposal requires empty closed_at_utc")
        if self.status is not ProposalStatus.OPEN and not self.closed_at_utc.strip():
            raise ValueError("terminal proposal status requires closed_at_utc")
        if self.status is ProposalStatus.ACCEPTED and self.gate.blockers:
            raise ValueError("blocker accept rejected: accepted proposal cannot carry blockers")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchProposal(proposal_id={self.proposal_id!r}, kind={self.kind!r}, status={self.status!r})"

    def to_dict(self) -> dict[str, Any]:
        """Return the operator-console JSON contract for this proposal."""
        return {
            "proposal_id": self.proposal_id,
            "kind": self.kind.value,
            "status": self.status.value,
            "affected_assets": list(self.affected_assets),
            "affected_revisions": list(self.affected_revisions),
            "pre_promotion_evals": [
                {
                    "eval_id": eval_result.eval_id,
                    "kind": eval_result.kind.value,
                    "run_id": eval_result.run_id,
                    "asset_id": eval_result.asset_id,
                    "asset_revision": eval_result.asset_revision,
                }
                for eval_result in self.pre_promotion_evals
            ],
            "gate": self.gate.to_dict(),
            "attached_outcome": None if self.attached_outcome is None else dataclass_to_dict(self.attached_outcome),
            "opened_at_utc": self.opened_at_utc,
            "closed_at_utc": self.closed_at_utc,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class Promotion:
    """The operator decision for one proposal."""

    promotion_id: str
    proposal_id: str
    accepted: bool
    decided_at_utc: str
    decided_by: str
    rationale: str

    def __post_init__(self) -> None:
        _require_non_empty(self.promotion_id, "promotion_id")
        _require_non_empty(self.proposal_id, "proposal_id")
        _require_non_empty(self.decided_at_utc, "decided_at_utc")
        _require_non_empty(self.decided_by, "decided_by")
        if not self.accepted:
            _require_non_empty(self.rationale, "rationale")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"Promotion(promotion_id={self.promotion_id!r}, proposal_id={self.proposal_id!r}, accepted={self.accepted!r})"


__all__ = ["Promotion", "ProposalGate", "ProposalStatus", "WorkbenchProposal", "WorkbenchProposalKind"]
