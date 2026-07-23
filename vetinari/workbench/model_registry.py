"""Workbench model registry lifecycle.

The registry tracks model versions, aliases, compatibility records, stage
transitions, deprecations, rollback targets, and promotion evidence gates. It
uses an append-only JSONL event log only when a caller constructs
``WorkbenchModelRegistry``; importing this module performs no I/O.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from vetinari.constants import OUTPUTS_DIR
from vetinari.workbench.evals import EvalKind
from vetinari.workbench.model_foundry.contracts import PromotionRequest
from vetinari.workbench.model_foundry.promotion import promote_or_raise
from vetinari.workbench.model_registry_contracts import (
    _ALLOWED_STAGE_TRANSITIONS,
    BLOCKER_FAILED_EVAL,
    BLOCKER_INVALID_STAGE_TRANSITION,
    BLOCKER_JUDGE_ONLY_EVIDENCE,
    BLOCKER_MISSING_EVIDENCE,
    BLOCKER_MISSING_POLICY,
    BLOCKER_MISSING_PROVENANCE,
    BLOCKER_MISSING_ROLLBACK,
    BLOCKER_PROPOSAL_NOT_OPEN,
    BLOCKER_UNREACHABLE_ROLLBACK,
    AliasBinding,
    CompatibilityRecord,
    DeprecationState,
    ModelCard,
    ModelStage,
    ModelVersion,
    RegistryGateOutcome,
    RegistrySnapshot,
    StageTransition,
    WorkbenchModelRegistryError,
)
from vetinari.workbench.model_registry_support import (
    _json_safe,
    _require_string_tuple,
    _tuple,
    _utc_now_iso,
)
from vetinari.workbench.proposals import ProposalStatus, WorkbenchProposal, WorkbenchProposalKind
from vetinari.workbench.spine_consumers import record_asset_written, record_promotion

_DEFAULT_REGISTRY_DIR = OUTPUTS_DIR / "workbench" / "model-registry"
_STATE_FILENAME = "model_registry.jsonl"
_MODEL_REGISTRY_AUDIT_PROJECT_ID = "workbench-model-registry"


class WorkbenchModelRegistry:
    """Append-only workbench model registry with fail-closed gate checks."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self._state_dir = Path(state_dir) if state_dir is not None else _DEFAULT_REGISTRY_DIR
        self._state_path = self._state_dir / _STATE_FILENAME
        self._lock = threading.RLock()
        self._cards: dict[str, ModelCard] = {}
        self._compatibility: dict[str, CompatibilityRecord] = {}
        self._versions: dict[str, ModelVersion] = {}
        self._aliases: dict[str, AliasBinding] = {}
        self._transitions: list[StageTransition] = []
        self._load()

    @property
    def state_path(self) -> Path:
        """Return the append-only state path."""
        return self._state_path

    def snapshot(self) -> RegistrySnapshot:
        """Return a stable in-memory projection of the registry.

        Returns:
            RegistrySnapshot value produced by snapshot().
        """
        with self._lock:
            return RegistrySnapshot(
                versions=tuple(self._versions.values()),
                cards=tuple(self._cards.values()),
                compatibility=tuple(self._compatibility.values()),
                aliases=tuple(self._aliases.values()),
                transitions=tuple(self._transitions),
            )

    def get_version(self, version_id: str) -> ModelVersion:
        """Return one registered version or fail closed.

        Returns:
            Resolved version value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            return self._versions[version_id]
        except KeyError as exc:
            raise WorkbenchModelRegistryError("model version not found", blockers=(version_id,)) from exc

    def get_alias(self, alias: str) -> AliasBinding:
        """Return one alias binding or fail closed.

        Returns:
            Resolved alias value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            return self._aliases[alias]
        except KeyError as exc:
            raise WorkbenchModelRegistryError("model alias not found", blockers=(alias,)) from exc

    def register_model(
        self,
        *,
        version: ModelVersion,
        card: ModelCard,
        compatibility: tuple[CompatibilityRecord, ...],
    ) -> RegistrySnapshot:
        """Append a model card, compatibility proof, and version registration.

        Returns:
            RegistrySnapshot value produced by register_model().
        """
        with self._lock:
            self._validate_new_model(version=version, card=card, compatibility=compatibility)
            self._append_event("card", card)
            self._cards[card.card_id] = card
            for record in compatibility:
                self._append_event("compatibility", record)
                self._compatibility[record.compatibility_id] = record
            self._append_event("version", version)
            self._versions[version.version_id] = version
            for alias in version.aliases:
                binding = AliasBinding(
                    alias=alias,
                    version_id=version.version_id,
                    evidence_ids=card.evidence_ids,
                    updated_at_utc=version.created_at_utc,
                )
                self._append_event("alias", binding)
                self._aliases[alias] = binding
            return self.snapshot()

    def evaluate_stage_gate(
        self,
        version_id: str,
        *,
        proposal: WorkbenchProposal,
        to_stage: ModelStage,
        rollback_version_id: str | None,
    ) -> RegistryGateOutcome:
        """Evaluate promotion, canary, serving, or rollback readiness.

        Returns:
            RegistryGateOutcome value produced by evaluate_stage_gate().
        """
        version = self.get_version(version_id)
        card = self._cards.get(version.card_id)
        blockers: list[str] = []
        evidence: dict[str, Any] = {
            "version_id": version_id,
            "proposal_id": proposal.proposal_id,
            "from_stage": version.stage.value,
            "to_stage": to_stage.value,
        }

        if card is None or not card.provenance or not any(card.provenance.values()):
            blockers.append(BLOCKER_MISSING_PROVENANCE)
        if card is None or not card.policy_ref.strip():
            blockers.append(BLOCKER_MISSING_POLICY)
        if card is None or not card.evidence_ids:
            blockers.append(BLOCKER_MISSING_EVIDENCE)

        if getattr(proposal.status, "value", proposal.status) != ProposalStatus.OPEN.value:
            blockers.append(BLOCKER_PROPOSAL_NOT_OPEN)
        if getattr(proposal.kind, "value", proposal.kind) != WorkbenchProposalKind.MODEL_DEFAULT.value:
            blockers.append(BLOCKER_MISSING_POLICY)
        if not proposal.gate.provenance_present:
            blockers.append(BLOCKER_MISSING_PROVENANCE)
        if not proposal.gate.eval_present or not proposal.pre_promotion_evals:
            blockers.append(BLOCKER_MISSING_EVIDENCE)
        if proposal.gate.blockers:
            blockers.extend(proposal.gate.blockers)
        if any(not score.passed for result in proposal.pre_promotion_evals for score in result.scores):
            blockers.append(BLOCKER_FAILED_EVAL)
        if any(
            getattr(result.kind, "value", result.kind) == EvalKind.JUDGE_ONLY.value
            for result in proposal.pre_promotion_evals
        ):
            blockers.append(BLOCKER_JUDGE_ONLY_EVIDENCE)

        if to_stage not in _ALLOWED_STAGE_TRANSITIONS.get(version.stage, frozenset()):
            blockers.append(BLOCKER_INVALID_STAGE_TRANSITION)

        to_stage_value = getattr(to_stage, "value", to_stage)
        if to_stage_value in {ModelStage.CANARY.value, ModelStage.SERVING.value}:
            if not proposal.gate.rollback_plan_present or not rollback_version_id:
                blockers.append(BLOCKER_MISSING_ROLLBACK)
            elif rollback_version_id not in self._versions:
                blockers.append(BLOCKER_UNREACHABLE_ROLLBACK)
        if to_stage_value == ModelStage.ROLLED_BACK.value and (
            not rollback_version_id or rollback_version_id not in self._versions
        ):
            blockers.append(BLOCKER_UNREACHABLE_ROLLBACK)

        evidence["eval_count"] = len(proposal.pre_promotion_evals)
        evidence["rollback_version_id"] = rollback_version_id
        unique_blockers = tuple(dict.fromkeys(blockers))
        return RegistryGateOutcome(passed=not unique_blockers, blockers=unique_blockers, evidence=evidence)

    def evaluate_foundry_promotion_gate(self, request: PromotionRequest) -> RegistryGateOutcome:
        """Evaluate a model-foundry promotion through the registry gate surface.

        Args:
            request: Complete model-foundry promotion request.

        Returns:
            Registry gate outcome backed by the model-foundry promotion
            decision evidence.

        Raises:
            ModelFoundryPromotionBlocked: If the foundry promotion gate
                rejects the request.
        """
        decision = promote_or_raise(request)
        return RegistryGateOutcome(
            passed=True,
            blockers=(),
            evidence={
                **decision.evidence,
                "request_id": decision.request_id,
                "approved": decision.approved,
            },
        )

    def transition_stage(
        self,
        version_id: str,
        *,
        to_stage: ModelStage,
        proposal: WorkbenchProposal,
        rollback_version_id: str | None,
        transition_id: str,
        evidence_ids: tuple[str, ...],
        created_at_utc: str | None = None,
    ) -> StageTransition:
        """Append a lifecycle transition after the evidence gate passes.

        Returns:
            StageTransition value produced by transition_stage().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        with self._lock:
            _require_string_tuple(evidence_ids, "evidence_ids")
            gate = self.evaluate_stage_gate(
                version_id,
                proposal=proposal,
                to_stage=to_stage,
                rollback_version_id=rollback_version_id,
            )
            if not gate.passed:
                raise WorkbenchModelRegistryError("stage transition blocked", blockers=gate.blockers)
            version = self.get_version(version_id)
            transition = StageTransition(
                transition_id=transition_id,
                version_id=version_id,
                from_stage=version.stage,
                to_stage=to_stage,
                proposal_id=proposal.proposal_id,
                evidence_ids=evidence_ids,
                rollback_version_id=rollback_version_id,
                created_at_utc=created_at_utc or _utc_now_iso(),
            )
            updated = ModelVersion(
                version_id=version.version_id,
                model_id=version.model_id,
                artifact_ref=version.artifact_ref,
                card_id=version.card_id,
                stage=to_stage,
                created_at_utc=version.created_at_utc,
                aliases=version.aliases,
                rollback_version_id=rollback_version_id,
                deprecation_state=version.deprecation_state,
            )
            self._append_event("transition", transition)
            self._append_event("version_update", updated)
            self._transitions.append(transition)
            self._versions[version_id] = updated
            return transition

    def bind_alias(
        self,
        alias: str,
        *,
        version_id: str,
        evidence_ids: tuple[str, ...],
        updated_at_utc: str | None = None,
    ) -> AliasBinding:
        """Move an alias to a reachable version with explicit evidence.

        Returns:
            AliasBinding value produced by bind_alias().
        """
        with self._lock:
            self.get_version(version_id)
            binding = AliasBinding(
                alias=alias,
                version_id=version_id,
                evidence_ids=evidence_ids,
                updated_at_utc=updated_at_utc or _utc_now_iso(),
            )
            self._append_event("alias", binding)
            self._aliases[alias] = binding
            return binding

    def deprecate_version(
        self,
        version_id: str,
        *,
        evidence_ids: tuple[str, ...],
        deprecated_at_utc: str | None = None,
    ) -> ModelVersion:
        """Mark a version deprecated without deleting rollback history.

        Returns:
            ModelVersion value produced by deprecate_version().
        """
        with self._lock:
            _require_string_tuple(evidence_ids, "evidence_ids")
            version = self.get_version(version_id)
            updated = ModelVersion(
                version_id=version.version_id,
                model_id=version.model_id,
                artifact_ref=version.artifact_ref,
                card_id=version.card_id,
                stage=ModelStage.DEPRECATED,
                created_at_utc=version.created_at_utc,
                aliases=version.aliases,
                rollback_version_id=version.rollback_version_id,
                deprecation_state=DeprecationState.DEPRECATED,
            )
            transition = StageTransition(
                transition_id=f"deprecate-{version_id}-{len(self._transitions) + 1}",
                version_id=version_id,
                from_stage=version.stage,
                to_stage=ModelStage.DEPRECATED,
                proposal_id="deprecation",
                evidence_ids=evidence_ids,
                rollback_version_id=version.rollback_version_id,
                created_at_utc=deprecated_at_utc or _utc_now_iso(),
            )
            self._append_event("transition", transition)
            self._append_event("version_update", updated)
            self._transitions.append(transition)
            self._versions[version_id] = updated
            return updated

    def _validate_new_model(
        self,
        *,
        version: ModelVersion,
        card: ModelCard,
        compatibility: tuple[CompatibilityRecord, ...],
    ) -> None:
        if version.version_id in self._versions:
            raise WorkbenchModelRegistryError("duplicate model version", blockers=(version.version_id,))
        if card.card_id in self._cards:
            raise WorkbenchModelRegistryError("duplicate model card", blockers=(card.card_id,))
        if version.card_id != card.card_id:
            raise WorkbenchModelRegistryError("version card_id does not match supplied card")
        if version.model_id != card.model_id:
            raise WorkbenchModelRegistryError("version model_id does not match supplied card")
        if not compatibility:
            raise WorkbenchModelRegistryError("missing compatibility records", blockers=(BLOCKER_MISSING_POLICY,))
        for record in compatibility:
            if record.version_id != version.version_id:
                raise WorkbenchModelRegistryError("compatibility record points at another version")
            if record.compatibility_id in self._compatibility:
                raise WorkbenchModelRegistryError("duplicate compatibility record", blockers=(record.compatibility_id,))
        declared = set(card.compatibility_ids)
        actual = {record.compatibility_id for record in compatibility}
        if declared and declared != actual:
            raise WorkbenchModelRegistryError("card compatibility_ids do not match supplied compatibility records")
        if version.rollback_version_id and version.rollback_version_id not in self._versions:
            raise WorkbenchModelRegistryError(
                "initial rollback target is unreachable",
                blockers=(BLOCKER_UNREACHABLE_ROLLBACK,),
            )

    def _load(self) -> None:
        with self._lock:
            try:
                self._state_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise WorkbenchModelRegistryError("registry directory unavailable", path=self._state_dir) from exc
            if not self._state_path.exists():
                self._state_path.touch()
            try:
                raw = self._state_path.read_bytes()
            except OSError as exc:
                raise WorkbenchModelRegistryError("registry state unreadable", path=self._state_path) from exc
            if raw and not raw.endswith(b"\n"):
                raise WorkbenchModelRegistryError("registry state truncated", path=self._state_path) from None
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WorkbenchModelRegistryError("registry state is not UTF-8", path=self._state_path) from exc
            for lineno, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    self._apply_event(str(row["kind"]), row["payload"])
                except (KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
                    raise WorkbenchModelRegistryError(
                        f"registry state damaged at line {lineno}",
                        path=self._state_path,
                    ) from exc

    def _append_event(self, kind: str, payload: Any) -> None:
        safe_payload = _json_safe(payload)
        line = json.dumps({"kind": kind, "payload": safe_payload}, separators=(",", ":"), sort_keys=True) + "\n"
        try:
            with self._state_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            payload_map = safe_payload if isinstance(safe_payload, dict) else {}
            asset_id = payload_map.get("model_id") or payload_map.get("card_id") or kind
            audit_project_id = str(payload_map.get("project_id") or _MODEL_REGISTRY_AUDIT_PROJECT_ID)
            record_asset_written(
                asset_id=str(asset_id),
                kind="model",
                project_id=audit_project_id,
                path=str(self._state_path),
                redact_fields=["path"],
            )
            if kind == "promotion":
                record_promotion(
                    run_id=str(payload_map.get("run_id", kind)),
                    project_id=audit_project_id,
                    promoted_model_id=str(asset_id),
                )
        except OSError as exc:
            raise WorkbenchModelRegistryError("registry append failed", path=self._state_path) from exc

    def _apply_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "card":
            card = _model_card_from_payload(payload)
            self._cards[card.card_id] = card
        elif kind == "compatibility":
            record = _compatibility_from_payload(payload)
            self._compatibility[record.compatibility_id] = record
        elif kind in {"version", "version_update"}:
            version = _version_from_payload(payload)
            self._versions[version.version_id] = version
        elif kind == "alias":
            binding = _alias_from_payload(payload)
            self._aliases[binding.alias] = binding
        elif kind == "transition":
            self._transitions.append(_transition_from_payload(payload))
        else:
            raise ValueError(f"unknown registry event kind {kind!r}")


def _model_card_from_payload(payload: dict[str, Any]) -> ModelCard:
    return ModelCard(
        card_id=str(payload["card_id"]),
        model_id=str(payload["model_id"]),
        display_name=str(payload["display_name"]),
        provider=str(payload["provider"]),
        capabilities=_tuple(payload, "capabilities"),
        provenance={str(k): str(v) for k, v in dict(payload["provenance"]).items()},
        evidence_ids=_tuple(payload, "evidence_ids"),
        policy_ref=str(payload["policy_ref"]),
        license_spdx=str(payload["license_spdx"]),
        artifact_sha256=payload.get("artifact_sha256"),
        compatibility_ids=_tuple(payload, "compatibility_ids"),
    )


def _compatibility_from_payload(payload: dict[str, Any]) -> CompatibilityRecord:
    return CompatibilityRecord(
        compatibility_id=str(payload["compatibility_id"]),
        version_id=str(payload["version_id"]),
        runtime_kind=str(payload["runtime_kind"]),
        backend=str(payload["backend"]),
        min_runtime_version=str(payload["min_runtime_version"]),
        policy_ref=str(payload["policy_ref"]),
        evidence_ids=_tuple(payload, "evidence_ids"),
    )


def _version_from_payload(payload: dict[str, Any]) -> ModelVersion:
    return ModelVersion(
        version_id=str(payload["version_id"]),
        model_id=str(payload["model_id"]),
        artifact_ref=str(payload["artifact_ref"]),
        card_id=str(payload["card_id"]),
        stage=ModelStage(str(payload["stage"])),
        created_at_utc=str(payload["created_at_utc"]),
        aliases=_tuple(payload, "aliases"),
        rollback_version_id=payload.get("rollback_version_id"),
        deprecation_state=DeprecationState(str(payload.get("deprecation_state", DeprecationState.ACTIVE.value))),
    )


def _alias_from_payload(payload: dict[str, Any]) -> AliasBinding:
    return AliasBinding(
        alias=str(payload["alias"]),
        version_id=str(payload["version_id"]),
        evidence_ids=_tuple(payload, "evidence_ids"),
        updated_at_utc=str(payload["updated_at_utc"]),
    )


def _transition_from_payload(payload: dict[str, Any]) -> StageTransition:
    return StageTransition(
        transition_id=str(payload["transition_id"]),
        version_id=str(payload["version_id"]),
        from_stage=ModelStage(str(payload["from_stage"])),
        to_stage=ModelStage(str(payload["to_stage"])),
        proposal_id=str(payload["proposal_id"]),
        evidence_ids=_tuple(payload, "evidence_ids"),
        rollback_version_id=payload.get("rollback_version_id"),
        created_at_utc=str(payload["created_at_utc"]),
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
    "AliasBinding",
    "CompatibilityRecord",
    "DeprecationState",
    "ModelCard",
    "ModelStage",
    "ModelVersion",
    "RegistryGateOutcome",
    "RegistrySnapshot",
    "StageTransition",
    "WorkbenchModelRegistry",
    "WorkbenchModelRegistryError",
]
