"""Canonical episodic-memory records and feedback metadata."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.security.fail_closed import assert_closed_schema, sanitize_untrusted_text


class EpisodeFeedbackLabel(str, Enum):
    """Deterministic candidate feedback labels for stored episodes."""

    HELPFUL = "helpful"
    HARMFUL = "harmful"


def _sanitize_feedback_text(value: object, default: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        value = default
    return sanitize_untrusted_text(value, max_length=max_length)


@dataclass(frozen=True, slots=True)
class EpisodeFeedback:
    """Candidate helpful/harmful metadata attached to an episode."""

    label: EpisodeFeedbackLabel
    source: str
    task_id: str = ""
    model_id: str = ""
    task_type: str = ""
    inspector_score: float | None = None
    user_feedback: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)
    candidate_only: bool = True
    requires_memory_firewall: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Serialize feedback into stable JSON metadata."""
        return {
            "label": self.label.value,
            "source": self.source,
            "task_id": self.task_id,
            "model_id": self.model_id,
            "task_type": self.task_type,
            "inspector_score": self.inspector_score,
            "user_feedback": self.user_feedback,
            "timestamp": self.timestamp,
            "metadata": dict(self.metadata),
            "candidate_only": self.candidate_only,
            "requires_memory_firewall": self.requires_memory_firewall,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> EpisodeFeedback:
        """Build feedback from stored JSON metadata.

        Returns:
            A validated feedback record.
        """
        assert_closed_schema(
            value,
            allowed_keys={
                "label",
                "source",
                "task_id",
                "model_id",
                "task_type",
                "inspector_score",
                "user_feedback",
                "timestamp",
                "metadata",
                "candidate_only",
                "requires_memory_firewall",
            },
            required_keys={"label"},
        )
        return cls(
            label=EpisodeFeedbackLabel(str(value["label"])),
            source=_sanitize_feedback_text(value.get("source"), "unknown", max_length=256),
            task_id=_sanitize_feedback_text(value.get("task_id"), "unknown", max_length=256),
            model_id=_sanitize_feedback_text(value.get("model_id"), "unknown", max_length=512),
            task_type=_sanitize_feedback_text(value.get("task_type"), "unknown", max_length=256),
            inspector_score=value.get("inspector_score"),
            user_feedback=(
                sanitize_untrusted_text(value["user_feedback"], max_length=20_000)
                if value.get("user_feedback") is not None
                else None
            ),
            timestamp=sanitize_untrusted_text(
                value.get("timestamp", datetime.now(timezone.utc).isoformat()),
                max_length=128,
            ),
            metadata=dict(value.get("metadata") or {}),
            candidate_only=bool(value.get("candidate_only", True)),
            requires_memory_firewall=bool(value.get("requires_memory_firewall", True)),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"EpisodeFeedback(label={self.label!r}, source={self.source!r}, task_id={self.task_id!r})"


@dataclass(frozen=True, slots=True)
class CanonicalEpisode:
    """Canonical episode shape shared by legacy and unified memory callers."""

    episode_id: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    task_summary: str = ""
    agent_type: str = ""
    task_type: str = ""
    output_summary: str = ""
    quality_score: float = 0.0
    success: bool = False
    model_id: str = ""
    importance: float = 0.5
    provenance: str = "canonical_episodic_memory"
    metadata: dict[str, Any] = field(default_factory=dict)
    feedback: tuple[EpisodeFeedback, ...] = ()
    candidate_status: str = "candidate"
    requires_memory_firewall: bool = True

    def metadata_for_storage(self) -> dict[str, Any]:
        """Return metadata with candidate-only governance fields included.

        Returns:
            dict[str, Any] value produced by metadata_for_storage().
        """
        metadata = dict(self.metadata)
        metadata.setdefault("provenance", self.provenance)
        metadata["candidate_status"] = self.candidate_status
        metadata["candidate_only"] = True
        metadata["requires_memory_firewall"] = self.requires_memory_firewall
        if self.feedback:
            metadata["episode_feedback"] = [entry.to_dict() for entry in self.feedback]
        return metadata

    @classmethod
    def from_recorded(cls, episode: Any) -> CanonicalEpisode:
        """Adapt a RecordedEpisode-like object to the canonical shape.

        Returns:
            CanonicalEpisode value produced by from_recorded().
        """
        metadata = dict(getattr(episode, "metadata", {}) or {})
        feedback_values = metadata.get("episode_feedback") or []
        feedback = tuple(
            EpisodeFeedback.from_dict(value)
            for value in feedback_values
            if isinstance(value, dict) and value.get("label") in {label.value for label in EpisodeFeedbackLabel}
        )
        return cls(
            episode_id=str(getattr(episode, "episode_id", "")),
            timestamp=str(getattr(episode, "timestamp", "")),
            task_summary=str(getattr(episode, "task_summary", "")),
            agent_type=str(getattr(episode, "agent_type", "")),
            task_type=str(getattr(episode, "task_type", "")),
            output_summary=str(getattr(episode, "output_summary", "")),
            quality_score=float(getattr(episode, "quality_score", 0.0)),
            success=bool(getattr(episode, "success", False)),
            model_id=str(getattr(episode, "model_id", "")),
            importance=float(getattr(episode, "importance", metadata.get("importance", 0.5))),
            provenance=str(metadata.get("provenance", "memory_episodes")),
            metadata=metadata,
            feedback=feedback,
            candidate_status=str(metadata.get("candidate_status", "candidate")),
            requires_memory_firewall=bool(metadata.get("requires_memory_firewall", True)),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CanonicalEpisode(episode_id={self.episode_id!r}, timestamp={self.timestamp!r}, task_summary={self.task_summary!r})"
