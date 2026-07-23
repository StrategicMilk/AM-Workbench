"""Step 1 of Foreman's intake phase: raw prompt is resolved into a structured RequestFrame before any planning begins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from vetinari.security.redaction import redact_text
from vetinari.utils import wrap_privacy_envelope


@dataclass(frozen=True, slots=True)
class RequestFrame:
    """Structured intake contract passed to Foreman before planning."""

    goal: str = ""  # User-facing outcome that Foreman should plan toward.
    persona_name: str | None = None  # Optional persona selected upstream by the user or UI.
    preferred_worker_mode: str | None = None  # Worker mode hint resolved from the intake tree.
    preferred_model_tier: str | None = None  # Model tier hint resolved against the model catalog.
    urgency: Literal["low", "medium", "high"] = "medium"  # Planning urgency for routing and budget choices.
    scope_hint: str | None = None  # Concise boundary hint that narrows expected work.
    destructive_intent: bool = False  # Whether the prompt signals destructive or irreversible work.
    budget_tokens: int | None = None  # Optional token budget hint for downstream workers.
    raw_prompt: str = ""  # Original prompt preserved verbatim for audit and review.
    privacy_class: Literal["public", "operational", "subject_data", "secret"] = "operational"
    privacy_subject_id: str | None = None
    privacy_retention_days: int = 30

    def __post_init__(self) -> None:
        if self.raw_prompt:
            wrap_privacy_envelope(
                {"raw_prompt_present": True},
                privacy_class=self.privacy_class,
                subject_id=self.privacy_subject_id,
                retention_days=self.privacy_retention_days,
                source="request_frame.raw_prompt",
                redaction_applied=True,
            )

    def __repr__(self) -> str:
        return (
            "RequestFrame("
            f"goal={self._repr_goal()!r}, "
            f"persona_name={redact_text(str(self.persona_name)) if self.persona_name is not None else None!r}, "
            f"preferred_worker_mode={self.preferred_worker_mode!r}, "
            f"destructive_intent={self.destructive_intent!r})"
        )

    def _repr_goal(self) -> str:
        if self.raw_prompt and self.goal == self.raw_prompt.strip():
            return "<raw_prompt_goal>"
        return redact_text(self.goal)


__all__ = ["RequestFrame"]
