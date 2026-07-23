"""Runtime contracts for the multimodal media workbench.

The layer composes existing media assets and eval records into review,
voice-harness, adapter-slot, and multimodal dataset records. It keeps external
streaming and telephony providers optional while making provenance, redactions,
and operator-facing review state explicit.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.media import MediaAsset
from vetinari.workbench.multimodal.media_contracts import (
    AdapterSlot,
    AdapterSlotKind,
    MediaReviewRecord,
    MultimodalDatasetCase,
    MultimodalEvalDataset,
    MultimodalWorkbenchConfig,
    MultimodalWorkbenchError,
    TurnTakingMetric,
    VoiceHarnessResult,
    VoiceTurn,
    _parse_adapter_slot,
    _string_tuple,
)

DEFAULT_MULTIMODAL_CONFIG_PATH = PROJECT_ROOT / "config" / "workbench_multimodal.yaml"


class MultimodalMediaWorkbench:
    """In-memory runtime facade for assembling multimodal workbench records."""

    def __init__(self, config: MultimodalWorkbenchConfig) -> None:
        self.config = config

    def build_review(
        self,
        *,
        review_id: str,
        media_asset: MediaAsset,
        reviewer_ref: str,
        annotation_refs: Iterable[str],
        redaction_refs: Iterable[str] = (),
        scene_refs: Iterable[str] = (),
        provenance_refs: Iterable[str],
        risk_flags: Iterable[str] = (),
    ) -> MediaReviewRecord:
        """Create a trusted media review record."""
        return build_media_review_record(
            review_id=review_id,
            media_asset=media_asset,
            reviewer_ref=reviewer_ref,
            annotation_refs=annotation_refs,
            redaction_refs=redaction_refs,
            scene_refs=scene_refs,
            provenance_refs=provenance_refs,
            risk_flags=risk_flags,
        )

    def build_voice_harness(
        self,
        *,
        harness_id: str,
        media_asset_id: str,
        adapter_slot_id: str,
        turns: Iterable[VoiceTurn],
        provenance_refs: Iterable[str],
        latency_budget_ms: int | None = None,
    ) -> VoiceHarnessResult:
        """Create turn-taking metrics for a configured adapter slot.

        Returns:
            Newly constructed voice harness value.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if adapter_slot_id not in {slot.slot_id for slot in self.config.adapter_slots}:
            raise MultimodalWorkbenchError("adapter_slot_id must reference configured adapter slot")
        return build_voice_harness_result(
            harness_id=harness_id,
            media_asset_id=media_asset_id,
            adapter_slot_id=adapter_slot_id,
            turns=turns,
            provenance_refs=provenance_refs,
            latency_budget_ms=latency_budget_ms or self.config.default_latency_budget_ms,
        )

    def build_eval_dataset(
        self,
        *,
        dataset_id: str,
        dataset_revision_id: str,
        cases: Iterable[MultimodalDatasetCase],
        provenance_refs: Iterable[str],
        required_modalities: Iterable[str] | None = None,
    ) -> MultimodalEvalDataset:
        """Create a multimodal eval dataset record."""
        return build_multimodal_eval_dataset(
            dataset_id=dataset_id,
            dataset_revision_id=dataset_revision_id,
            cases=cases,
            required_modalities=required_modalities or self.config.required_modalities,
            provenance_refs=provenance_refs,
        )


def build_media_review_record(
    *,
    review_id: str,
    media_asset: MediaAsset,
    reviewer_ref: str,
    annotation_refs: Iterable[str],
    redaction_refs: Iterable[str] = (),
    scene_refs: Iterable[str] = (),
    provenance_refs: Iterable[str],
    risk_flags: Iterable[str] = (),
) -> MediaReviewRecord:
    """Normalize a media asset into an operator-review record.

    Returns:
        Newly constructed media review record value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    try:
        if not media_asset.transcript_alignments and not media_asset.frame_samples:
            raise MultimodalWorkbenchError("media review requires transcript alignment or frame samples")
    except AttributeError as exc:
        raise MultimodalWorkbenchError("media_asset must use the media runtime contract") from exc
    return MediaReviewRecord(
        review_id=review_id,
        media_asset=media_asset,
        reviewer_ref=reviewer_ref,
        annotation_refs=tuple(annotation_refs),
        redaction_refs=tuple(redaction_refs),
        scene_refs=tuple(scene_refs),
        provenance_refs=tuple(provenance_refs),
        risk_flags=tuple(risk_flags),
    )


def build_voice_harness_result(
    *,
    harness_id: str,
    media_asset_id: str,
    adapter_slot_id: str,
    turns: Iterable[VoiceTurn],
    provenance_refs: Iterable[str],
    latency_budget_ms: int,
) -> VoiceHarnessResult:
    """Build voice-agent latency, turn-taking, and barge-in metrics.

    Returns:
        Newly constructed voice harness result value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    normalized_turns = tuple(turns)
    agent_turns = tuple(turn for turn in normalized_turns if turn.role == "agent")
    if not agent_turns:
        raise MultimodalWorkbenchError("voice harness requires at least one agent turn")
    max_latency = max(turn.response_latency_ms for turn in agent_turns)
    average_latency = sum(turn.response_latency_ms for turn in agent_turns) / len(agent_turns)
    barge_in_count = sum(1 for turn in normalized_turns if turn.barge_in)
    interruption_count = sum(1 for turn in normalized_turns if turn.interrupted_turn_id)
    metrics = TurnTakingMetric(
        average_agent_latency_ms=average_latency,
        max_agent_latency_ms=max_latency,
        barge_in_count=barge_in_count,
        interruption_count=interruption_count,
        latency_budget_ms=latency_budget_ms,
        passed=max_latency <= latency_budget_ms and interruption_count >= barge_in_count,
    )
    return VoiceHarnessResult(
        harness_id=harness_id,
        media_asset_id=media_asset_id,
        adapter_slot_id=adapter_slot_id,
        turns=normalized_turns,
        metrics=metrics,
        provenance_refs=tuple(provenance_refs),
    )


def build_multimodal_eval_dataset(
    *,
    dataset_id: str,
    dataset_revision_id: str,
    cases: Iterable[MultimodalDatasetCase],
    required_modalities: Iterable[str],
    provenance_refs: Iterable[str],
) -> MultimodalEvalDataset:
    """Build a dataset and verify every case binds review and eval identity.

    Returns:
        Newly constructed multimodal eval dataset value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    normalized_cases = tuple(cases)
    case_ids = [case.case_id for case in normalized_cases]
    if len(case_ids) != len(set(case_ids)):
        raise MultimodalWorkbenchError("MultimodalEvalDataset case ids must be unique")
    for case in normalized_cases:
        if case.eval_result.asset_id != case.media_asset_id:
            raise MultimodalWorkbenchError("eval_result.asset_id must match media_asset_id")
        if case.eval_result.asset_revision != dataset_revision_id:
            raise MultimodalWorkbenchError("eval_result.asset_revision must match dataset_revision_id")
    return MultimodalEvalDataset(
        dataset_id=dataset_id,
        dataset_revision_id=dataset_revision_id,
        cases=normalized_cases,
        required_modalities=tuple(required_modalities),
        provenance_refs=tuple(provenance_refs),
    )


def load_multimodal_workbench_config(
    path: Path | str = DEFAULT_MULTIMODAL_CONFIG_PATH,
) -> MultimodalWorkbenchConfig:
    """Load the multimodal workbench config and fail closed on damaged YAML.

    Returns:
        Resolved multimodal workbench config value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MultimodalWorkbenchError(f"multimodal config unreadable: {config_path}") from exc
    if not isinstance(raw, Mapping):
        raise MultimodalWorkbenchError("multimodal config must be a mapping")
    slots = raw.get("adapter_slots")
    if not isinstance(slots, list):
        raise MultimodalWorkbenchError("adapter_slots must be a list")
    return MultimodalWorkbenchConfig(
        schema_version=int(raw.get("schema_version", 0)),
        adapter_slots=tuple(_parse_adapter_slot(row) for row in slots),
        default_latency_budget_ms=int(raw.get("default_latency_budget_ms", 0)),
        required_modalities=_string_tuple(raw.get("required_modalities", ())),
    )


__all__ = [
    "DEFAULT_MULTIMODAL_CONFIG_PATH",
    "AdapterSlot",
    "AdapterSlotKind",
    "MediaReviewRecord",
    "MultimodalDatasetCase",
    "MultimodalEvalDataset",
    "MultimodalMediaWorkbench",
    "MultimodalWorkbenchConfig",
    "MultimodalWorkbenchError",
    "TurnTakingMetric",
    "VoiceHarnessResult",
    "VoiceTurn",
    "build_media_review_record",
    "build_multimodal_eval_dataset",
    "build_voice_harness_result",
    "load_multimodal_workbench_config",
]
