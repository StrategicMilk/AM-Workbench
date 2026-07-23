"""Governed tiny causal-language-model trainer for AM Workbench experiments."""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.workbench.cost.jsonl_rotator import RotatingJsonlStore
from vetinari.workbench.cost.token_cost_split import PricingConfigError, load_rotation_settings
from vetinari.workbench.data_quality import DataQualityReport, require_trusted_dataset_revision
from vetinari.workbench.evals import EvalKind, EvalResult, EvalScore
from vetinari.workbench.model_foundry.tiny_artifacts import (
    TinyCheckpoint,
    TinyExperimentalRegistryEntry,
    TinyModelCard,
    TinyScratchRunArtifact,
    TinyTrainingMetrics,
    TinyTrainingReceipt,
)
from vetinari.workbench.model_foundry.tokenizer import TinyTokenizer
from vetinari.workbench.resource_cockpit.cost_calculator import calculate_resource_cost

logger = logging.getLogger(__name__)

_RESOURCE_LEDGER_ROTATION_KEY = "ledger_jsonl"


class TinyScratchTrainerError(ValueError):
    """Raised when the tiny scratch trainer cannot safely produce an artifact."""


@dataclass(frozen=True, slots=True)
class TinyScratchDataset:
    """Governed text dataset revision for the tiny trainer."""

    dataset_revision_id: str
    samples: tuple[str, ...]
    quality_report: DataQualityReport
    consent_ref: str
    license_ref: str
    redaction_ref: str
    lineage_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.dataset_revision_id, "dataset_revision_id")
        _require_samples(self.samples)
        if not isinstance(self.quality_report, DataQualityReport):
            raise TinyScratchTrainerError("quality_report must be DataQualityReport")
        for field_name in ("consent_ref", "license_ref", "redaction_ref", "lineage_ref"):
            _require_non_empty(str(getattr(self, field_name)), field_name)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TinyScratchDataset(dataset_revision_id={self.dataset_revision_id!r}, samples={self.samples!r}, quality_report={self.quality_report!r})"


@dataclass(frozen=True, slots=True)
class TinyScratchTrainingControls:
    """Authority, budget, safety, confidence, and persistence controls."""

    run_id: str
    requested_at_utc: str
    operator: str
    authority_ref: str
    safety_ref: str
    budget_ref: str
    persistence_ref: str
    policy_ref: str
    project_id: str = "default"
    max_epochs: int = 1
    seed: int = 13
    eval_threshold: float = 0.8
    confidence_threshold: float = 0.8
    resource_ledger_path: str = ""
    gpu_seconds: float = 0.0

    def __post_init__(self) -> None:
        for field_name in (
            "run_id",
            "requested_at_utc",
            "operator",
            "project_id",
            "authority_ref",
            "safety_ref",
            "budget_ref",
            "persistence_ref",
            "policy_ref",
        ):
            _require_non_empty(str(getattr(self, field_name)), field_name)
        if self.max_epochs <= 0:
            raise TinyScratchTrainerError("max_epochs must be positive")
        for field_name in ("eval_threshold", "confidence_threshold"):
            value = float(getattr(self, field_name))
            if value < 0 or value > 1:
                raise TinyScratchTrainerError(f"{field_name} must be between 0 and 1")
        try:
            gpu_seconds = float(self.gpu_seconds)
        except (TypeError, ValueError) as exc:
            raise TinyScratchTrainerError("gpu_seconds must be finite and non-negative") from exc
        if gpu_seconds < 0 or not math.isfinite(gpu_seconds):
            raise TinyScratchTrainerError("gpu_seconds must be finite and non-negative")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TinyScratchTrainingControls(run_id={self.run_id!r}, requested_at_utc={self.requested_at_utc!r}, operator={self.operator!r})"


def train_tiny_scratch_model(
    dataset: TinyScratchDataset,
    controls: TinyScratchTrainingControls,
) -> TinyScratchRunArtifact:
    """Train a deterministic tiny bigram model and emit a governed artifact.

    Args:
        dataset: Structured data consumed by the operation.
        controls: Controls value consumed by train_tiny_scratch_model().

    Returns:
        TinyScratchRunArtifact value produced by train_tiny_scratch_model().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(dataset, TinyScratchDataset):
        raise TinyScratchTrainerError("dataset must be TinyScratchDataset")
    if not isinstance(controls, TinyScratchTrainingControls):
        raise TinyScratchTrainerError("controls must be TinyScratchTrainingControls")

    require_trusted_dataset_revision(dataset.quality_report, dataset_revision_id=dataset.dataset_revision_id)
    started = time.monotonic()
    tokenizer, encoded_samples, transitions = _train_bigram_model(dataset, controls)
    metrics = _training_metrics(encoded_samples, transitions, tokenizer, controls)
    checkpoint = _checkpoint_for(controls, metrics)
    receipts = _build_receipts(dataset, controls)
    evidence_ids = tuple(receipt.evidence_ref for receipt in receipts)
    eval_result = _eval_result_for(dataset, controls, metrics)
    registry_entry = _experimental_registry_entry(
        run_id=controls.run_id,
        checkpoint_ref=checkpoint.artifact_ref,
        eval_passed=metrics.eval_passed,
        evidence_ids=evidence_ids,
    )
    artifact = _run_artifact_for(
        dataset=dataset,
        controls=controls,
        tokenizer=tokenizer,
        transitions=transitions,
        metrics=metrics,
        receipts=receipts,
        checkpoint=checkpoint,
        eval_result=eval_result,
        registry_entry=registry_entry,
    )
    _append_resource_ledger_if_configured(
        artifact,
        controls,
        elapsed_s=time.monotonic() - started,
    )
    return artifact


def _train_bigram_model(
    dataset: TinyScratchDataset,
    controls: TinyScratchTrainingControls,
) -> tuple[TinyTokenizer, tuple[tuple[int, ...], ...], list[list[float]]]:
    tokenizer = TinyTokenizer.train(dataset.samples)
    encoded_samples = tuple(tokenizer.encode(sample) for sample in dataset.samples)
    transitions = _initial_transition_scores(tokenizer.vocab_size, controls.seed)
    for _ in range(controls.max_epochs):
        for prev_token, next_token in _iter_bigram_pairs(encoded_samples):
            transitions[prev_token][next_token] += 1.0
    return tokenizer, encoded_samples, transitions


def _training_metrics(
    encoded_samples: tuple[tuple[int, ...], ...],
    transitions: list[list[float]],
    tokenizer: TinyTokenizer,
    controls: TinyScratchTrainingControls,
) -> TinyTrainingMetrics:
    eval_score = _next_token_accuracy(encoded_samples, transitions)
    confidence = eval_score
    eval_passed = eval_score >= controls.eval_threshold and confidence >= controls.confidence_threshold
    return TinyTrainingMetrics(
        token_count=sum(len(sample) for sample in encoded_samples),
        vocab_size=tokenizer.vocab_size,
        epoch_count=controls.max_epochs,
        training_loss=_average_negative_log_likelihood(encoded_samples, transitions),
        eval_score=eval_score,
        eval_threshold=controls.eval_threshold,
        confidence=confidence,
        eval_passed=eval_passed,
    )


def _checkpoint_for(controls: TinyScratchTrainingControls, metrics: TinyTrainingMetrics) -> TinyCheckpoint:
    return TinyCheckpoint(
        checkpoint_id=f"{controls.run_id}:epoch-{controls.max_epochs}",
        artifact_ref=f"tiny-scratch://{controls.run_id}/checkpoint/epoch-{controls.max_epochs}",
        epoch=controls.max_epochs,
        metrics=metrics,
        resume_supported=True,
        rollback_supported=True,
    )


def _eval_result_for(
    dataset: TinyScratchDataset,
    controls: TinyScratchTrainingControls,
    metrics: TinyTrainingMetrics,
) -> EvalResult:
    return EvalResult(
        eval_id=f"{controls.run_id}:tiny-eval",
        kind=EvalKind.OFFLINE_SUITE,
        run_id=controls.run_id,
        asset_id=f"tiny-scratch:{controls.run_id}",
        asset_revision=dataset.dataset_revision_id,
        scores=(
            EvalScore("next_token_accuracy", metrics.eval_score, controls.eval_threshold, metrics.eval_passed),
            EvalScore("confidence", metrics.confidence, controls.confidence_threshold, metrics.eval_passed),
        ),
        captured_at_utc=controls.requested_at_utc,
        notes="Deterministic tiny bigram eval; not evidence of frontier-model capability.",
    )


def _run_artifact_for(
    *,
    dataset: TinyScratchDataset,
    controls: TinyScratchTrainingControls,
    tokenizer: TinyTokenizer,
    transitions: list[list[float]],
    metrics: TinyTrainingMetrics,
    receipts: tuple[TinyTrainingReceipt, ...],
    checkpoint: TinyCheckpoint,
    eval_result: EvalResult,
    registry_entry: TinyExperimentalRegistryEntry,
) -> TinyScratchRunArtifact:
    return TinyScratchRunArtifact(
        schema_version=1,
        run_id=controls.run_id,
        dataset_revision_id=dataset.dataset_revision_id,
        tokenizer=tokenizer.to_dict(),
        model_parameters={
            "model_family": "tiny_char_bigram_causal_lm",
            "seed": controls.seed,
            "transition_scores": _transition_payload(transitions),
        },
        metrics=metrics,
        receipts=receipts,
        checkpoints=(checkpoint,),
        model_card=_model_card_for(dataset, controls, tuple(receipt.evidence_ref for receipt in receipts)),
        eval_result=eval_result,
        registry_entry=registry_entry,
    )


def _model_card_for(
    dataset: TinyScratchDataset,
    controls: TinyScratchTrainingControls,
    evidence_ids: tuple[str, ...],
) -> TinyModelCard:
    return TinyModelCard(
        card_id=f"tiny-card:{controls.run_id}",
        display_name=f"Tiny scratch model {controls.run_id}",
        intended_use="Education, smoke tests, and controlled local utility experiments only.",
        limitations=(
            "Character bigram model; not a general-purpose language model.",
            "Experimental registration only; routing requires later human approval.",
        ),
        provenance={
            "dataset_revision_id": dataset.dataset_revision_id,
            "trainer": "vetinari.workbench.model_foundry.tiny_trainer",
            "operator": controls.operator,
        },
        evidence_ids=evidence_ids,
        policy_ref=controls.policy_ref,
    )


def _experimental_registry_entry(
    *,
    run_id: str,
    checkpoint_ref: str,
    eval_passed: bool,
    evidence_ids: tuple[str, ...],
) -> TinyExperimentalRegistryEntry:
    if eval_passed:
        return TinyExperimentalRegistryEntry(
            registry_id=f"tiny-registry:{run_id}",
            model_id=f"tiny-scratch:{run_id}",
            artifact_ref=checkpoint_ref,
            status="experimental_registered",
            route_eligible=False,
            blockers=("operator-approval-required",),
            evidence_ids=evidence_ids,
        )
    return TinyExperimentalRegistryEntry(
        registry_id=f"tiny-registry:{run_id}",
        model_id=f"tiny-scratch:{run_id}",
        artifact_ref=checkpoint_ref,
        status="blocked_failed_eval",
        route_eligible=False,
        blockers=("eval-gate-failed",),
        evidence_ids=evidence_ids,
    )


def _build_receipts(
    dataset: TinyScratchDataset,
    controls: TinyScratchTrainingControls,
) -> tuple[TinyTrainingReceipt, ...]:
    return (
        TinyTrainingReceipt("dataset-quality", "quality", dataset.quality_report.quality_report_id),
        TinyTrainingReceipt("dataset-consent", "consent", dataset.consent_ref),
        TinyTrainingReceipt("dataset-license", "license", dataset.license_ref),
        TinyTrainingReceipt("dataset-redaction", "redaction", dataset.redaction_ref),
        TinyTrainingReceipt("dataset-lineage", "lineage", dataset.lineage_ref),
        TinyTrainingReceipt("trainer-authority", "authority", controls.authority_ref),
        TinyTrainingReceipt("trainer-safety", "safety", controls.safety_ref),
        TinyTrainingReceipt("trainer-budget", "budget", controls.budget_ref),
        TinyTrainingReceipt("artifact-persistence", "persistence", controls.persistence_ref),
    )


def _append_resource_ledger_if_configured(
    artifact: TinyScratchRunArtifact,
    controls: TinyScratchTrainingControls,
    *,
    elapsed_s: float,
) -> None:
    ledger_path = controls.resource_ledger_path.strip()
    if not ledger_path:
        return
    _append_training_ledger_entry(
        ledger_path,
        project_id=controls.project_id,
        job_id=controls.run_id,
        model=artifact.registry_entry.model_id,
        elapsed_s=elapsed_s,
        gpu_seconds=controls.gpu_seconds,
        tokens_in=artifact.metrics.token_count,
        tokens_out=0,
        metadata={
            "dataset_revision_id": artifact.dataset_revision_id,
            "eval_passed": artifact.metrics.eval_passed,
            "operator": controls.operator,
            "trainer": "vetinari.workbench.model_foundry.tiny_trainer",
        },
    )


def _append_training_ledger_entry(
    path: str | Path,
    *,
    project_id: str,
    job_id: str,
    model: str,
    elapsed_s: float,
    gpu_seconds: float,
    tokens_in: int,
    tokens_out: int,
    metadata: Mapping[str, Any],
) -> None:
    cost = calculate_resource_cost(
        model=model,
        target_compute="gpu" if gpu_seconds > 0 else "cpu",
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        duration_s=gpu_seconds,
    )
    max_bytes = 1_048_576
    max_lines = 10_000
    backup_count = 10
    try:
        rotation = load_rotation_settings(
            _RESOURCE_LEDGER_ROTATION_KEY,
            default_max_bytes=max_bytes,
            default_max_lines=max_lines,
            default_backup_count=backup_count,
        )
        max_bytes = rotation.max_bytes
        max_lines = rotation.max_lines
        backup_count = rotation.backup_count
    except PricingConfigError as exc:
        logger.warning("Training ledger rotation config unavailable; using tiny trainer defaults: %s", exc)
    RotatingJsonlStore(path, max_bytes=max_bytes, max_lines=max_lines, backup_count=backup_count).append({
        "schema_version": "1.0",
        "kind": "training_resource_ledger",
        "project_id": project_id,
        "job_id": job_id,
        "model": model,
        "elapsed_s": elapsed_s,
        "gpu_hours": round(max(0.0, float(gpu_seconds)) / 3600.0, 8),
        "cost": cost.to_dict(),
        "metadata": dict(metadata),
    })


def _initial_transition_scores(vocab_size: int, seed: int) -> list[list[float]]:
    return [
        [0.001 + (((seed + row * 31 + column * 17) % 10) / 1000) for column in range(vocab_size)]
        for row in range(vocab_size)
    ]


def _iter_bigram_pairs(encoded_samples: Iterable[tuple[int, ...]]) -> Iterable[tuple[int, int]]:
    for sample in encoded_samples:
        for index in range(len(sample) - 1):
            yield sample[index], sample[index + 1]


def _average_negative_log_likelihood(
    encoded_samples: tuple[tuple[int, ...], ...], transitions: list[list[float]]
) -> float:
    total_loss = 0.0
    pair_count = 0
    for prev_token, next_token in _iter_bigram_pairs(encoded_samples):
        row = transitions[prev_token]
        probability = row[next_token] / sum(row)
        total_loss += -math.log(max(probability, 1e-12))
        pair_count += 1
    if pair_count == 0:
        raise TinyScratchTrainerError("encoded samples must contain bigram pairs")
    return round(total_loss / pair_count, 6)


def _next_token_accuracy(encoded_samples: tuple[tuple[int, ...], ...], transitions: list[list[float]]) -> float:
    correct = 0
    total = 0
    for prev_token, next_token in _iter_bigram_pairs(encoded_samples):
        predicted = max(range(len(transitions[prev_token])), key=lambda idx: transitions[prev_token][idx])
        correct += int(predicted == next_token)
        total += 1
    if total == 0:
        raise TinyScratchTrainerError("encoded samples must contain eval pairs")
    return round(correct / total, 6)


def _transition_payload(transitions: list[list[float]]) -> dict[str, list[float]]:
    return {str(row_index): [round(value, 6) for value in row] for row_index, row in enumerate(transitions)}


def _require_samples(samples: tuple[str, ...]) -> None:
    if not isinstance(samples, tuple) or not samples:
        raise TinyScratchTrainerError("samples must be a non-empty tuple")
    if any(not isinstance(sample, str) or len(sample.strip()) < 2 for sample in samples):
        raise TinyScratchTrainerError("samples must contain strings with at least two non-space characters")


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TinyScratchTrainerError(f"{field_name} must be non-empty")


__all__ = [
    "TinyScratchDataset",
    "TinyScratchTrainerError",
    "TinyScratchTrainingControls",
    "train_tiny_scratch_model",
]
