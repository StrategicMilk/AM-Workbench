"""Contracts for the multimodal media workbench."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum

from vetinari.workbench.evals import EvalResult
from vetinari.workbench.media import MediaAsset


class MultimodalWorkbenchError(ValueError):
    """Raised when a multimodal workbench record cannot be trusted."""


class AdapterSlotKind(str, Enum):
    """Optional adapter slots for live or replayed multimodal sessions."""

    LOCAL_FILE = "local_file"
    STREAMING = "streaming"
    TELEPHONY = "telephony"


@dataclass(frozen=True, slots=True)
class AdapterSlot:
    """Declared adapter slot without requiring a provider integration."""

    slot_id: str
    kind: AdapterSlotKind
    required_capabilities: tuple[str, ...]
    optional: bool = True

    def __post_init__(self) -> None:
        _require_non_empty(self.slot_id, "AdapterSlot.slot_id")
        object.__setattr__(self, "kind", AdapterSlotKind(self.kind))
        _set_string_tuple(
            self,
            "required_capabilities",
            self.required_capabilities,
            field_name="AdapterSlot.required_capabilities",
            require_non_empty=True,
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AdapterSlot(slot_id={self.slot_id!r}, kind={self.kind!r}, required_capabilities={self.required_capabilities!r})"


@dataclass(frozen=True, slots=True)
class MediaReviewRecord:
    """Operator-review record for one structured media asset."""

    review_id: str
    media_asset: MediaAsset
    reviewer_ref: str
    annotation_refs: tuple[str, ...]
    redaction_refs: tuple[str, ...]
    scene_refs: tuple[str, ...]
    provenance_refs: tuple[str, ...]
    risk_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.review_id, "MediaReviewRecord.review_id")
        _require_non_empty(self.reviewer_ref, "MediaReviewRecord.reviewer_ref")
        if not isinstance(self.media_asset, MediaAsset):
            raise MultimodalWorkbenchError("MediaReviewRecord.media_asset must be a MediaAsset")
        _set_string_tuple(
            self,
            "annotation_refs",
            self.annotation_refs,
            field_name="MediaReviewRecord.annotation_refs",
            require_non_empty=True,
        )
        _set_string_tuple(self, "redaction_refs", self.redaction_refs, field_name="MediaReviewRecord.redaction_refs")
        _set_string_tuple(self, "scene_refs", self.scene_refs, field_name="MediaReviewRecord.scene_refs")
        _set_string_tuple(
            self,
            "provenance_refs",
            self.provenance_refs,
            field_name="MediaReviewRecord.provenance_refs",
            require_non_empty=True,
        )
        _set_string_tuple(self, "risk_flags", self.risk_flags, field_name="MediaReviewRecord.risk_flags")
        if self.media_asset.redactions and not self.redaction_refs:
            raise MultimodalWorkbenchError("MediaReviewRecord.redaction_refs must cover media redactions")
        if self.media_asset.scenes and not self.scene_refs:
            raise MultimodalWorkbenchError("MediaReviewRecord.scene_refs must cover media scenes")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MediaReviewRecord(review_id={self.review_id!r}, media_asset={self.media_asset!r}, reviewer_ref={self.reviewer_ref!r})"


@dataclass(frozen=True, slots=True)
class VoiceTurn:
    """One user or agent turn aligned to media time."""

    turn_id: str
    speaker_id: str
    role: str
    start_seconds: float
    end_seconds: float
    response_latency_ms: int
    interrupted_turn_id: str = ""
    barge_in: bool = False

    def __post_init__(self) -> None:
        _require_non_empty(self.turn_id, "VoiceTurn.turn_id")
        _require_non_empty(self.speaker_id, "VoiceTurn.speaker_id")
        if self.role not in {"user", "agent", "system"}:
            raise MultimodalWorkbenchError("VoiceTurn.role must be user, agent, or system")
        _require_time_range(self.start_seconds, self.end_seconds, "VoiceTurn")
        if self.response_latency_ms < 0:
            raise MultimodalWorkbenchError("VoiceTurn.response_latency_ms must be non-negative")
        if self.barge_in and not self.interrupted_turn_id:
            raise MultimodalWorkbenchError("VoiceTurn.barge_in requires interrupted_turn_id")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"VoiceTurn(turn_id={self.turn_id!r}, speaker_id={self.speaker_id!r}, role={self.role!r})"


@dataclass(frozen=True, slots=True)
class TurnTakingMetric:
    """Voice-agent latency and interruption summary."""

    average_agent_latency_ms: float
    max_agent_latency_ms: int
    barge_in_count: int
    interruption_count: int
    latency_budget_ms: int
    passed: bool

    def __post_init__(self) -> None:
        if self.average_agent_latency_ms < 0:
            raise MultimodalWorkbenchError("average_agent_latency_ms must be non-negative")
        if self.max_agent_latency_ms < 0:
            raise MultimodalWorkbenchError("max_agent_latency_ms must be non-negative")
        if self.latency_budget_ms <= 0:
            raise MultimodalWorkbenchError("latency_budget_ms must be positive")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TurnTakingMetric(average_agent_latency_ms={self.average_agent_latency_ms!r}, max_agent_latency_ms={self.max_agent_latency_ms!r}, barge_in_count={self.barge_in_count!r})"


@dataclass(frozen=True, slots=True)
class VoiceHarnessResult:
    """Voice-agent test harness result over a media asset."""

    harness_id: str
    media_asset_id: str
    adapter_slot_id: str
    turns: tuple[VoiceTurn, ...]
    metrics: TurnTakingMetric
    provenance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.harness_id, "VoiceHarnessResult.harness_id")
        _require_non_empty(self.media_asset_id, "VoiceHarnessResult.media_asset_id")
        _require_non_empty(self.adapter_slot_id, "VoiceHarnessResult.adapter_slot_id")
        _set_tuple(self, "turns", self.turns, expected=VoiceTurn, field_name="VoiceHarnessResult.turns")
        if not self.turns:
            raise MultimodalWorkbenchError("VoiceHarnessResult.turns must be non-empty")
        if not isinstance(self.metrics, TurnTakingMetric):
            raise MultimodalWorkbenchError("VoiceHarnessResult.metrics must be TurnTakingMetric")
        _set_string_tuple(
            self,
            "provenance_refs",
            self.provenance_refs,
            field_name="VoiceHarnessResult.provenance_refs",
            require_non_empty=True,
        )
        turn_ids = {turn.turn_id for turn in self.turns}
        for turn in self.turns:
            if turn.interrupted_turn_id and turn.interrupted_turn_id not in turn_ids:
                raise MultimodalWorkbenchError("VoiceHarnessResult interruption target must exist")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"VoiceHarnessResult(harness_id={self.harness_id!r}, media_asset_id={self.media_asset_id!r}, adapter_slot_id={self.adapter_slot_id!r})"


@dataclass(frozen=True, slots=True)
class MultimodalDatasetCase:
    """One media review/harness/eval binding inside a dataset."""

    case_id: str
    media_asset_id: str
    review_id: str
    eval_result: EvalResult
    harness_id: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.case_id, "MultimodalDatasetCase.case_id")
        _require_non_empty(self.media_asset_id, "MultimodalDatasetCase.media_asset_id")
        _require_non_empty(self.review_id, "MultimodalDatasetCase.review_id")
        if not isinstance(self.eval_result, EvalResult):
            raise MultimodalWorkbenchError("MultimodalDatasetCase.eval_result must be EvalResult")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MultimodalDatasetCase(case_id={self.case_id!r}, media_asset_id={self.media_asset_id!r}, review_id={self.review_id!r})"


@dataclass(frozen=True, slots=True)
class MultimodalEvalDataset:
    """First-class multimodal eval dataset record."""

    dataset_id: str
    dataset_revision_id: str
    cases: tuple[MultimodalDatasetCase, ...]
    required_modalities: tuple[str, ...]
    provenance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.dataset_id, "MultimodalEvalDataset.dataset_id")
        _require_non_empty(self.dataset_revision_id, "MultimodalEvalDataset.dataset_revision_id")
        _set_tuple(self, "cases", self.cases, expected=MultimodalDatasetCase, field_name="MultimodalEvalDataset.cases")
        if not self.cases:
            raise MultimodalWorkbenchError("MultimodalEvalDataset.cases must be non-empty")
        _set_string_tuple(
            self,
            "required_modalities",
            self.required_modalities,
            field_name="MultimodalEvalDataset.required_modalities",
            require_non_empty=True,
        )
        _set_string_tuple(
            self,
            "provenance_refs",
            self.provenance_refs,
            field_name="MultimodalEvalDataset.provenance_refs",
            require_non_empty=True,
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MultimodalEvalDataset(dataset_id={self.dataset_id!r}, dataset_revision_id={self.dataset_revision_id!r}, cases={self.cases!r})"


@dataclass(frozen=True, slots=True)
class MultimodalWorkbenchConfig:
    """Validated YAML-backed workbench defaults."""

    schema_version: int
    adapter_slots: tuple[AdapterSlot, ...]
    default_latency_budget_ms: int
    required_modalities: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise MultimodalWorkbenchError("multimodal config schema_version must be 1")
        _set_tuple(self, "adapter_slots", self.adapter_slots, expected=AdapterSlot, field_name="adapter_slots")
        if not self.adapter_slots:
            raise MultimodalWorkbenchError("adapter_slots must be non-empty")
        kinds = {slot.kind for slot in self.adapter_slots}
        if {AdapterSlotKind.STREAMING, AdapterSlotKind.TELEPHONY} - kinds:
            raise MultimodalWorkbenchError("streaming and telephony adapter slots must be declared")
        if self.default_latency_budget_ms <= 0:
            raise MultimodalWorkbenchError("default_latency_budget_ms must be positive")
        _set_string_tuple(
            self,
            "required_modalities",
            self.required_modalities,
            field_name="required_modalities",
            require_non_empty=True,
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MultimodalWorkbenchConfig(schema_version={self.schema_version!r}, adapter_slots={self.adapter_slots!r}, default_latency_budget_ms={self.default_latency_budget_ms!r})"


def _parse_adapter_slot(raw: object) -> AdapterSlot:
    if not isinstance(raw, Mapping):
        raise MultimodalWorkbenchError("adapter slot row must be a mapping")
    return AdapterSlot(
        slot_id=str(raw.get("id", "")),
        kind=AdapterSlotKind(str(raw.get("kind", ""))),
        required_capabilities=_string_tuple(raw.get("required_capabilities", ())),
        optional=bool(raw.get("optional", True)),
    )


def _require_non_empty(value: str | None, field_name: str) -> None:
    if value is None or not str(value).strip():
        raise MultimodalWorkbenchError(f"{field_name} must be non-empty")


def _require_time_range(start_seconds: float, end_seconds: float, field_name: str) -> None:
    if start_seconds < 0:
        raise MultimodalWorkbenchError(f"{field_name}.start_seconds must be non-negative")
    if end_seconds <= start_seconds:
        raise MultimodalWorkbenchError(f"{field_name}.end_seconds must be greater than start_seconds")


def _set_string_tuple(
    instance: object,
    attr: str,
    values: Iterable[str],
    *,
    field_name: str,
    require_non_empty: bool = False,
) -> None:
    normalized = _string_tuple(values)
    if require_non_empty and not normalized:
        raise MultimodalWorkbenchError(f"{field_name} must be non-empty")
    object.__setattr__(instance, attr, normalized)


def _set_tuple(
    instance: object,
    attr: str,
    values: Iterable[object],
    *,
    expected: type[object],
    field_name: str,
) -> None:
    normalized = tuple(values)
    if any(not isinstance(value, expected) for value in normalized):
        raise MultimodalWorkbenchError(f"{field_name} must contain {expected.__name__} instances")
    object.__setattr__(instance, attr, normalized)


def _string_tuple(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    if isinstance(raw, Iterable) and not isinstance(raw, (bytes, Mapping)):
        return tuple(str(item) for item in raw if str(item).strip())
    return (str(raw),) if str(raw).strip() else ()
