"""Typed artifacts for Workbench tiny scratch-model runs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from vetinari.api.responses import json_safe as _json_safe
from vetinari.workbench.evals import EvalResult
from vetinari.workbench.spine_consumers import record_asset_written


class TinyScratchArtifactError(ValueError):
    """Raised when a tiny scratch-model artifact is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class TinyTrainingReceipt:
    """Evidence receipt for one required governance or runtime gate."""

    receipt_id: str
    kind: str
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.receipt_id, "receipt_id")
        _require_non_empty(self.kind, "kind")
        _require_non_empty(self.evidence_ref, "evidence_ref")


@dataclass(frozen=True, slots=True)
class TinyTrainingMetrics:
    """Small, honest metrics emitted by the deterministic tiny trainer."""

    token_count: int
    vocab_size: int
    epoch_count: int
    training_loss: float
    eval_score: float
    eval_threshold: float
    confidence: float
    eval_passed: bool

    def __post_init__(self) -> None:
        if self.token_count <= 0:
            raise TinyScratchArtifactError("token_count must be positive")
        if self.vocab_size <= 0:
            raise TinyScratchArtifactError("vocab_size must be positive")
        if self.epoch_count <= 0:
            raise TinyScratchArtifactError("epoch_count must be positive")
        for field_name in ("training_loss", "eval_score", "eval_threshold", "confidence"):
            value = float(getattr(self, field_name))
            if value < 0:
                raise TinyScratchArtifactError(f"{field_name} must be non-negative")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TinyTrainingMetrics(token_count={self.token_count!r}, vocab_size={self.vocab_size!r}, epoch_count={self.epoch_count!r})"


@dataclass(frozen=True, slots=True)
class TinyCheckpoint:
    """Checkpoint row for resume, rollback, and audit display."""

    checkpoint_id: str
    artifact_ref: str
    epoch: int
    metrics: TinyTrainingMetrics
    resume_supported: bool
    rollback_supported: bool

    def __post_init__(self) -> None:
        _require_non_empty(self.checkpoint_id, "checkpoint_id")
        _require_non_empty(self.artifact_ref, "artifact_ref")
        if self.epoch <= 0:
            raise TinyScratchArtifactError("checkpoint epoch must be positive")
        if not isinstance(self.metrics, TinyTrainingMetrics):
            raise TinyScratchArtifactError("checkpoint metrics must be TinyTrainingMetrics")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TinyCheckpoint(checkpoint_id={self.checkpoint_id!r}, artifact_ref={self.artifact_ref!r}, epoch={self.epoch!r})"


@dataclass(frozen=True, slots=True)
class TinyModelCard:
    """Machine-readable model card for an experimental tiny scratch model."""

    card_id: str
    display_name: str
    intended_use: str
    limitations: tuple[str, ...]
    provenance: dict[str, str]
    evidence_ids: tuple[str, ...]
    policy_ref: str

    def __post_init__(self) -> None:
        _require_non_empty(self.card_id, "card_id")
        _require_non_empty(self.display_name, "display_name")
        _require_non_empty(self.intended_use, "intended_use")
        _require_string_tuple(self.limitations, "limitations")
        _require_string_tuple(self.evidence_ids, "evidence_ids")
        _require_non_empty(self.policy_ref, "policy_ref")
        if not self.provenance or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in self.provenance.items()
        ):
            raise TinyScratchArtifactError("provenance must be a non-empty dict[str, str]")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TinyModelCard(card_id={self.card_id!r}, display_name={self.display_name!r}, intended_use={self.intended_use!r})"


@dataclass(frozen=True, slots=True)
class TinyExperimentalRegistryEntry:
    """Experimental registry row; it is not a serving-route approval."""

    registry_id: str
    model_id: str
    artifact_ref: str
    status: str
    route_eligible: bool
    blockers: tuple[str, ...]
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.registry_id, "registry_id")
        _require_non_empty(self.model_id, "model_id")
        _require_non_empty(self.artifact_ref, "artifact_ref")
        if self.status not in {"experimental_registered", "blocked_failed_eval"}:
            raise TinyScratchArtifactError("registry status must be experimental_registered or blocked_failed_eval")
        _require_string_tuple(self.evidence_ids, "evidence_ids")
        if self.route_eligible:
            raise TinyScratchArtifactError("tiny scratch MVP cannot mark a model route eligible")
        if self.status == "blocked_failed_eval" and "eval-gate-failed" not in self.blockers:
            raise TinyScratchArtifactError("failed eval registry rows require eval-gate-failed blocker")
        if self.status == "experimental_registered" and "operator-approval-required" not in self.blockers:
            raise TinyScratchArtifactError("experimental rows require operator approval before routing")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TinyExperimentalRegistryEntry(registry_id={self.registry_id!r}, model_id={self.model_id!r}, artifact_ref={self.artifact_ref!r})"


@dataclass(frozen=True, slots=True)
class TinyScratchRunArtifact:
    """Durable run artifact for a governed tiny scratch-model experiment."""

    schema_version: int
    run_id: str
    dataset_revision_id: str
    tokenizer: dict[str, object]
    model_parameters: dict[str, object]
    metrics: TinyTrainingMetrics
    receipts: tuple[TinyTrainingReceipt, ...]
    checkpoints: tuple[TinyCheckpoint, ...]
    model_card: TinyModelCard
    eval_result: EvalResult
    registry_entry: TinyExperimentalRegistryEntry

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise TinyScratchArtifactError("schema_version must be 1")
        _require_non_empty(self.run_id, "run_id")
        _require_non_empty(self.dataset_revision_id, "dataset_revision_id")
        if not self.tokenizer:
            raise TinyScratchArtifactError("tokenizer payload must be present")
        if not self.model_parameters:
            raise TinyScratchArtifactError("model_parameters must be present")
        if not isinstance(self.metrics, TinyTrainingMetrics):
            raise TinyScratchArtifactError("metrics must be TinyTrainingMetrics")
        if not self.receipts:
            raise TinyScratchArtifactError("receipts must be non-empty")
        if not self.checkpoints:
            raise TinyScratchArtifactError("checkpoints must be non-empty")
        if not isinstance(self.model_card, TinyModelCard):
            raise TinyScratchArtifactError("model_card must be TinyModelCard")
        if not isinstance(self.eval_result, EvalResult):
            raise TinyScratchArtifactError("eval_result must be EvalResult")
        if not isinstance(self.registry_entry, TinyExperimentalRegistryEntry):
            raise TinyScratchArtifactError("registry_entry must be TinyExperimentalRegistryEntry")
        if self.eval_result.run_id != self.run_id:
            raise TinyScratchArtifactError("eval_result must reference the tiny run_id")
        if self.eval_result.asset_revision != self.dataset_revision_id:
            raise TinyScratchArtifactError("eval_result must reference the governed dataset revision")
        if self.metrics.eval_passed and self.registry_entry.status != "experimental_registered":
            raise TinyScratchArtifactError("passing eval must create an experimental registry entry")
        if not self.metrics.eval_passed and self.registry_entry.status != "blocked_failed_eval":
            raise TinyScratchArtifactError("failed eval must block registry eligibility")

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe artifact payload."""
        return _json_safe(self)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TinyScratchRunArtifact(schema_version={self.schema_version!r}, run_id={self.run_id!r}, dataset_revision_id={self.dataset_revision_id!r})"


def write_tiny_run_artifact(artifact: TinyScratchRunArtifact, path: str | Path) -> Path:
    """Persist a tiny run artifact with atomic replace semantics.

    Args:
        artifact: Artifact value consumed by write_tiny_run_artifact().
        path: Filesystem path read or written by the operation.

    Returns:
        Path value produced by write_tiny_run_artifact().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(artifact, TinyScratchRunArtifact):
        raise TinyScratchArtifactError("artifact must be TinyScratchRunArtifact")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    payload = json.dumps(artifact.to_dict(), indent=2, sort_keys=True) + "\n"
    try:
        tmp.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(tmp, target)
        # spine_consumers invokes get_spine() and absorbs observability failures.
        record_asset_written(
            asset_id=artifact.run_id,
            kind="model",
            project_id="default",
            path=str(target),
            redact_fields=["path"],
        )
    except OSError as exc:
        raise TinyScratchArtifactError(f"unable to write tiny run artifact {target}") from exc
    return target


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise TinyScratchArtifactError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise TinyScratchArtifactError(f"{field_name} must be a non-empty tuple")
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise TinyScratchArtifactError(f"{field_name} must contain non-empty strings")


__all__ = [
    "TinyCheckpoint",
    "TinyExperimentalRegistryEntry",
    "TinyModelCard",
    "TinyScratchArtifactError",
    "TinyScratchRunArtifact",
    "TinyTrainingMetrics",
    "TinyTrainingReceipt",
    "write_tiny_run_artifact",
]
