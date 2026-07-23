"""Training recipe records and enums."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from vetinari.runtime.workbench_scheduler import Lane
from vetinari.workbench.data_quality import DataQualityReport
from vetinari.workbench.proposals import WorkbenchProposal
from vetinari.workbench.runs import WorkbenchRun


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TrainingRecipeError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple) or (not allow_empty and not values):
        raise TrainingRecipeError(f"{field_name} must be a non-empty tuple[str, ...]")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise TrainingRecipeError(f"{field_name} must contain non-empty strings")


class TrainingRecipeError(ValueError):
    """Raised when a training recipe or request cannot be trusted."""


class TrainingRecipeKind(str, Enum):
    """Supported recipe families in the governed training harness."""

    SFT = "sft"
    DPO = "dpo"
    PREFERENCE = "preference"
    CLASSIFICATION = "classification"
    EMBEDDER = "embedder"
    LORA_MERGE = "lora_merge"
    QUANTIZATION = "quantization"


class DatasetPreparationStep(str, Enum):
    """Dataset preparation operations that must be evident before training."""

    CLEAN = "clean"
    DEDUPE = "dedupe"
    CONSENT_CHECK = "consent_check"
    PROVENANCE_VALIDATE = "provenance_validate"
    SPLIT = "split"
    REDACT = "redact"


class TrainingArtifactKind(str, Enum):
    """Artifacts emitted by recipe execution or packaging."""

    MODEL_CHECKPOINT = "model_checkpoint"
    ADAPTER = "adapter"
    MERGED_MODEL = "merged_model"
    QUANTIZED_MODEL = "quantized_model"
    EMBEDDING_INDEX = "embedding_index"


class TrainingPlanStatus(str, Enum):
    """Planning outcome for one governed training request."""

    READY = "ready"
    BLOCKED = "blocked"
    PROPOSED_HANDOFF = "proposed_handoff"


@dataclass(frozen=True, slots=True)
class RecipeResourcePlan:
    """Local resource estimate and cloud-handoff trigger for a recipe."""

    min_vram_gb: float
    cpu_threads: int
    estimated_hours: float
    max_cost_usd: float
    cloud_handoff_allowed: bool = False

    def __post_init__(self) -> None:
        if self.min_vram_gb <= 0:
            raise TrainingRecipeError("min_vram_gb must be positive")
        if self.cpu_threads < 1:
            raise TrainingRecipeError("cpu_threads must be >= 1")
        if self.estimated_hours <= 0:
            raise TrainingRecipeError("estimated_hours must be positive")
        if self.max_cost_usd < 0:
            raise TrainingRecipeError("max_cost_usd must be non-negative")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RecipeResourcePlan(min_vram_gb={self.min_vram_gb!r}, cpu_threads={self.cpu_threads!r}, estimated_hours={self.estimated_hours!r})"


@dataclass(frozen=True, slots=True)
class TrainingEvalGate:
    """One required evaluation gate for a trained artifact."""

    eval_id: str
    metric_name: str
    value: float
    threshold: float
    passed: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.eval_id, "eval_id")
        _require_non_empty(self.metric_name, "metric_name")
        _require_non_empty(self.evidence_ref, "evidence_ref")
        if not self.passed and self.value >= self.threshold:
            raise TrainingRecipeError("failed eval gate must report a value below threshold")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TrainingEvalGate(eval_id={self.eval_id!r}, metric_name={self.metric_name!r}, value={self.value!r})"


EvalGate = TrainingEvalGate


@dataclass(frozen=True, slots=True)
class DatasetGateEvidence:
    """Consent, license, redaction, quality, and contamination proof for training data."""

    dataset_revision_id: str
    quality_report: DataQualityReport
    consent_ref: str
    license_ref: str
    redaction_ref: str
    lineage_ref: str
    contamination_signals: tuple[str, ...] = ()
    unsupported_modalities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.dataset_revision_id, "dataset_revision_id")
        _require_non_empty(self.consent_ref, "consent_ref")
        _require_non_empty(self.license_ref, "license_ref")
        _require_non_empty(self.redaction_ref, "redaction_ref")
        _require_non_empty(self.lineage_ref, "lineage_ref")
        if not isinstance(self.quality_report, DataQualityReport):
            raise TrainingRecipeError("quality_report must be a DataQualityReport")
        _require_string_tuple(self.contamination_signals, "contamination_signals", allow_empty=True)
        _require_string_tuple(self.unsupported_modalities, "unsupported_modalities", allow_empty=True)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DatasetGateEvidence(dataset_revision_id={self.dataset_revision_id!r}, quality_report={self.quality_report!r}, consent_ref={self.consent_ref!r})"


@dataclass(frozen=True, slots=True)
class RecipeTrainingCheckpoint:
    """Checkpoint browser row with resume and rollback metadata."""

    checkpoint_id: str
    artifact_ref: str
    created_at_utc: str
    eval_gates: tuple[TrainingEvalGate, ...]
    resume_supported: bool
    rollback_supported: bool

    def __post_init__(self) -> None:
        _require_non_empty(self.checkpoint_id, "checkpoint_id")
        _require_non_empty(self.artifact_ref, "artifact_ref")
        _require_non_empty(self.created_at_utc, "created_at_utc")
        if not self.eval_gates:
            raise TrainingRecipeError("checkpoint eval_gates must be non-empty")
        for gate in self.eval_gates:
            if not isinstance(gate, TrainingEvalGate):
                raise TrainingRecipeError("checkpoint eval_gates must contain TrainingEvalGate instances")

    @property
    def promotable(self) -> bool:
        """Return whether every checkpoint eval gate passed."""
        return all(gate.passed for gate in self.eval_gates)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RecipeTrainingCheckpoint(checkpoint_id={self.checkpoint_id!r}, artifact_ref={self.artifact_ref!r}, created_at_utc={self.created_at_utc!r})"


TrainingCheckpoint = RecipeTrainingCheckpoint


@dataclass(frozen=True, slots=True)
class TrainingRecipe:
    """Governed recipe definition loaded from catalog or built by callers."""

    recipe_id: str
    kind: TrainingRecipeKind
    base_model_id: str
    output_artifact_kind: TrainingArtifactKind
    dataset_steps: tuple[DatasetPreparationStep, ...]
    resource_plan: RecipeResourcePlan
    eval_gates: tuple[TrainingEvalGate, ...]
    hyperparameters: dict[str, Any]
    packaging_format: str
    registry_alias: str
    scheduler_lane: Lane = Lane.TRAINING

    def __post_init__(self) -> None:
        _require_non_empty(self.recipe_id, "recipe_id")
        _require_non_empty(self.base_model_id, "base_model_id")
        _require_non_empty(self.packaging_format, "packaging_format")
        _require_non_empty(self.registry_alias, "registry_alias")
        if self.scheduler_lane is not Lane.TRAINING:
            raise TrainingRecipeError("training recipes must use scheduler lane training")
        if not self.dataset_steps:
            raise TrainingRecipeError("dataset_steps must be non-empty")
        required = {
            DatasetPreparationStep.CLEAN,
            DatasetPreparationStep.DEDUPE,
            DatasetPreparationStep.SPLIT,
            DatasetPreparationStep.REDACT,
        }
        missing = required - set(self.dataset_steps)
        if missing:
            raise TrainingRecipeError(f"dataset_steps missing required steps: {sorted(step.value for step in missing)}")
        if not self.eval_gates:
            raise TrainingRecipeError("eval_gates must be non-empty")
        for gate in self.eval_gates:
            if not isinstance(gate, TrainingEvalGate):
                raise TrainingRecipeError("eval_gates must contain TrainingEvalGate instances")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TrainingRecipe(recipe_id={self.recipe_id!r}, kind={self.kind!r}, base_model_id={self.base_model_id!r})"


@dataclass(frozen=True, slots=True)
class TrainingRequest:
    """One operator request to plan a governed training job."""

    request_id: str
    recipe: TrainingRecipe
    dataset_gate: DatasetGateEvidence
    requested_at_utc: str
    operator: str
    local_vram_gb: float

    def __post_init__(self) -> None:
        _require_non_empty(self.request_id, "request_id")
        _require_non_empty(self.requested_at_utc, "requested_at_utc")
        _require_non_empty(self.operator, "operator")
        if not isinstance(self.recipe, TrainingRecipe):
            raise TrainingRecipeError("recipe must be a TrainingRecipe")
        if not isinstance(self.dataset_gate, DatasetGateEvidence):
            raise TrainingRecipeError("dataset_gate must be DatasetGateEvidence")
        if self.local_vram_gb < 0:
            raise TrainingRecipeError("local_vram_gb must be non-negative")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TrainingRequest(request_id={self.request_id!r}, recipe={self.recipe!r}, dataset_gate={self.dataset_gate!r})"


@dataclass(frozen=True, slots=True)
class TrainingArtifactPackage:
    """Packaged artifact manifest for registry proposal or local execution."""

    package_id: str
    artifact_ref: str
    artifact_kind: TrainingArtifactKind
    recipe_id: str
    dataset_revision_id: str
    eval_gate_refs: tuple[str, ...]
    rollback_ref: str
    registry_alias: str

    def __post_init__(self) -> None:
        _require_non_empty(self.package_id, "package_id")
        _require_non_empty(self.artifact_ref, "artifact_ref")
        _require_non_empty(self.recipe_id, "recipe_id")
        _require_non_empty(self.dataset_revision_id, "dataset_revision_id")
        _require_non_empty(self.rollback_ref, "rollback_ref")
        _require_non_empty(self.registry_alias, "registry_alias")
        _require_string_tuple(self.eval_gate_refs, "eval_gate_refs")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TrainingArtifactPackage(package_id={self.package_id!r}, artifact_ref={self.artifact_ref!r}, artifact_kind={self.artifact_kind!r})"


@dataclass(frozen=True, slots=True)
class TrainingPlan:
    """Deterministic training plan produced by the runtime harness."""

    request_id: str
    status: TrainingPlanStatus
    scheduler_lane: Lane
    blockers: tuple[str, ...]
    next_action: str
    resource_plan: RecipeResourcePlan
    run: WorkbenchRun | None = None
    registry_proposal: WorkbenchProposal | None = None
    artifact_package: TrainingArtifactPackage | None = None
    checkpoints: tuple[TrainingCheckpoint, ...] = ()
    effective_config_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.request_id, "request_id")
        _require_non_empty(self.next_action, "next_action")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)
        if self.status is TrainingPlanStatus.READY and self.run is None:
            raise TrainingRecipeError("ready training plan requires a WorkbenchRun")
        if self.status is TrainingPlanStatus.PROPOSED_HANDOFF and self.registry_proposal is None:
            raise TrainingRecipeError("handoff training plan requires a registry_proposal")
        if self.status is TrainingPlanStatus.BLOCKED and not self.blockers:
            raise TrainingRecipeError("blocked training plan requires blockers")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TrainingPlan(request_id={self.request_id!r}, status={self.status!r}, scheduler_lane={self.scheduler_lane!r})"
