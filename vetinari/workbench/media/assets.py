"""Immutable media intelligence records for Workbench assets."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from vetinari.security.redaction import redact_repr

_SHA256_RE = re.compile(r"^[a-fA-F0-9]{64}$")


class MediaAssetError(ValueError):
    """Raised when media evidence cannot be trusted."""


class MediaAssetKind(str, Enum):
    """Media asset kinds supported by the intelligence substrate."""

    AUDIO = "audio"
    VIDEO = "video"
    IMAGE_SEQUENCE = "image_sequence"


class MediaRedactionStatus(str, Enum):
    """Lifecycle state for a media redaction reference."""

    PROPOSED = "proposed"
    REVIEWED = "reviewed"
    APPLIED_UPSTREAM = "applied_upstream"


@dataclass(frozen=True, slots=True)
class MediaSegment:
    """Time-bounded audio or video segment."""

    segment_id: str
    start_seconds: float
    end_seconds: float
    confidence: float
    provenance_refs: tuple[str, ...]
    label: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.segment_id, "MediaSegment.segment_id")
        _require_time_range(self.start_seconds, self.end_seconds, "MediaSegment")
        _require_confidence(self.confidence, "MediaSegment.confidence")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="MediaSegment.provenance_refs")
        if not self.provenance_refs:
            raise MediaAssetError("MediaSegment.provenance_refs must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MediaSegment(segment_id={self.segment_id!r}, start_seconds={self.start_seconds!r}, end_seconds={self.end_seconds!r})"


@dataclass(frozen=True, slots=True)
class TranscriptAlignment:
    """Transcript text aligned to one media segment and time range."""

    alignment_id: str
    segment_id: str
    transcript_text: str
    start_seconds: float
    end_seconds: float
    confidence: float
    provenance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.alignment_id, "TranscriptAlignment.alignment_id")
        _require_non_empty(self.segment_id, "TranscriptAlignment.segment_id")
        _require_non_empty(self.transcript_text, "TranscriptAlignment.transcript_text")
        _require_time_range(self.start_seconds, self.end_seconds, "TranscriptAlignment")
        _require_confidence(self.confidence, "TranscriptAlignment.confidence")
        _set_tuple(
            self,
            "provenance_refs",
            self.provenance_refs,
            field_name="TranscriptAlignment.provenance_refs",
        )
        if not self.provenance_refs:
            raise MediaAssetError("TranscriptAlignment.provenance_refs must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return redact_repr(
            "TranscriptAlignment",
            {
                "alignment_id": self.alignment_id,
                "segment_id": self.segment_id,
                "transcript_text": self.transcript_text,
            },
        )


@dataclass(frozen=True, slots=True)
class FrameSample:
    """Sampled frame evidence with visual/object grounding."""

    frame_id: str
    timestamp_seconds: float
    provenance_refs: tuple[str, ...]
    visual_grounding_ref: str | None = None
    object_labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.frame_id, "FrameSample.frame_id")
        if self.timestamp_seconds < 0:
            raise MediaAssetError("FrameSample.timestamp_seconds must be non-negative")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="FrameSample.provenance_refs")
        _set_tuple(self, "object_labels", self.object_labels, field_name="FrameSample.object_labels")
        if not self.provenance_refs:
            raise MediaAssetError("FrameSample.provenance_refs must be non-empty")
        if not self.visual_grounding_ref and not self.object_labels:
            raise MediaAssetError("FrameSample requires visual_grounding_ref or object_labels")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"FrameSample(frame_id={self.frame_id!r}, timestamp_seconds={self.timestamp_seconds!r}, provenance_refs={self.provenance_refs!r})"


@dataclass(frozen=True, slots=True)
class SpeakerTurn:
    """Speaker attribution over a media time range."""

    turn_id: str
    speaker_id: str
    start_seconds: float
    end_seconds: float
    confidence: float
    provenance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.turn_id, "SpeakerTurn.turn_id")
        _require_non_empty(self.speaker_id, "SpeakerTurn.speaker_id")
        _require_time_range(self.start_seconds, self.end_seconds, "SpeakerTurn")
        _require_confidence(self.confidence, "SpeakerTurn.confidence")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="SpeakerTurn.provenance_refs")
        if not self.provenance_refs:
            raise MediaAssetError("SpeakerTurn.provenance_refs must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SpeakerTurn(turn_id={self.turn_id!r}, speaker_id={self.speaker_id!r}, start_seconds={self.start_seconds!r})"


@dataclass(frozen=True, slots=True)
class SceneMetadata:
    """Scene-level media metadata."""

    scene_id: str
    start_seconds: float
    end_seconds: float
    labels: tuple[str, ...]
    confidence: float
    provenance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.scene_id, "SceneMetadata.scene_id")
        _require_time_range(self.start_seconds, self.end_seconds, "SceneMetadata")
        _set_tuple(self, "labels", self.labels, field_name="SceneMetadata.labels")
        if not self.labels:
            raise MediaAssetError("SceneMetadata.labels must be non-empty")
        _require_confidence(self.confidence, "SceneMetadata.confidence")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="SceneMetadata.provenance_refs")
        if not self.provenance_refs:
            raise MediaAssetError("SceneMetadata.provenance_refs must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SceneMetadata(scene_id={self.scene_id!r}, start_seconds={self.start_seconds!r}, end_seconds={self.end_seconds!r})"


@dataclass(frozen=True, slots=True)
class MediaRedactionSpan:
    """Media redaction reference without claiming this layer enforced it."""

    redaction_id: str
    start_seconds: float
    end_seconds: float
    redaction_reason: str
    policy_ref: str
    provenance_refs: tuple[str, ...]
    status: MediaRedactionStatus = MediaRedactionStatus.PROPOSED

    def __post_init__(self) -> None:
        _require_non_empty(self.redaction_id, "MediaRedactionSpan.redaction_id")
        _require_time_range(self.start_seconds, self.end_seconds, "MediaRedactionSpan")
        _require_non_empty(self.redaction_reason, "MediaRedactionSpan.redaction_reason")
        _require_non_empty(self.policy_ref, "MediaRedactionSpan.policy_ref")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="MediaRedactionSpan.provenance_refs")
        if not self.provenance_refs:
            raise MediaAssetError("MediaRedactionSpan.provenance_refs must be non-empty")
        object.__setattr__(self, "status", MediaRedactionStatus(self.status))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MediaRedactionSpan(redaction_id={self.redaction_id!r}, start_seconds={self.start_seconds!r}, end_seconds={self.end_seconds!r})"


@dataclass(frozen=True, slots=True)
class MediaAnnotationHook:
    """Annotation hook for later review layers."""

    annotation_id: str
    target_ref: str
    label: str
    provenance_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.annotation_id, "MediaAnnotationHook.annotation_id")
        _require_non_empty(self.target_ref, "MediaAnnotationHook.target_ref")
        _require_non_empty(self.label, "MediaAnnotationHook.label")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="MediaAnnotationHook.provenance_refs")
        if not self.provenance_refs:
            raise MediaAssetError("MediaAnnotationHook.provenance_refs must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MediaAnnotationHook(annotation_id={self.annotation_id!r}, target_ref={self.target_ref!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """Complete structured media evidence payload."""

    asset_id: str
    asset_kind: MediaAssetKind
    source_card_id: str
    dataset_revision_id: str
    content_sha256: str
    provenance_refs: tuple[str, ...]
    segments: tuple[MediaSegment, ...] = ()
    transcript_alignments: tuple[TranscriptAlignment, ...] = ()
    frame_samples: tuple[FrameSample, ...] = ()
    speaker_turns: tuple[SpeakerTurn, ...] = ()
    scenes: tuple[SceneMetadata, ...] = ()
    redactions: tuple[MediaRedactionSpan, ...] = ()
    annotation_hooks: tuple[MediaAnnotationHook, ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.asset_id, "MediaAsset.asset_id")
        object.__setattr__(self, "asset_kind", MediaAssetKind(self.asset_kind))
        _require_non_empty(self.source_card_id, "MediaAsset.source_card_id")
        _require_non_empty(self.dataset_revision_id, "MediaAsset.dataset_revision_id")
        _require_sha256(self.content_sha256, "MediaAsset.content_sha256")
        _set_tuple(self, "provenance_refs", self.provenance_refs, field_name="MediaAsset.provenance_refs")
        _set_tuple(self, "segments", self.segments, expected=MediaSegment, field_name="MediaAsset.segments")
        _set_tuple(
            self,
            "transcript_alignments",
            self.transcript_alignments,
            expected=TranscriptAlignment,
            field_name="MediaAsset.transcript_alignments",
        )
        _set_tuple(
            self,
            "frame_samples",
            self.frame_samples,
            expected=FrameSample,
            field_name="MediaAsset.frame_samples",
        )
        _set_tuple(
            self,
            "speaker_turns",
            self.speaker_turns,
            expected=SpeakerTurn,
            field_name="MediaAsset.speaker_turns",
        )
        _set_tuple(self, "scenes", self.scenes, expected=SceneMetadata, field_name="MediaAsset.scenes")
        _set_tuple(
            self,
            "redactions",
            self.redactions,
            expected=MediaRedactionSpan,
            field_name="MediaAsset.redactions",
        )
        _set_tuple(
            self,
            "annotation_hooks",
            self.annotation_hooks,
            expected=MediaAnnotationHook,
            field_name="MediaAsset.annotation_hooks",
        )
        if not self.provenance_refs:
            raise MediaAssetError("MediaAsset.provenance_refs must be non-empty")
        if not (self.segments or self.frame_samples):
            raise MediaAssetError("MediaAsset requires at least one segment or frame sample")
        segment_ids = {segment.segment_id for segment in self.segments}
        for alignment in self.transcript_alignments:
            if alignment.segment_id not in segment_ids:
                raise MediaAssetError(f"TranscriptAlignment {alignment.alignment_id!r} references an unknown segment")
            segment = next(item for item in self.segments if item.segment_id == alignment.segment_id)
            if alignment.start_seconds < segment.start_seconds or alignment.end_seconds > segment.end_seconds:
                raise MediaAssetError("TranscriptAlignment time range must stay inside its segment")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MediaAsset(asset_id={self.asset_id!r}, asset_kind={self.asset_kind!r}, source_card_id={self.source_card_id!r})"


def build_media_asset(
    *,
    asset_id: str,
    asset_kind: MediaAssetKind | str,
    source_card_id: str,
    dataset_revision_id: str,
    content_sha256: str,
    provenance_refs: Iterable[str],
    segments: Iterable[MediaSegment] = (),
    transcript_alignments: Iterable[TranscriptAlignment] = (),
    frame_samples: Iterable[FrameSample] = (),
    speaker_turns: Iterable[SpeakerTurn] = (),
    scenes: Iterable[SceneMetadata] = (),
    redactions: Iterable[MediaRedactionSpan] = (),
    annotation_hooks: Iterable[MediaAnnotationHook] = (),
) -> MediaAsset:
    """Normalize iterables into an immutable media asset and validate cross-references."""
    return MediaAsset(
        asset_id=asset_id,
        asset_kind=MediaAssetKind(asset_kind),
        source_card_id=source_card_id,
        dataset_revision_id=dataset_revision_id,
        content_sha256=content_sha256,
        provenance_refs=tuple(provenance_refs),
        segments=tuple(segments),
        transcript_alignments=tuple(transcript_alignments),
        frame_samples=tuple(frame_samples),
        speaker_turns=tuple(speaker_turns),
        scenes=tuple(scenes),
        redactions=tuple(redactions),
        annotation_hooks=tuple(annotation_hooks),
    )


def _require_non_empty(value: str | None, field_name: str) -> None:
    if value is None or not str(value).strip():
        raise MediaAssetError(f"{field_name} must be non-empty")


def _require_time_range(start_seconds: float, end_seconds: float, field_name: str) -> None:
    if start_seconds < 0:
        raise MediaAssetError(f"{field_name}.start_seconds must be non-negative")
    if end_seconds <= start_seconds:
        raise MediaAssetError(f"{field_name}.end_seconds must be greater than start_seconds")


def _require_confidence(value: float, field_name: str) -> None:
    if value < 0.0 or value > 1.0:
        raise MediaAssetError(f"{field_name} must be between 0.0 and 1.0")


def _require_sha256(value: str, field_name: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise MediaAssetError(f"{field_name} must be a 64-character sha256 hex digest")


def _set_tuple(
    instance: object,
    attr: str,
    values: Iterable[object],
    *,
    expected: type[object] | None = None,
    field_name: str,
) -> None:
    normalized = tuple(values)
    if expected is not None and any(not isinstance(item, expected) for item in normalized):
        raise MediaAssetError(f"{field_name} must contain {expected.__name__} instances")
    for item in normalized:
        if isinstance(item, str):
            _require_non_empty(item, f"{field_name}[]")
    object.__setattr__(instance, attr, normalized)


__all__ = [
    "FrameSample",
    "MediaAnnotationHook",
    "MediaAsset",
    "MediaAssetError",
    "MediaAssetKind",
    "MediaRedactionSpan",
    "MediaRedactionStatus",
    "MediaSegment",
    "SceneMetadata",
    "SpeakerTurn",
    "TranscriptAlignment",
    "build_media_asset",
]
