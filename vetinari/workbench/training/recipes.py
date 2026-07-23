"""Fail-closed training recipe planning for AM Workbench.

This module is import-safe. It performs no I/O until callers explicitly load a
recipe catalog, and it writes no state. The runtime path turns a governed
training request into either a scheduler-ready WorkbenchRun or a registry
proposal for cloud/distributed handoff when local resources are insufficient.
"""

from __future__ import annotations

from pathlib import Path

from vetinari.constants import PROJECT_ROOT
from vetinari.runtime.workbench_scheduler import Lane
from vetinari.workbench.data_quality import require_trusted_dataset_revision
from vetinari.workbench.effective_config import capture_training_config_snapshot
from vetinari.workbench.evals import EvalResult
from vetinari.workbench.training.recipe_helpers import (
    _build_registry_proposal,
    _build_workbench_run,
    _initial_checkpoints,
    _load_recipe_yaml,
    _require_non_empty,
)
from vetinari.workbench.training.recipe_records import (
    DatasetGateEvidence,
    DatasetPreparationStep,
    EvalGate,
    RecipeResourcePlan,
    TrainingArtifactKind,
    TrainingArtifactPackage,
    TrainingCheckpoint,
    TrainingEvalGate,
    TrainingPlan,
    TrainingPlanStatus,
    TrainingRecipe,
    TrainingRecipeError,
    TrainingRecipeKind,
    TrainingRequest,
)

_DEFAULT_CATALOG_PATH = PROJECT_ROOT / "config" / "workbench_training_recipes.yaml"


class CheckpointBrowser:
    """Read-only helper for resume and rollback checkpoint selection."""

    def __init__(self, checkpoints: tuple[TrainingCheckpoint, ...]) -> None:
        if not checkpoints:
            raise TrainingRecipeError("checkpoint browser requires at least one checkpoint")
        self._checkpoints = checkpoints

    def list_checkpoints(self) -> tuple[TrainingCheckpoint, ...]:
        """Return every known checkpoint without mutating state."""
        return self._checkpoints

    def resume_candidates(self) -> tuple[TrainingCheckpoint, ...]:
        """Return checkpoints that are both resumable and eval-clean."""
        return tuple(
            checkpoint for checkpoint in self._checkpoints if checkpoint.resume_supported and checkpoint.promotable
        )

    def rollback_candidates(self) -> tuple[TrainingCheckpoint, ...]:
        """Return checkpoints that are rollback-capable and eval-clean."""
        return tuple(
            checkpoint for checkpoint in self._checkpoints if checkpoint.rollback_supported and checkpoint.promotable
        )

    def require_resume_checkpoint(self, checkpoint_id: str) -> TrainingCheckpoint:
        """Return a trusted resume checkpoint or fail closed."""
        return self._require_candidate(checkpoint_id, self.resume_candidates(), "resume")

    def require_rollback_checkpoint(self, checkpoint_id: str) -> TrainingCheckpoint:
        """Return a trusted rollback checkpoint or fail closed."""
        return self._require_candidate(checkpoint_id, self.rollback_candidates(), "rollback")

    @staticmethod
    def _require_candidate(
        checkpoint_id: str,
        candidates: tuple[TrainingCheckpoint, ...],
        action: str,
    ) -> TrainingCheckpoint:
        _require_non_empty(checkpoint_id, "checkpoint_id")
        for checkpoint in candidates:
            if checkpoint.checkpoint_id == checkpoint_id:
                return checkpoint
        raise TrainingRecipeError(f"checkpoint {checkpoint_id!r} is not approved for {action}")


def _training_blockers(request: TrainingRequest) -> list[str]:
    blockers: list[str] = []
    try:
        require_trusted_dataset_revision(
            request.dataset_gate.quality_report,
            dataset_revision_id=request.dataset_gate.dataset_revision_id,
        )
    except Exception as exc:
        blockers.append(f"quality-gate-failed:{exc}")
    blockers.extend(f"contamination:{signal}" for signal in request.dataset_gate.contamination_signals)
    blockers.extend(f"unsupported-modality:{value}" for value in request.dataset_gate.unsupported_modalities)
    if not all(step in request.recipe.dataset_steps for step in DatasetPreparationStep):
        blockers.append("dataset-preparation-incomplete")
    blockers.extend(f"eval-gate-failed:{gate.metric_name}" for gate in request.recipe.eval_gates if not gate.passed)
    return blockers


def build_training_plan(request: TrainingRequest) -> TrainingPlan:
    """Plan local training or a handoff proposal after all governance gates pass.

    Returns:
        Newly constructed training plan value.
    """
    blockers = _training_blockers(request)
    package = package_training_artifact(request)
    if blockers:
        snapshot = capture_training_config_snapshot(request, status="blocked", blockers=tuple(blockers))
        return TrainingPlan(
            request_id=request.request_id,
            status=TrainingPlanStatus.BLOCKED,
            scheduler_lane=Lane.TRAINING,
            blockers=tuple(blockers),
            next_action="fix training data, eval, or recipe blockers before scheduling",
            resource_plan=request.recipe.resource_plan,
            artifact_package=package,
            effective_config_snapshot_id=snapshot.snapshot_id,
        )

    if request.local_vram_gb < request.recipe.resource_plan.min_vram_gb:
        if not request.recipe.resource_plan.cloud_handoff_allowed:
            snapshot = capture_training_config_snapshot(
                request,
                status="blocked",
                blockers=("local-vram-insufficient", "cloud-handoff-not-allowed"),
            )
            return TrainingPlan(
                request_id=request.request_id,
                status=TrainingPlanStatus.BLOCKED,
                scheduler_lane=Lane.TRAINING,
                blockers=("local-vram-insufficient", "cloud-handoff-not-allowed"),
                next_action="reduce recipe resource requirements or provision local training hardware",
                resource_plan=request.recipe.resource_plan,
                artifact_package=package,
                effective_config_snapshot_id=snapshot.snapshot_id,
            )
        proposal = _build_registry_proposal(request, package, blockers=())
        snapshot = capture_training_config_snapshot(request, status="proposed_handoff", blockers=())
        return TrainingPlan(
            request_id=request.request_id,
            status=TrainingPlanStatus.PROPOSED_HANDOFF,
            scheduler_lane=Lane.TRAINING,
            blockers=(),
            next_action="review cloud/distributed training handoff proposal",
            resource_plan=request.recipe.resource_plan,
            registry_proposal=proposal,
            artifact_package=package,
            checkpoints=_initial_checkpoints(request, package),
            effective_config_snapshot_id=snapshot.snapshot_id,
        )

    snapshot = capture_training_config_snapshot(request, status="ready", blockers=())
    return TrainingPlan(
        request_id=request.request_id,
        status=TrainingPlanStatus.READY,
        scheduler_lane=Lane.TRAINING,
        blockers=(),
        next_action="acquire scheduler training lane and start local recipe runner",
        resource_plan=request.recipe.resource_plan,
        run=_build_workbench_run(request),
        artifact_package=package,
        checkpoints=_initial_checkpoints(request, package),
        effective_config_snapshot_id=snapshot.snapshot_id,
    )


def package_training_artifact(request: TrainingRequest) -> TrainingArtifactPackage:
    """Create a deterministic artifact package manifest for a request.

    Returns:
        TrainingArtifactPackage value produced by package_training_artifact().
    """
    package_id = f"training-package:{request.request_id}"
    artifact_ref = f"{request.recipe.packaging_format}:{request.recipe.registry_alias}:{request.request_id}"
    rollback_ref = f"model-registry:{request.recipe.base_model_id}:rollback"
    return TrainingArtifactPackage(
        package_id=package_id,
        artifact_ref=artifact_ref,
        artifact_kind=request.recipe.output_artifact_kind,
        recipe_id=request.recipe.recipe_id,
        dataset_revision_id=request.dataset_gate.dataset_revision_id,
        eval_gate_refs=tuple(gate.evidence_ref for gate in request.recipe.eval_gates),
        rollback_ref=rollback_ref,
        registry_alias=request.recipe.registry_alias,
    )


def load_training_recipe_catalog(path: str | Path = _DEFAULT_CATALOG_PATH) -> tuple[TrainingRecipe, ...]:
    """Load recipe definitions from YAML using the same runtime constructors as direct callers.

    Returns:
        Resolved training recipe catalog value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    return _load_recipe_yaml(path)


__all__ = [
    "CheckpointBrowser",
    "DatasetGateEvidence",
    "DatasetPreparationStep",
    "EvalGate",
    "EvalResult",
    "RecipeResourcePlan",
    "TrainingArtifactKind",
    "TrainingArtifactPackage",
    "TrainingCheckpoint",
    "TrainingEvalGate",
    "TrainingPlan",
    "TrainingPlanStatus",
    "TrainingRecipe",
    "TrainingRecipeError",
    "TrainingRecipeKind",
    "TrainingRequest",
    "build_training_plan",
    "load_training_recipe_catalog",
    "package_training_artifact",
]
