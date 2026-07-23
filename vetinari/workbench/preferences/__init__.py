"""Governed Workbench preference cards.

Preference cards are transparent, editable records of user preferences and
boundaries. Observed behavior may create proposed cards, but active downstream
effects require explicit consent and fail closed when scope, evidence, or
decay state is unavailable.
"""

from __future__ import annotations

from vetinari.workbench.preferences.cards import (
    DownstreamEffect,
    PreferenceCard,
    PreferenceCardDecision,
    PreferenceCardError,
    PreferenceCardService,
    PreferenceDecayPolicy,
    PreferenceEvidence,
    PreferenceEvidenceKind,
    PreferenceKind,
    PreferenceScope,
    PreferenceScopeType,
    PreferenceStatus,
    evaluate_preference_card,
)

__all__ = [
    "DownstreamEffect",
    "PreferenceCard",
    "PreferenceCardDecision",
    "PreferenceCardError",
    "PreferenceCardService",
    "PreferenceDecayPolicy",
    "PreferenceEvidence",
    "PreferenceEvidenceKind",
    "PreferenceKind",
    "PreferenceScope",
    "PreferenceScopeType",
    "PreferenceStatus",
    "evaluate_preference_card",
]
