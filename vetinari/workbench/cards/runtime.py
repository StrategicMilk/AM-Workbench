"""Card generation from existing Workbench asset and proposal metadata."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

from vetinari.security.redaction import redact_route_payload
from vetinari.ux import display_label
from vetinari.workbench.assets import AssetKind, WorkbenchAsset
from vetinari.workbench.collaboration import CollaborationAuditView, CollaborationBoard, WorkbenchProject
from vetinari.workbench.metadata_spine import WorkbenchSpine, WorkbenchSpineCorrupt, get_workbench_spine
from vetinari.workbench.proposals import ProposalStatus, WorkbenchProposal


class CardGenerationError(RuntimeError):
    """Raised when a card cannot be generated without overclaiming proof."""


class WorkbenchCardKind(str, Enum):
    """Exportable Workbench card kinds."""

    MODEL_CARD = "model_card"
    DATASET_CARD = "dataset_card"
    DATA_CARD = "data_card"
    PROMPT_CARD = "prompt_card"
    SYSTEM_CARD = "system_card"


@dataclass(frozen=True, slots=True)
class WorkbenchCard:
    """A redacted, evidence-linked Workbench card."""

    schema_version: int
    card_id: str
    kind: WorkbenchCardKind
    title: str
    project_id: str
    subject_id: str
    revision: str
    provenance: tuple[tuple[str, str], ...]
    evidence_refs: tuple[str, ...]
    governance_refs: tuple[str, ...]
    sections: tuple[tuple[str, str], ...]
    collaboration: CollaborationAuditView | None = None
    risk_posture: str = "unknown"
    degraded: bool = False
    redactions_applied: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise CardGenerationError("workbench card schema_version must be 1")
        for field_name in ("card_id", "title", "project_id", "subject_id", "revision", "risk_posture"):
            _require_non_empty(getattr(self, field_name), field_name)
        if not isinstance(self.kind, WorkbenchCardKind):
            raise CardGenerationError("kind must be a WorkbenchCardKind")
        if not dict(self.provenance).get("source", "").strip():
            raise CardGenerationError("card provenance.source must be non-empty")
        if not self.sections:
            raise CardGenerationError("card sections must be non-empty")

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-serializable card payload.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = {
            "schema_version": self.schema_version,
            "card_id": self.card_id,
            "kind": self.kind.value,
            "kind_label": display_label(self.kind),
            "title": self.title,
            "project_id": self.project_id,
            "subject_id": self.subject_id,
            "revision": self.revision,
            "provenance": [list(row) for row in self.provenance],
            "evidence_refs": list(self.evidence_refs),
            "governance_refs": list(self.governance_refs),
            "sections": [list(row) for row in self.sections],
            "collaboration": asdict(self.collaboration) if self.collaboration is not None else None,
            "risk_posture": self.risk_posture,
            "risk_posture_label": display_label(self.risk_posture),
            "degraded": self.degraded,
            "redactions_applied": list(self.redactions_applied),
            "metadata": dict(sorted(self.metadata.items())),
        }
        return redact_route_payload(payload)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WorkbenchCard(schema_version={self.schema_version!r}, card_id={self.card_id!r}, kind={self.kind!r})"


class WorkbenchCardBuilder:
    """Build shareable cards without writing to the metadata spine."""

    def build_asset_card(
        self,
        asset: WorkbenchAsset,
        *,
        proposals: Sequence[WorkbenchProposal] = (),
        collaboration: CollaborationAuditView | None = None,
    ) -> WorkbenchCard:
        """Build a model, dataset/data, prompt, or system card from one asset.

        Returns:
            Newly constructed asset card value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        source = asset.provenance.get("source", "").strip()
        if not source:
            raise CardGenerationError(f"asset {asset.asset_id!r} is missing provenance.source")
        project_id = asset.provenance.get("project_id", "default")
        relevant = [proposal for proposal in proposals if asset.asset_id in proposal.affected_assets]
        evidence_refs = _evidence_refs(asset, relevant)
        governance_refs = tuple(sorted(proposal.proposal_id for proposal in relevant))
        risk_posture, degraded = _risk_posture(asset, relevant)
        redacted_provenance = _redacted_pairs(asset.provenance)
        sections = (
            ("summary", f"{display_label(asset.kind)} {asset.name} at revision {asset.revision}"),
            ("evidence", "; ".join(evidence_refs) if evidence_refs else "no eval or proposal evidence linked"),
            ("governance", "; ".join(governance_refs) if governance_refs else "no proposal history linked"),
        )
        return WorkbenchCard(
            schema_version=1,
            card_id=f"{_kind_for_asset(asset).value}:{asset.asset_id}:{asset.revision}",
            kind=_kind_for_asset(asset),
            title=asset.name,
            project_id=project_id,
            subject_id=asset.asset_id,
            revision=asset.revision,
            provenance=redacted_provenance,
            evidence_refs=evidence_refs,
            governance_refs=governance_refs,
            sections=sections,
            collaboration=collaboration,
            risk_posture=risk_posture,
            degraded=degraded,
            redactions_applied=_redactions_applied(asset.provenance, dict(redacted_provenance)),
            metadata={"source_asset_kind": asset.kind.value, "source_asset_kind_label": display_label(asset.kind)},
        )

    def build_system_card(
        self,
        *,
        project_id: str,
        title: str,
        policy_summary: dict[str, str],
        collaboration: CollaborationAuditView | None = None,
    ) -> WorkbenchCard:
        """Build a system card from policy/explainability metadata.

        Returns:
            Newly constructed system card value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        _require_non_empty(project_id, "project_id")
        _require_non_empty(title, "title")
        source = policy_summary.get("source", "").strip()
        if not source:
            raise CardGenerationError("system card policy_summary.source must be non-empty")
        degraded = policy_summary.get("allowed", "false").lower() != "true"
        return WorkbenchCard(
            schema_version=1,
            card_id=f"system_card:{project_id}:{title.lower().replace(' ', '-')}",
            kind=WorkbenchCardKind.SYSTEM_CARD,
            title=title,
            project_id=project_id,
            subject_id=project_id,
            revision=policy_summary.get("revision", "policy-current"),
            provenance=_redacted_pairs(policy_summary),
            evidence_refs=tuple(
                sorted(value for key, value in policy_summary.items() if key.endswith("_ref") and value)
            ),
            governance_refs=tuple(
                sorted(value for key, value in policy_summary.items() if key.endswith("_id") and value)
            ),
            sections=(
                ("policy", policy_summary.get("decision", "policy decision unavailable")),
                ("failure_behavior", policy_summary.get("failure_behavior", "deny when policy proof is missing")),
            ),
            collaboration=collaboration,
            risk_posture="review-required" if degraded else "shareable",
            degraded=degraded,
            redactions_applied=_redactions_applied(policy_summary, dict(_redacted_pairs(policy_summary))),
            metadata={"source": "policy_explainability"},
        )


class WorkbenchCardService:
    """Reachable card-generation entry point over the existing metadata spine."""

    def __init__(self, spine: WorkbenchSpine | None = None, builder: WorkbenchCardBuilder | None = None) -> None:
        self._spine = spine
        self._builder = builder or WorkbenchCardBuilder()

    def list_cards(
        self,
        *,
        project_id: str = "default",
        collaboration_project: WorkbenchProject | None = None,
    ) -> list[WorkbenchCard]:
        """Generate cards for assets scoped to one project.

        Returns:
            Collection of cards values.
        """
        spine = self._get_or_init_spine()
        collaboration = (
            CollaborationBoard().audit_view(collaboration_project) if collaboration_project is not None else None
        )
        cards: list[WorkbenchCard] = []
        project_asset_ids = {
            asset_id
            for run in spine.list_runs()
            if run.project_id == project_id
            for asset_id, _revision in run.asset_revisions
        }
        for asset in spine.list_assets():
            asset_project = asset.provenance.get("project_id")
            if asset_project == project_id or (asset_project is None and asset.asset_id in project_asset_ids):
                proposals = _project_proposals(spine, project_id=project_id, asset_id=asset.asset_id)
                cards.append(self._builder.build_asset_card(asset, proposals=proposals, collaboration=collaboration))
        cards.sort(key=lambda card: (card.kind.value, card.subject_id, card.revision))
        return cards

    def _get_or_init_spine(self) -> WorkbenchSpine:
        if self._spine is not None:
            return self._spine
        try:
            self._spine = get_workbench_spine()
        except WorkbenchSpineCorrupt as exc:
            raise CardGenerationError("workbench spine unavailable; repair metadata before card export") from exc
        return self._spine


def _kind_for_asset(asset: WorkbenchAsset) -> WorkbenchCardKind:
    if asset.kind is AssetKind.MODEL:
        return WorkbenchCardKind.MODEL_CARD
    if asset.kind is AssetKind.DATASET:
        return WorkbenchCardKind.DATASET_CARD
    if asset.kind is AssetKind.PROMPT:
        return WorkbenchCardKind.PROMPT_CARD
    return WorkbenchCardKind.SYSTEM_CARD


def _evidence_refs(asset: WorkbenchAsset, proposals: Sequence[WorkbenchProposal]) -> tuple[str, ...]:
    refs = {f"asset:{asset.asset_id}@{asset.revision}"}
    refs.update(f"proposal:{proposal.proposal_id}" for proposal in proposals)
    refs.update(f"eval:{result.eval_id}" for proposal in proposals for result in proposal.pre_promotion_evals)
    return tuple(sorted(refs))


def _project_proposals(
    spine: WorkbenchSpine,
    *,
    project_id: str,
    asset_id: str,
) -> tuple[WorkbenchProposal, ...]:
    project_run_ids = {run.run_id for run in spine.list_runs() if run.project_id == project_id}
    project_eval_ids = {result.eval_id for result in spine.list_evals() if result.run_id in project_run_ids}
    return tuple(
        proposal
        for proposal in spine.list_proposals()
        if asset_id in proposal.affected_assets
        and (
            any(result.eval_id in project_eval_ids for result in proposal.pre_promotion_evals)
            or any(result.run_id in project_run_ids for result in proposal.pre_promotion_evals)
        )
    )


def _risk_posture(asset: WorkbenchAsset, proposals: Sequence[WorkbenchProposal]) -> tuple[str, bool]:
    if any(taint.severity == "blocker" for taint in asset.taints):
        return "blocked", True
    if any(proposal.status is ProposalStatus.BLOCKED or proposal.gate.blockers for proposal in proposals):
        return "review-required", True
    if not proposals:
        return "unverified", True
    if all(proposal.status is ProposalStatus.ACCEPTED for proposal in proposals):
        return "shareable", False
    return "review-required", True


def _redacted_pairs(values: dict[str, str]) -> tuple[tuple[str, str], ...]:
    redacted = redact_route_payload(values)
    if not isinstance(redacted, dict):
        raise CardGenerationError("redaction returned unexpected provenance shape")
    return tuple(sorted((str(key), str(value)) for key, value in redacted.items() if str(value).strip()))


def _redactions_applied(source: dict[str, str], redacted: dict[str, str]) -> tuple[str, ...]:
    markers: list[str] = []
    for key, value in source.items():
        if str(redacted.get(key, "")) != str(value):
            markers.append(key)
    return tuple(sorted(markers))


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CardGenerationError(f"{field_name} must be non-empty")
