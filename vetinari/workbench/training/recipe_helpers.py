"""Private helpers for Workbench training recipe planning."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _training_blockers(request: Any) -> list[str]:
    from vetinari.workbench.data_quality import require_trusted_dataset_revision
    from vetinari.workbench.training.recipe_records import DatasetPreparationStep

    blockers: list[str] = []
    try:
        require_trusted_dataset_revision(
            request.dataset_gate.quality_report, dataset_revision_id=request.dataset_gate.dataset_revision_id
        )
    except Exception as exc:
        blockers.append(f"quality-gate-failed:{exc}")
    blockers.extend(f"contamination:{signal}" for signal in request.dataset_gate.contamination_signals)
    blockers.extend(f"unsupported-modality:{value}" for value in request.dataset_gate.unsupported_modalities)
    if not all(step in request.recipe.dataset_steps for step in DatasetPreparationStep):
        blockers.append("dataset-preparation-incomplete")
    blockers.extend(f"eval-gate-failed:{gate.metric_name}" for gate in request.recipe.eval_gates if not gate.passed)
    return blockers


def _build_workbench_run(request: Any) -> Any:
    from vetinari.types import AgentType, ShardKind
    from vetinari.workbench.runs import RunKind, RunMetric, RunStatus, WorkbenchRun

    return WorkbenchRun(
        f"training:{request.request_id}",
        RunKind.TRAINING_RUN,
        RunStatus.PENDING,
        request.requested_at_utc,
        "",
        AgentType.WORKBENCH,
        (),
        "",
        ShardKind.STANDARD,
        (
            RunMetric("estimated_hours", request.recipe.resource_plan.estimated_hours, "hours"),
            RunMetric("min_vram_gb", request.recipe.resource_plan.min_vram_gb, "gb"),
            RunMetric("max_cost_usd", request.recipe.resource_plan.max_cost_usd, "usd"),
        ),
    )


def _build_registry_proposal(request: Any, package: Any, *, blockers: tuple[str, ...]) -> Any:
    from vetinari.workbench.proposals import ProposalGate, ProposalStatus, WorkbenchProposal, WorkbenchProposalKind
    from vetinari.workbench.training.recipe_records import TrainingArtifactKind

    proposal_kind = (
        WorkbenchProposalKind.ADAPTER_VERSION
        if request.recipe.output_artifact_kind is TrainingArtifactKind.ADAPTER
        else WorkbenchProposalKind.MODEL_DEFAULT
    )
    return WorkbenchProposal(
        f"training-handoff:{request.request_id}",
        proposal_kind,
        ProposalStatus.OPEN,
        (request.recipe.base_model_id, package.artifact_ref),
        ((request.dataset_gate.dataset_revision_id, "trusted"), (package.artifact_ref, "candidate")),
        (),
        ProposalGate(True, all(gate.passed for gate in request.recipe.eval_gates), True, blockers),
        None,
        request.requested_at_utc,
        "",
        "cloud/distributed training handoff proposed because local VRAM is insufficient.",
    )


def _initial_checkpoints(request: Any, package: Any) -> tuple[Any, ...]:
    from vetinari.workbench.training.recipe_records import TrainingCheckpoint

    return (
        TrainingCheckpoint(
            f"{request.request_id}:initial",
            package.artifact_ref,
            request.requested_at_utc,
            request.recipe.eval_gates,
            True,
            True,
        ),
    )


def _load_recipe_yaml(path: str | Path) -> tuple[Any, ...]:
    import yaml

    from vetinari.workbench.training.recipe_records import TrainingRecipeError

    catalog_path = Path(path)
    try:
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise TrainingRecipeError(f"invalid YAML in {catalog_path}") from exc
    except OSError as exc:
        raise TrainingRecipeError(f"unable to read training recipe catalog {catalog_path}") from exc
    if not isinstance(raw, dict):
        raise TrainingRecipeError("training recipe catalog must be a mapping")
    recipes = raw.get("recipes")
    if not isinstance(recipes, list) or not recipes:
        raise TrainingRecipeError("training recipe catalog requires non-empty recipes")
    return tuple(_recipe_from_mapping(item) for item in recipes)


def _recipe_from_mapping(item: object) -> Any:
    from vetinari.workbench.training.recipe_records import (
        DatasetPreparationStep,
        RecipeResourcePlan,
        TrainingArtifactKind,
        TrainingEvalGate,
        TrainingRecipe,
        TrainingRecipeError,
        TrainingRecipeKind,
    )

    def eval_gate(raw: object) -> TrainingEvalGate:
        if not isinstance(raw, dict):
            raise TrainingRecipeError("eval gate entry must be a mapping")
        passed = raw.get("passed", False)
        if not isinstance(passed, bool):
            raise TrainingRecipeError("eval gate passed must be a boolean")
        return TrainingEvalGate(
            str(raw.get("eval_id", "")),
            str(raw.get("metric_name", "")),
            float(raw.get("value", 0)),
            float(raw.get("threshold", 0)),
            passed,
            str(raw.get("evidence_ref", "")),
        )

    if not isinstance(item, dict):
        raise TrainingRecipeError("recipe entry must be a mapping")
    resource = item.get("resource_plan")
    eval_gates = item.get("eval_gates")
    if not isinstance(resource, dict):
        raise TrainingRecipeError("recipe resource_plan must be a mapping")
    if not isinstance(eval_gates, list) or not eval_gates:
        raise TrainingRecipeError("recipe eval_gates must be a non-empty list")
    return TrainingRecipe(
        str(item.get("recipe_id", "")),
        TrainingRecipeKind(str(item.get("kind", ""))),
        str(item.get("base_model_id", "")),
        TrainingArtifactKind(str(item.get("output_artifact_kind", ""))),
        tuple(DatasetPreparationStep(str(step)) for step in item.get("dataset_steps", ())),
        RecipeResourcePlan(
            float(resource.get("min_vram_gb", 0)),
            int(resource.get("cpu_threads", 0)),
            float(resource.get("estimated_hours", 0)),
            float(resource.get("max_cost_usd", 0)),
            bool(resource.get("cloud_handoff_allowed", False)),
        ),
        tuple(eval_gate(gate) for gate in eval_gates),
        dict(item.get("hyperparameters", {})),
        str(item.get("packaging_format", "")),
        str(item.get("registry_alias", "")),
    )


def _require_non_empty(value: str, field_name: str) -> None:
    from vetinari.workbench.training.recipe_records import TrainingRecipeError

    if not value or not value.strip():
        raise TrainingRecipeError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    from vetinari.workbench.training.recipe_records import TrainingRecipeError

    if not isinstance(values, tuple):
        raise TrainingRecipeError(f"{field_name} must be a tuple")
    if not allow_empty and not values:
        raise TrainingRecipeError(f"{field_name} must be non-empty")
    for value in values:
        _require_non_empty(value, f"{field_name} entry")
