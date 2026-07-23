"""Consent-scoped preference cards with deterministic decay.

This module is intentionally storage-neutral. It provides the contract that
spine/API/UI layers can call without creating hidden active behavior changes:
observations become proposed cards, and only explicitly consented active cards
can influence prompts, routes, automations, UI defaults, or review gates.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from math import pow

from vetinari.workbench.spine import WorkbenchProjectIdRejected, validate_project_id


class PreferenceCardError(ValueError):
    """Raised when a preference card cannot be trusted."""


class PreferenceKind(str, Enum):
    """Preference categories represented by card records."""

    USER_PREFERENCE = "user_preference"
    TRUST_BOUNDARY = "trust_boundary"
    DELEGATION_CHOICE = "delegation_choice"
    EXPERTISE_LEVEL = "expertise_level"
    PRIVACY_BOUNDARY = "privacy_boundary"
    FRUSTRATION_PATTERN = "frustration_pattern"


class PreferenceStatus(str, Enum):
    """Lifecycle states for a preference card."""

    PROPOSED = "proposed"
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"
    REJECTED = "rejected"


class PreferenceScopeType(str, Enum):
    """Where a preference may be applied."""

    GLOBAL = "global"
    PROJECT = "project"
    WORKFLOW = "workflow"
    ROUTE = "route"
    AUTOMATION = "automation"
    UI = "ui"


class PreferenceEvidenceKind(str, Enum):
    """Evidence kinds accepted by preference cards."""

    USER_STATEMENT = "user_statement"
    OBSERVED_BEHAVIOR = "observed_behavior"
    CORRECTION = "correction"
    EXPLICIT_CONSENT = "explicit_consent"
    REVOCATION = "revocation"


class DownstreamEffect(str, Enum):
    """Downstream systems a consented card may influence."""

    PROMPT = "prompt"
    ROUTE = "route"
    AUTOMATION = "automation"
    UI_DEFAULT = "ui_default"
    REVIEW_GATE = "review_gate"


@dataclass(frozen=True, slots=True)
class PreferenceEvidence:
    """Evidence attached to one preference card."""

    evidence_id: str
    kind: PreferenceEvidenceKind
    summary: str
    observed_at_utc: str
    source: str
    authority: str

    def __post_init__(self) -> None:
        _require_non_empty(self.evidence_id, "evidence_id")
        _require_non_empty(self.summary, "summary")
        _require_non_empty(self.source, "source")
        _require_non_empty(self.authority, "authority")
        if not isinstance(self.kind, PreferenceEvidenceKind):
            raise PreferenceCardError("evidence kind must be a PreferenceEvidenceKind")
        _parse_utc(self.observed_at_utc, "observed_at_utc")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PreferenceEvidence(evidence_id={self.evidence_id!r}, kind={self.kind!r}, summary={self.summary!r})"


@dataclass(frozen=True, slots=True)
class PreferenceScope:
    """Bounded application scope for a preference card."""

    scope_type: PreferenceScopeType
    project_id: str | None = None
    workflow_id: str | None = None
    route_id: str | None = None
    automation_id: str | None = None
    ui_surface: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope_type, PreferenceScopeType):
            raise PreferenceCardError("scope_type must be a PreferenceScopeType")
        if self.project_id is not None:
            _canonical_project_id(self.project_id)
        required_by_scope = {
            PreferenceScopeType.PROJECT: self.project_id,
            PreferenceScopeType.WORKFLOW: self.workflow_id,
            PreferenceScopeType.ROUTE: self.route_id,
            PreferenceScopeType.AUTOMATION: self.automation_id,
            PreferenceScopeType.UI: self.ui_surface,
        }
        required_value = required_by_scope.get(self.scope_type)
        if self.scope_type is not PreferenceScopeType.GLOBAL and not required_value:
            raise PreferenceCardError(f"{self.scope_type.value} scope requires its matching identifier")

    def matches(self, context: Mapping[str, str | None]) -> bool:
        """Return whether a runtime context is inside this scope.

        Returns:
            bool value produced by matches().
        """
        if self.scope_type is PreferenceScopeType.GLOBAL:
            return True
        if self.scope_type is PreferenceScopeType.PROJECT:
            return context.get("project_id") == self.project_id
        if self.scope_type is PreferenceScopeType.WORKFLOW:
            return context.get("workflow_id") == self.workflow_id
        if self.scope_type is PreferenceScopeType.ROUTE:
            return context.get("route_id") == self.route_id
        if self.scope_type is PreferenceScopeType.AUTOMATION:
            return context.get("automation_id") == self.automation_id
        if self.scope_type is PreferenceScopeType.UI:
            return context.get("ui_surface") == self.ui_surface
        return False

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PreferenceScope(scope_type={self.scope_type!r}, project_id={self.project_id!r}, workflow_id={self.workflow_id!r})"


@dataclass(frozen=True, slots=True)
class PreferenceDecayPolicy:
    """Confidence decay policy for a preference card."""

    max_age_days: int
    half_life_days: int
    min_confidence: float

    def __post_init__(self) -> None:
        if self.max_age_days <= 0:
            raise PreferenceCardError("max_age_days must be positive")
        if self.half_life_days <= 0:
            raise PreferenceCardError("half_life_days must be positive")
        if not 0 <= self.min_confidence <= 1:
            raise PreferenceCardError("min_confidence must be between 0 and 1")

    def effective_confidence(
        self,
        base_confidence: float,
        *,
        last_confirmed_at_utc: str,
        now_utc: datetime,
    ) -> float:
        """Return decayed confidence for a card at ``now_utc``.

        Returns:
            float value produced by effective_confidence().
        """
        confirmed = _parse_utc(last_confirmed_at_utc, "last_confirmed_at_utc")
        age_seconds = (now_utc - confirmed).total_seconds()
        if age_seconds < 0:
            return 0.0
        age_days = age_seconds / 86400
        if age_days > self.max_age_days:
            return 0.0
        return base_confidence * pow(0.5, age_days / self.half_life_days)


@dataclass(frozen=True, slots=True)
class PreferenceCard:
    """Transparent preference card with consent, scope, evidence, and decay."""

    card_id: str
    kind: PreferenceKind
    label: str
    statement: str
    status: PreferenceStatus
    scope: PreferenceScope
    confidence: float
    evidence: tuple[PreferenceEvidence, ...]
    decay_policy: PreferenceDecayPolicy
    downstream_effects: tuple[DownstreamEffect, ...]
    created_at_utc: str
    last_confirmed_at_utc: str | None
    consent_granted: bool
    consent_granted_at_utc: str | None
    revoke_path: str
    revoked_at_utc: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.card_id, "card_id")
        _require_non_empty(self.label, "label")
        _require_non_empty(self.statement, "statement")
        _require_non_empty(self.revoke_path, "revoke_path")
        if not isinstance(self.kind, PreferenceKind):
            raise PreferenceCardError("kind must be a PreferenceKind")
        if not isinstance(self.status, PreferenceStatus):
            raise PreferenceCardError("status must be a PreferenceStatus")
        if not isinstance(self.scope, PreferenceScope):
            raise PreferenceCardError("scope must be a PreferenceScope")
        if not isinstance(self.decay_policy, PreferenceDecayPolicy):
            raise PreferenceCardError("decay_policy must be a PreferenceDecayPolicy")
        if not 0 <= self.confidence <= 1:
            raise PreferenceCardError("confidence must be between 0 and 1")
        if not self.evidence:
            raise PreferenceCardError("preference cards require source evidence")
        if not self.downstream_effects:
            raise PreferenceCardError("preference cards require downstream_effects")
        if any(not isinstance(effect, DownstreamEffect) for effect in self.downstream_effects):
            raise PreferenceCardError("downstream_effects must be DownstreamEffect values")
        _parse_utc(self.created_at_utc, "created_at_utc")
        if self.last_confirmed_at_utc is not None:
            _parse_utc(self.last_confirmed_at_utc, "last_confirmed_at_utc")
        if self.consent_granted_at_utc is not None:
            _parse_utc(self.consent_granted_at_utc, "consent_granted_at_utc")
        if self.revoked_at_utc is not None:
            _parse_utc(self.revoked_at_utc, "revoked_at_utc")
        if self.status is PreferenceStatus.ACTIVE:
            if not self.consent_granted or self.consent_granted_at_utc is None:
                raise PreferenceCardError("active preference cards require explicit consent")
            if self.last_confirmed_at_utc is None:
                raise PreferenceCardError("active preference cards require last_confirmed_at_utc")
            if not any(item.kind is PreferenceEvidenceKind.EXPLICIT_CONSENT for item in self.evidence):
                raise PreferenceCardError("active preference cards require explicit consent evidence")
        if self.status is PreferenceStatus.REVOKED and self.revoked_at_utc is None:
            raise PreferenceCardError("revoked preference cards require revoked_at_utc")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PreferenceCard(card_id={self.card_id!r}, kind={self.kind!r}, label={self.label!r})"


@dataclass(frozen=True, slots=True)
class PreferenceCardDecision:
    """Fail-closed decision for a preference-card downstream effect."""

    passed: bool
    card_id: str
    effective_status: PreferenceStatus
    effective_confidence: float
    permitted_effects: tuple[DownstreamEffect, ...]
    rejection_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.passed:
            if not self.permitted_effects:
                raise PreferenceCardError("passed decisions require permitted_effects")
            if self.rejection_reasons:
                raise PreferenceCardError("passed decisions cannot include rejection_reasons")
        elif not self.rejection_reasons:
            raise PreferenceCardError("failed decisions require rejection_reasons")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PreferenceCardDecision(passed={self.passed!r}, card_id={self.card_id!r}, effective_status={self.effective_status!r})"


class PreferenceCardService:
    """Small in-memory service for deterministic proposal/consent/revocation flows."""

    def __init__(self, cards: tuple[PreferenceCard, ...] = ()) -> None:
        self._cards = {card.card_id: card for card in cards}

    def list_cards(self) -> tuple[PreferenceCard, ...]:
        """Return cards sorted by newest first."""
        return tuple(sorted(self._cards.values(), key=lambda card: card.created_at_utc, reverse=True))

    def propose_card(
        self,
        *,
        card_id: str,
        kind: PreferenceKind,
        label: str,
        statement: str,
        scope: PreferenceScope,
        confidence: float,
        evidence: tuple[PreferenceEvidence, ...],
        decay_policy: PreferenceDecayPolicy,
        downstream_effects: tuple[DownstreamEffect, ...],
        created_at_utc: str,
        revoke_path: str,
    ) -> PreferenceCard:
        """Create a proposed card; observations never activate preferences.

        Returns:
            PreferenceCard value produced by propose_card().
        """
        card = PreferenceCard(
            card_id=card_id,
            kind=kind,
            label=label,
            statement=statement,
            status=PreferenceStatus.PROPOSED,
            scope=scope,
            confidence=confidence,
            evidence=evidence,
            decay_policy=decay_policy,
            downstream_effects=downstream_effects,
            created_at_utc=created_at_utc,
            last_confirmed_at_utc=None,
            consent_granted=False,
            consent_granted_at_utc=None,
            revoke_path=revoke_path,
        )
        self._cards[card.card_id] = card
        return card

    def activate_card(
        self,
        card_id: str,
        *,
        consent_evidence: PreferenceEvidence,
        consent_granted_at_utc: str,
    ) -> PreferenceCard:
        """Activate a proposed card only with explicit consent evidence.

        Returns:
            PreferenceCard value produced by activate_card().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        existing = self._cards[card_id]
        if consent_evidence.kind is not PreferenceEvidenceKind.EXPLICIT_CONSENT:
            raise PreferenceCardError("activation requires explicit consent evidence")
        card = replace(
            existing,
            status=PreferenceStatus.ACTIVE,
            evidence=(*existing.evidence, consent_evidence),
            last_confirmed_at_utc=consent_granted_at_utc,
            consent_granted=True,
            consent_granted_at_utc=consent_granted_at_utc,
        )
        self._cards[card.card_id] = card
        return card

    def revoke_card(
        self,
        card_id: str,
        *,
        revocation_evidence: PreferenceEvidence,
        revoked_at_utc: str,
    ) -> PreferenceCard:
        """Revoke a card so future evaluations fail closed.

        Returns:
            PreferenceCard value produced by revoke_card().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        existing = self._cards[card_id]
        if revocation_evidence.kind is not PreferenceEvidenceKind.REVOCATION:
            raise PreferenceCardError("revocation requires revocation evidence")
        card = replace(
            existing,
            status=PreferenceStatus.REVOKED,
            evidence=(*existing.evidence, revocation_evidence),
            consent_granted=False,
            revoked_at_utc=revoked_at_utc,
        )
        self._cards[card.card_id] = card
        return card

    def evaluate(
        self,
        card_id: str,
        *,
        context: Mapping[str, str | None],
        requested_effect: DownstreamEffect,
        now_utc: datetime | None = None,
    ) -> PreferenceCardDecision:
        """Evaluate one stored card for a downstream effect.

        Returns:
            PreferenceCardDecision value produced by evaluate().
        """
        card = self._cards.get(card_id)
        if card is None:
            return PreferenceCardDecision(
                passed=False,
                card_id=card_id,
                effective_status=PreferenceStatus.REJECTED,
                effective_confidence=0.0,
                permitted_effects=(),
                rejection_reasons=("preference card not found",),
            )
        return evaluate_preference_card(
            card,
            context=context,
            requested_effect=requested_effect,
            now_utc=now_utc,
        )


def evaluate_preference_card(
    card: PreferenceCard,
    *,
    context: Mapping[str, str | None],
    requested_effect: DownstreamEffect,
    now_utc: datetime | None = None,
) -> PreferenceCardDecision:
    """Return whether a card may affect the requested downstream system.

    Returns:
        PreferenceCardDecision value produced by evaluate_preference_card().
    """
    now = _normalize_now(now_utc)
    reasons: list[str] = []
    effective_status = card.status
    effective_confidence = 0.0

    if card.status is not PreferenceStatus.ACTIVE:
        reasons.append(f"preference card is not active: {card.status.value}")
    if not card.consent_granted or card.consent_granted_at_utc is None:
        reasons.append("explicit consent is unavailable")
    if not card.evidence:
        reasons.append("source evidence is unavailable")
    if requested_effect not in card.downstream_effects:
        reasons.append(f"downstream effect {requested_effect.value!r} is not permitted")
    if not card.scope.matches(context):
        reasons.append("runtime context is outside the preference card scope")

    if card.last_confirmed_at_utc is None:
        reasons.append("last confirmation timestamp is unavailable")
    else:
        try:
            effective_confidence = card.decay_policy.effective_confidence(
                card.confidence,
                last_confirmed_at_utc=card.last_confirmed_at_utc,
                now_utc=now,
            )
        except ValueError:
            reasons.append("last confirmation timestamp is malformed")
        if effective_confidence < card.decay_policy.min_confidence:
            reasons.append("preference confidence decayed below the minimum threshold")
            effective_status = PreferenceStatus.EXPIRED

    if reasons:
        return PreferenceCardDecision(
            passed=False,
            card_id=card.card_id,
            effective_status=effective_status,
            effective_confidence=effective_confidence,
            permitted_effects=(),
            rejection_reasons=tuple(dict.fromkeys(reasons)),
        )
    return PreferenceCardDecision(
        passed=True,
        card_id=card.card_id,
        effective_status=effective_status,
        effective_confidence=effective_confidence,
        permitted_effects=(requested_effect,),
        rejection_reasons=(),
    )


def _canonical_project_id(project_id: str) -> str:
    try:
        return validate_project_id(project_id)
    except WorkbenchProjectIdRejected as exc:
        raise PreferenceCardError(str(exc)) from exc


def _normalize_now(value: datetime | None) -> datetime:
    now = value or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise PreferenceCardError("now_utc must be timezone-aware UTC")
    return now.astimezone(timezone.utc)


def _parse_utc(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreferenceCardError(f"{field_name} must be ISO8601 UTC") from exc
    if parsed.tzinfo is None:
        raise PreferenceCardError(f"{field_name} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise PreferenceCardError(f"{field_name} must be non-empty")
