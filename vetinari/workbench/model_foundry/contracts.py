"""Contracts for the Workbench model foundry lane.

The foundry is a planning and promotion contract, not a trainer. Imports are
side-effect free; callers pass recipe, job, artifact, and promotion evidence
into deterministic functions that fail closed when proof is missing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from typing import Any

BLOCKER_MISSING_CONSENT = "missing_consent"
BLOCKER_INCOMPATIBLE_LICENSE = "incompatible_license"
BLOCKER_PII_TAINT = "pii_taint"
BLOCKER_MISSING_EVALS = "missing_evals"
BLOCKER_FAILED_EVAL = "failed_eval"
BLOCKER_MISSING_ROLLBACK = "missing_rollback_target"
BLOCKER_MISSING_RECEIPT = "missing_receipt"
BLOCKER_MISSING_SOURCE_CARD = "missing_source_card"
BLOCKER_MISSING_PROVENANCE = "missing_provenance"
BLOCKER_BUDGET_UNAVAILABLE = "budget_unavailable"
BLOCKER_ROUTE_NOT_ELIGIBLE = "route_not_eligible"
BLOCKER_JOB_NOT_COMPLETE = "job_not_complete"


class ModelFoundryError(ValueError):
    """Raised when a model foundry contract object cannot be trusted."""


class ModelFoundryPromotionBlocked(PermissionError):
    """Raised when an unsafe model artifact promotion is attempted."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        super().__init__(f"model foundry promotion blocked: {list(blockers)}")
        self.blockers = blockers


class ModelDevelopmentStrategy(str, Enum):
    """Supported model development lanes."""

    SCRATCH_PRETRAINING = "scratch_pretraining"
    TINY_UTILITY_SCRATCH = "tiny_utility_scratch"
    CONTINUED_PRETRAINING = "continued_pretraining"
    FINE_TUNING_ADAPTER = "fine_tuning_adapter"
    PREFERENCE_TUNING = "preference_tuning"
    DISTILLATION = "distillation"
    ROUTE_ELIGIBILITY = "route_eligibility"


class TokenizerKind(str, Enum):
    """Tokenizer families the foundry can describe."""

    BPE = "bpe"
    SENTENCEPIECE = "sentencepiece"
    UNIGRAM = "unigram"
    EXISTING = "existing"


class TrainingJobStatus(str, Enum):
    """Lifecycle state for a planned model foundry job."""

    DRY_RUN = "dry_run"
    BLOCKED = "blocked"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"


class ModelArtifactKind(str, Enum):
    """Artifact families produced by foundry jobs."""

    BASE_MODEL = "base_model"
    TINY_UTILITY_MODEL = "tiny_utility_model"
    CONTINUED_PRETRAINED_MODEL = "continued_pretrained_model"
    ADAPTER = "adapter"
    PREFERENCE_ADAPTER = "preference_adapter"
    DISTILLED_MODEL = "distilled_model"
    ROUTE_ELIGIBILITY_CARD = "route_eligibility_card"


@dataclass(frozen=True, slots=True)
class TokenizerSpec:
    """Tokenizer provenance and compatibility contract."""

    tokenizer_id: str
    kind: TokenizerKind
    vocab_size: int
    source_ref: str
    training_corpus_ref: str

    def __post_init__(self) -> None:
        _require_text(self.tokenizer_id, "tokenizer_id")
        _require_positive_int(self.vocab_size, "vocab_size")
        _require_text(self.source_ref, "source_ref")
        _require_text(self.training_corpus_ref, "training_corpus_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TokenizerSpec(tokenizer_id={self.tokenizer_id!r}, kind={self.kind!r}, vocab_size={self.vocab_size!r})"


@dataclass(frozen=True, slots=True)
class ScratchModelSpec:
    """Architecture shape for scratch and tiny utility model lanes."""

    architecture: str
    parameter_count: int
    context_window: int
    tokenizer: TokenizerSpec
    initialization_ref: str

    def __post_init__(self) -> None:
        _require_text(self.architecture, "architecture")
        _require_positive_int(self.parameter_count, "parameter_count")
        _require_positive_int(self.context_window, "context_window")
        if not isinstance(self.tokenizer, TokenizerSpec):
            raise ModelFoundryError("tokenizer must be a TokenizerSpec")
        _require_text(self.initialization_ref, "initialization_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ScratchModelSpec(architecture={self.architecture!r}, parameter_count={self.parameter_count!r}, context_window={self.context_window!r})"


@dataclass(frozen=True, slots=True)
class DatasetRevisionRef:
    """Dataset revision proof required before training or promotion."""

    dataset_revision_id: str
    source_card_id: str
    consent_ref: str
    license_ref: str
    pii_taint: bool
    lineage_ref: str

    def __post_init__(self) -> None:
        _require_text(self.dataset_revision_id, "dataset_revision_id")
        _require_text(self.source_card_id, "source_card_id")
        _require_text(self.lineage_ref, "lineage_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"DatasetRevisionRef(dataset_revision_id={self.dataset_revision_id!r}, source_card_id={self.source_card_id!r}, consent_ref={self.consent_ref!r})"


@dataclass(frozen=True, slots=True)
class SourceCardRef:
    """Source-card evidence attached to recipes and artifacts."""

    source_card_id: str
    policy_ref: str
    provenance_ref: str
    license_classification: str

    def __post_init__(self) -> None:
        _require_text(self.source_card_id, "source_card_id")
        _require_text(self.policy_ref, "policy_ref")
        _require_text(self.provenance_ref, "provenance_ref")
        _require_text(self.license_classification, "license_classification")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SourceCardRef(source_card_id={self.source_card_id!r}, policy_ref={self.policy_ref!r}, provenance_ref={self.provenance_ref!r})"


@dataclass(frozen=True, slots=True)
class EvalGate:
    """Promotion evaluation gate for a model artifact."""

    eval_id: str
    metric_name: str
    value: float
    threshold: float
    passed: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_text(self.eval_id, "eval_id")
        _require_text(self.metric_name, "metric_name")
        _require_text(self.evidence_ref, "evidence_ref")
        if not self.passed and self.value >= self.threshold:
            raise ModelFoundryError("failed eval gate must report value below threshold")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EvalGate(eval_id={self.eval_id!r}, metric_name={self.metric_name!r}, value={self.value!r})"


@dataclass(frozen=True, slots=True)
class FeasibilityEstimate:
    """Dry-run estimate for cost, capacity, cadence, and stop rules."""

    estimate_id: str
    parameters: int
    training_tokens: int
    hardware: str
    storage_gb: float
    checkpoint_cadence_steps: int
    estimated_wall_clock_hours: float
    stop_conditions: tuple[str, ...]
    blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.estimate_id, "estimate_id")
        _require_positive_int(self.parameters, "parameters")
        _require_positive_int(self.training_tokens, "training_tokens")
        _require_text(self.hardware, "hardware")
        _require_non_negative_float(self.storage_gb, "storage_gb")
        _require_positive_int(self.checkpoint_cadence_steps, "checkpoint_cadence_steps")
        _require_non_negative_float(self.estimated_wall_clock_hours, "estimated_wall_clock_hours")
        _require_string_tuple(self.stop_conditions, "stop_conditions")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"FeasibilityEstimate(estimate_id={self.estimate_id!r}, parameters={self.parameters!r}, training_tokens={self.training_tokens!r})"


@dataclass(frozen=True, slots=True)
class ModelRecipe:
    """Foundry recipe with governance and data proof references."""

    recipe_id: str
    strategy: ModelDevelopmentStrategy
    output_kind: ModelArtifactKind
    base_model_ref: str
    dataset_revisions: tuple[DatasetRevisionRef, ...]
    source_cards: tuple[SourceCardRef, ...]
    eval_gates: tuple[EvalGate, ...]
    receipt_refs: tuple[str, ...]
    rollback_target_ref: str
    route_eligible: bool
    budget_ref: str
    scratch_spec: ScratchModelSpec | None = None
    distillation_teacher_ref: str = ""
    base_model_parameter_count: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.recipe_id, "recipe_id")
        if self.strategy in {
            ModelDevelopmentStrategy.SCRATCH_PRETRAINING,
            ModelDevelopmentStrategy.TINY_UTILITY_SCRATCH,
        }:
            if self.scratch_spec is None:
                raise ModelFoundryError("scratch lanes require scratch_spec")
        else:
            _require_text(self.base_model_ref, "base_model_ref")
            if self.base_model_parameter_count is not None:
                _require_positive_int(self.base_model_parameter_count, "base_model_parameter_count")
        if self.strategy is ModelDevelopmentStrategy.DISTILLATION:
            _require_text(self.distillation_teacher_ref, "distillation_teacher_ref")
        if not self.dataset_revisions:
            raise ModelFoundryError("dataset_revisions must be non-empty")
        if not self.source_cards:
            raise ModelFoundryError("source_cards must be non-empty")
        _require_string_tuple(self.receipt_refs, "receipt_refs")
        _require_text(self.budget_ref, "budget_ref")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ModelRecipe(recipe_id={self.recipe_id!r}, strategy={self.strategy!r}, output_kind={self.output_kind!r})"
        )


@dataclass(frozen=True, slots=True)
class FoundryTrainingJob:
    """Scheduler-facing foundry job plan."""

    job_id: str
    recipe_id: str
    strategy: ModelDevelopmentStrategy
    status: TrainingJobStatus
    estimate: FeasibilityEstimate
    blockers: tuple[str, ...]
    receipt_refs: tuple[str, ...]
    created_at_utc: str

    def __post_init__(self) -> None:
        _require_text(self.job_id, "job_id")
        _require_text(self.recipe_id, "recipe_id")
        if not isinstance(self.estimate, FeasibilityEstimate):
            raise ModelFoundryError("estimate must be a FeasibilityEstimate")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)
        _require_string_tuple(self.receipt_refs, "receipt_refs", allow_empty=True)
        _require_text(self.created_at_utc, "created_at_utc")
        if self.status is TrainingJobStatus.BLOCKED and not self.blockers:
            raise ModelFoundryError("blocked job requires blockers")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"FoundryTrainingJob(job_id={self.job_id!r}, recipe_id={self.recipe_id!r}, strategy={self.strategy!r})"


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    """Immutable model artifact card emitted by a foundry job."""

    artifact_id: str
    kind: ModelArtifactKind
    recipe_id: str
    job_id: str
    artifact_ref: str
    dataset_revision_ids: tuple[str, ...]
    source_card_ids: tuple[str, ...]
    eval_gates: tuple[EvalGate, ...]
    receipt_refs: tuple[str, ...]
    rollback_target_ref: str
    route_eligible: bool

    def __post_init__(self) -> None:
        _require_text(self.artifact_id, "artifact_id")
        _require_text(self.recipe_id, "recipe_id")
        _require_text(self.job_id, "job_id")
        _require_text(self.artifact_ref, "artifact_ref")
        _require_string_tuple(self.dataset_revision_ids, "dataset_revision_ids")
        _require_string_tuple(self.source_card_ids, "source_card_ids")
        if not self.eval_gates:
            raise ModelFoundryError("artifact eval_gates must be non-empty")
        _require_string_tuple(self.receipt_refs, "receipt_refs")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ModelArtifact(artifact_id={self.artifact_id!r}, kind={self.kind!r}, recipe_id={self.recipe_id!r})"


@dataclass(frozen=True, slots=True)
class PromotionRequest:
    """Promotion gate input for registry or route eligibility changes."""

    request_id: str
    recipe: ModelRecipe
    job: FoundryTrainingJob
    artifact: ModelArtifact
    target_ref: str
    requested_by: str

    def __post_init__(self) -> None:
        _require_text(self.request_id, "request_id")
        _require_text(self.target_ref, "target_ref")
        _require_text(self.requested_by, "requested_by")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PromotionRequest(request_id={self.request_id!r}, recipe={self.recipe!r}, job={self.job!r})"


@dataclass(frozen=True, slots=True)
class FoundryPromotionDecision:
    """Fail-closed promotion decision."""

    request_id: str
    approved: bool
    blockers: tuple[str, ...]
    evidence: dict[str, Any]

    def __post_init__(self) -> None:
        _require_text(self.request_id, "request_id")
        _require_string_tuple(self.blockers, "blockers", allow_empty=True)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"FoundryPromotionDecision(request_id={self.request_id!r}, approved={self.approved!r}, blockers={self.blockers!r})"


def to_jsonable(value: Any) -> Any:
    """Return a JSON-compatible representation for schemas and tests.

    Returns:
        Any value produced by to_jsonable().
    """
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, tuple):
        return [to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    return value


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ModelFoundryError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise ModelFoundryError(f"{field_name} must be a tuple")
    if not allow_empty and not values:
        raise ModelFoundryError(f"{field_name} must be non-empty")
    for value in values:
        _require_text(value, f"{field_name} entry")


def _require_positive_int(value: int, field_name: str) -> None:
    if not isinstance(value, int) or value <= 0:
        raise ModelFoundryError(f"{field_name} must be a positive integer")


def _require_non_negative_float(value: float, field_name: str) -> None:
    if not isinstance(value, (int, float)) or value < 0:
        raise ModelFoundryError(f"{field_name} must be non-negative")
