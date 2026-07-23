"""Validation helpers for training record intake."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from vetinari.constants import (
    TRUNCATE_OUTPUT_PREVIEW,
    TRUNCATE_PROMPT_TRAINING,
    TRUNCATE_RESPONSE_TRAINING,
)
from vetinari.types import EvidenceBasis

from .training_record import TrainingRecord

logger = logging.getLogger(__name__)

_FALLBACK_RESPONSE_PATTERNS: frozenset[str] = frozenset({
    "",
    "{}",
    '{"content":"","sections":[]}',
    '{"content": "", "sections": []}',
})
_HF_ALLOWED_LICENSE_CLASSIFICATIONS: frozenset[str] = frozenset({
    "permissive",
    "permissive_open_source",
    "permissive_with_attribution",
    "apache-2.0",
    "apache_2_0",
    "mit",
    "bsd",
    "cc-by",
    "cc-by-4.0",
    "cc0",
})
_HF_BLOCKED_LICENSE_CLASSIFICATIONS: frozenset[str] = frozenset({
    "",
    "unknown",
    "unknown_blocked",
    "restricted",
    "non_commercial",
    "non-commercial",
    "proprietary_redistribution_blocked",
    "proprietary",
})


@dataclass(frozen=True, slots=True)
class _TrainingRecordInput:
    """Normalized inputs used to validate and build one training record."""

    task: str
    prompt: str
    response: str
    score: float
    model_id: str
    task_type: str
    prompt_variant_id: str
    agent_type: str
    latency_ms: int
    tokens_used: int
    success: bool
    metadata: dict[str, Any] | None
    benchmark_suite: str
    benchmark_pass: bool
    benchmark_score: float
    rejection_reason: str
    rejection_category: str
    inspector_feedback: str
    trace_id: str
    evidence_basis: EvidenceBasis | None
    target_stream: str | None

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"task={self.task!r}, "
            f"prompt={self.prompt!r}, "
            f"response={self.response!r}, "
            f"score={self.score!r}"
            ")"
        )


def _record_training_rejection(reason: str, model_id: str) -> None:
    """Best-effort skipped-record metric; never blocks data-quality rejection."""
    try:
        from vetinari.metrics import increment_training_records_skipped

        increment_training_records_skipped(reason=reason, model=model_id)
    except Exception:
        logger.warning("Failed to record training rejection metric", exc_info=True)


def _redact_training_value(value: Any) -> Any:
    """Return a JSON-like value with PII removed from all string leaves."""
    from vetinari.safety.guardrails import redact_pii_payload

    return redact_pii_payload(value)


def _privacy_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize privacy fields persisted with a training record."""
    normalized = _redact_training_value(dict(metadata or {}))
    if not isinstance(normalized, dict):
        normalized = {}
    consent_basis = str(normalized.get("training_consent") or normalized.get("consent_basis") or "local-only")
    normalized["consent_basis"] = consent_basis
    normalized.setdefault("pii_classification", "high-sensitivity-user-content")
    normalized["raw_content_redacted"] = True
    normalized.setdefault("retention_policy_id", "training-records-30d")
    return normalized


def _training_license_blocker(metadata: dict[str, Any] | None) -> str:
    if not metadata or not _metadata_indicates_hf_dataset(metadata):
        return ""
    raw_license = (
        str(
            metadata.get("license_classification")
            or metadata.get("dataset_license_classification")
            or metadata.get("license")
            or ""
        )
        .strip()
        .lower()
    )
    if raw_license in _HF_ALLOWED_LICENSE_CLASSIFICATIONS:
        return ""
    if raw_license in _HF_BLOCKED_LICENSE_CLASSIFICATIONS:
        return f"hf_dataset_license_blocked:{raw_license or 'missing'}"
    return f"hf_dataset_license_unrecognized:{raw_license}"


def _metadata_indicates_hf_dataset(metadata: dict[str, Any]) -> bool:
    source_kind = str(metadata.get("source_kind") or metadata.get("dataset_source_kind") or "").lower()
    if source_kind in {"hf_dataset", "huggingface_dataset", "huggingface"}:
        return True
    if metadata.get("hf_dataset_id") or metadata.get("huggingface_dataset_id"):
        return True
    source_uri = str(metadata.get("source_uri") or metadata.get("dataset_uri") or metadata.get("source") or "").lower()
    return source_uri.startswith(("hf://", "https://huggingface.co/datasets/"))


def _resolve_basis_and_stream(input_data: _TrainingRecordInput) -> tuple[str, str]:
    """Resolve typed evidence metadata before applying stream filters."""
    metadata = input_data.metadata or {}
    meta_basis = str(metadata.get("evidence_basis", ""))
    meta_stream = str(metadata.get("training_stream", ""))
    if input_data.evidence_basis is not None:
        if meta_basis and meta_basis != input_data.evidence_basis.value:
            logger.warning(
                "[TrainingDataCollector] evidence_basis mismatch: typed param=%s, "
                "metadata=%s - using typed param (model=%s)",
                input_data.evidence_basis.value,
                meta_basis,
                input_data.model_id,
            )
        resolved_basis = input_data.evidence_basis.value
    else:
        resolved_basis = meta_basis
    resolved_stream = input_data.target_stream if input_data.target_stream is not None else meta_stream
    return resolved_basis, resolved_stream


_TrainingRejectionRecorder = Callable[[str, str], None]


def _passes_basic_training_gates(
    input_data: _TrainingRecordInput,
    record_rejection: _TrainingRejectionRecorder,
) -> bool:
    """Apply fallback, provenance, and license gates before record creation."""
    if input_data.tokens_used == 0:
        record_rejection("tokens_zero", input_data.model_id)
        logger.warning(
            "[TrainingDataCollector] Rejected record: tokens_used=0 (likely fallback/mock), model=%s",
            input_data.model_id,
        )
        return False
    if input_data.latency_ms == 0:
        record_rejection("latency_zero", input_data.model_id)
        logger.warning(
            "[TrainingDataCollector] Rejected record: latency_ms=0 (likely fallback/mock), model=%s",
            input_data.model_id,
        )
        return False
    if input_data.metadata and input_data.metadata.get("_is_fallback"):
        record_rejection("fallback", input_data.model_id)
        logger.warning(
            "[TrainingDataCollector] Rejected record: _is_fallback=True, model=%s",
            input_data.model_id,
        )
        return False

    resolved_basis, resolved_stream = _resolve_basis_and_stream(input_data)
    if resolved_basis == EvidenceBasis.LLM_JUDGMENT.value and resolved_stream == "tool_evidence":
        logger.warning(
            "[TrainingDataCollector] Rejected record: LLM-judgment signal (basis=%s) cannot enter "
            "tool-evidence training stream (stream=%s, model=%s, task_type=%s)",
            resolved_basis,
            resolved_stream,
            input_data.model_id,
            input_data.task_type,
        )
        return False

    license_blocker = _training_license_blocker(input_data.metadata)
    if license_blocker:
        record_rejection("license_blocked", input_data.model_id)
        logger.warning(
            "[TrainingDataCollector] Rejected record: %s, model=%s, task_type=%s",
            license_blocker,
            input_data.model_id,
            input_data.task_type,
        )
        return False

    stripped = input_data.response.strip() if input_data.response else ""
    if stripped in _FALLBACK_RESPONSE_PATTERNS:
        record_rejection("fallback_pattern", input_data.model_id)
        logger.warning(
            "[TrainingDataCollector] Rejected record: response matches fallback pattern, model=%s",
            input_data.model_id,
        )
        return False
    return True


def _passes_secrets_filter(input_data: _TrainingRecordInput) -> bool:
    """Run the fail-closed training secrets filter."""
    try:
        from vetinari.learning.secrets_filter import filter_training_record

        is_safe, detections = filter_training_record(input_data.prompt, input_data.response)
    except Exception:
        logger.exception(
            "[TrainingDataCollector] Secrets filter raised an exception for model=%s"
            " - blocking record to fail closed; investigate the filter error above",
            input_data.model_id,
        )
        return False
    if is_safe:
        return True
    logger.warning(
        "[TrainingDataCollector] Rejected record: secrets detected (%d findings), model=%s",
        len(detections),
        input_data.model_id,
    )
    return False


def _snapshot_vram_used_gb() -> float:
    """Best-effort VRAM snapshot for training telemetry."""
    try:
        from vetinari.models.vram_manager import get_vram_manager

        return float(get_vram_manager().get_used_vram_gb())
    except Exception:
        logger.warning("Failed to snapshot VRAM usage for training record", exc_info=True)
        return 0.0


def _make_training_record(input_data: _TrainingRecordInput, vram_used: float) -> TrainingRecord:
    """Create the persisted TrainingRecord after all gates pass."""
    redacted_task = str(_redact_training_value(input_data.task))
    redacted_prompt = str(_redact_training_value(input_data.prompt))
    redacted_response = str(_redact_training_value(input_data.response))
    redacted_rejection_reason = str(_redact_training_value(input_data.rejection_reason))
    redacted_inspector_feedback = str(_redact_training_value(input_data.inspector_feedback))
    return TrainingRecord(
        record_id=f"tr_{uuid.uuid4().hex[:8]}",
        timestamp=datetime.now(timezone.utc).isoformat(),
        task=redacted_task[:TRUNCATE_OUTPUT_PREVIEW],
        prompt=redacted_prompt[:TRUNCATE_PROMPT_TRAINING],
        response=redacted_response[:TRUNCATE_RESPONSE_TRAINING],
        score=round(input_data.score, 4),
        model_id=input_data.model_id,
        task_type=input_data.task_type,
        prompt_variant_id=input_data.prompt_variant_id,
        agent_type=input_data.agent_type,
        latency_ms=input_data.latency_ms,
        tokens_used=input_data.tokens_used,
        success=input_data.success,
        vram_used_gb=round(vram_used, 2),
        benchmark_suite=input_data.benchmark_suite,
        benchmark_pass=input_data.benchmark_pass,
        benchmark_score=round(input_data.benchmark_score, 4),
        rejection_reason=redacted_rejection_reason,
        rejection_category=input_data.rejection_category,
        inspector_feedback=redacted_inspector_feedback,
        trace_id=input_data.trace_id,
        metadata=_privacy_metadata(input_data.metadata),
    )


def _build_training_record(
    input_data: _TrainingRecordInput,
    *,
    record_rejection_fn: _TrainingRejectionRecorder = _record_training_rejection,
) -> TrainingRecord | None:
    """Return a training record when all intake gates accept the data."""
    if not _passes_basic_training_gates(input_data, record_rejection_fn):
        return None
    if not _passes_secrets_filter(input_data):
        return None
    return _make_training_record(input_data, _snapshot_vram_used_gb())
