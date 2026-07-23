"""Fail-closed structured explanations for visible Workbench decisions.

Why panels are presentation objects derived from typed decision records. They
never infer a successful decision from free-form agent prose; missing evidence,
authority, provenance, confidence, or blocking policy gates degrade the panel
and expose next actions instead of rendering success.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

logger = logging.getLogger(__name__)


_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_TRAVERSAL_MARKERS = ("/", "\\", "..", "\x00")


class DecisionKind(str, Enum):
    """Decision categories that can be explained to Workbench operators."""

    MODEL = "model"
    SOURCE = "source"
    TOOL = "tool"
    POLICY = "policy"
    COST = "cost"
    ROUTE = "route"
    AUTOMATION = "automation"
    APPROVAL = "approval"
    USER_QUESTION = "user_question"


class WhyPanelStatus(str, Enum):
    """Trust state rendered by the why-panel UI."""

    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class PolicyGateState(str, Enum):
    """Policy gate outcomes considered before a decision can be trusted."""

    ALLOW = "pass"
    WARN = "warn"
    BLOCK = "block"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class WhyEvidenceRef:
    """One cited evidence item behind a visible decision."""

    evidence_id: str
    kind: str
    title: str
    uri: str
    captured_at_utc: str
    provenance: tuple[tuple[str, str], ...]
    supports: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_identifier(self.evidence_id, "evidence_id")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WhyEvidenceRef(evidence_id={self.evidence_id!r}, kind={self.kind!r}, title={self.title!r})"


@dataclass(frozen=True, slots=True)
class WhyAuthorityRef:
    """The authority that allowed, denied, or constrained the decision."""

    authority_id: str
    source: str
    rule: str
    decision: str
    provenance: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _validate_identifier(self.authority_id, "authority_id")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WhyAuthorityRef(authority_id={self.authority_id!r}, source={self.source!r}, rule={self.rule!r})"


@dataclass(frozen=True, slots=True)
class PreferenceEffect:
    """User preference influence disclosed in the explanation."""

    preference_id: str
    effect: str
    applied: bool
    reason: str

    def __post_init__(self) -> None:
        _validate_identifier(self.preference_id, "preference_id")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"PreferenceEffect(preference_id={self.preference_id!r}, effect={self.effect!r}, applied={self.applied!r})"
        )


@dataclass(frozen=True, slots=True)
class WhyDecisionRecord:
    """Structured input record for one why-panel explanation."""

    decision_id: str
    kind: DecisionKind | str
    subject: str
    chosen_option: str
    alternatives: tuple[str, ...]
    confidence: float | int | None
    evidence: tuple[WhyEvidenceRef, ...]
    authority: tuple[WhyAuthorityRef, ...]
    policy_gates: tuple[tuple[str, PolicyGateState | str], ...] = ()
    preference_effects: tuple[PreferenceEffect, ...] = ()
    stale_after_seconds: int = 86_400
    next_actions: tuple[str, ...] = ()
    agent_summary: str = ""

    def __post_init__(self) -> None:
        _validate_identifier(self.decision_id, "decision_id")
        try:
            DecisionKind(self.kind)
        except ValueError as exc:
            raise ValueError(f"unknown decision kind: {self.kind!r}") from exc
        if not str(self.subject).strip():
            raise ValueError("subject must be non-empty")
        if not str(self.chosen_option).strip():
            raise ValueError("chosen_option must be non-empty")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WhyDecisionRecord(decision_id={self.decision_id!r}, kind={self.kind!r}, subject={self.subject!r})"


@dataclass(frozen=True, slots=True)
class WhyPanel:
    """Rendered explanation state safe for UI and API consumers."""

    decision_id: str
    kind: DecisionKind
    subject: str
    chosen_option: str
    status: WhyPanelStatus
    trusted: bool
    confidence: float | None
    summary: str
    reasons: tuple[str, ...]
    missing: tuple[str, ...]
    blockers: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    authority_refs: tuple[str, ...]
    stale_facts: tuple[str, ...]
    preference_effects: tuple[PreferenceEffect, ...]
    policy_gates: tuple[tuple[str, PolicyGateState], ...]
    next_actions: tuple[str, ...]

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"WhyPanel(decision_id={self.decision_id!r}, kind={self.kind!r}, subject={self.subject!r})"


class WhyPanelBuilder:
    """Build fail-closed why panels from typed decision records."""

    def __init__(self, *, now_utc: datetime | None = None) -> None:
        current = now_utc if now_utc is not None else datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        self.now_utc = current.astimezone(timezone.utc)

    def build(self, record: WhyDecisionRecord) -> WhyPanel:
        """Execute the build operation.

        Returns:
            WhyPanel value produced by build().
        """
        kind = DecisionKind(record.kind)
        missing: list[str] = []
        blockers: list[str] = []
        reasons: list[str] = []
        stale_facts: list[str] = []

        confidence = _coerce_confidence(record.confidence, missing)
        evidence_refs = self._evaluate_evidence(record, missing, blockers, stale_facts)
        authority_refs = self._evaluate_authority(record, missing, blockers)
        policy_gates = self._evaluate_policy_gates(record, blockers)

        if confidence is not None:
            reasons.append(f"confidence {confidence:.2f} supplied by decision record")
        if evidence_refs:
            reasons.append(f"{len(evidence_refs)} evidence item(s) support the decision")
        if authority_refs:
            reasons.append(f"{len(authority_refs)} authority record(s) constrain the decision")
        if record.preference_effects:
            reasons.append("user preference effects were disclosed")
        if stale_facts:
            blockers.extend(f"stale_provenance:{item}" for item in stale_facts)

        trusted = not missing and not blockers
        stale_only = (
            bool(stale_facts) and not missing and all(blocker.startswith("stale_provenance:") for blocker in blockers)
        )
        if trusted:
            status = WhyPanelStatus.READY
        elif stale_only:
            status = WhyPanelStatus.DEGRADED
        else:
            status = WhyPanelStatus.BLOCKED

        next_actions = tuple(record.next_actions) or _default_next_actions(missing, blockers)
        summary = _structured_summary(record, kind, trusted)
        return WhyPanel(
            decision_id=record.decision_id,
            kind=kind,
            subject=record.subject,
            chosen_option=record.chosen_option,
            status=status,
            trusted=trusted,
            confidence=confidence,
            summary=summary,
            reasons=tuple(reasons),
            missing=tuple(dict.fromkeys(missing)),
            blockers=tuple(dict.fromkeys(blockers)),
            evidence_refs=tuple(evidence_refs),
            authority_refs=tuple(authority_refs),
            stale_facts=tuple(stale_facts),
            preference_effects=record.preference_effects,
            policy_gates=policy_gates,
            next_actions=next_actions,
        )

    def _evaluate_evidence(
        self,
        record: WhyDecisionRecord,
        missing: list[str],
        blockers: list[str],
        stale_facts: list[str],
    ) -> tuple[str, ...]:
        if not record.evidence:
            missing.append("evidence")
            return ()
        accepted: list[str] = []
        max_age = timedelta(seconds=record.stale_after_seconds)
        for item in record.evidence:
            if not item.provenance:
                blockers.append(f"missing_provenance:{item.evidence_id}")
            captured = _parse_utc(item.captured_at_utc)
            if captured is None:
                blockers.append(f"unreadable_provenance_time:{item.evidence_id}")
            elif captured < self.now_utc - max_age or captured > self.now_utc + timedelta(minutes=5):
                stale_facts.append(item.evidence_id)
            if not item.supports:
                blockers.append(f"missing_support_claim:{item.evidence_id}")
            accepted.append(item.evidence_id)
        return tuple(accepted)

    @staticmethod
    def _evaluate_authority(
        record: WhyDecisionRecord,
        missing: list[str],
        blockers: list[str],
    ) -> tuple[str, ...]:
        if not record.authority:
            missing.append("authority")
            return ()
        accepted: list[str] = []
        for item in record.authority:
            if not item.provenance:
                blockers.append(f"missing_authority_provenance:{item.authority_id}")
            if not item.rule.strip() or not item.decision.strip():
                blockers.append(f"incomplete_authority:{item.authority_id}")
            accepted.append(item.authority_id)
        return tuple(accepted)

    @staticmethod
    def _evaluate_policy_gates(
        record: WhyDecisionRecord,
        blockers: list[str],
    ) -> tuple[tuple[str, PolicyGateState], ...]:
        gates: list[tuple[str, PolicyGateState]] = []
        for gate_name, state_value in record.policy_gates:
            _validate_identifier(gate_name, "policy_gate")
            try:
                state = PolicyGateState(state_value)
            except ValueError:
                state = PolicyGateState.UNKNOWN
            if state is PolicyGateState.BLOCK:
                blockers.append(f"policy_gate_blocked:{gate_name}")
            elif state is PolicyGateState.UNKNOWN:
                blockers.append(f"policy_gate_unknown:{gate_name}")
            gates.append((gate_name, state))
        return tuple(gates)


def build_why_panel(record: WhyDecisionRecord, *, now_utc: datetime | None = None) -> WhyPanel:
    """Build one fail-closed why panel from a structured record."""
    return WhyPanelBuilder(now_utc=now_utc).build(record)


def _coerce_confidence(value: float | int | None, missing: list[str]) -> float | None:
    if value is None:
        missing.append("confidence")
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        missing.append("confidence")
        return None
    if confidence < 0.0 or confidence > 1.0:
        missing.append("confidence")
        return None
    return confidence


def _structured_summary(record: WhyDecisionRecord, kind: DecisionKind, trusted: bool) -> str:
    state = "selected" if trusted else "blocked"
    return f"{kind.value} decision {state}: {record.chosen_option} for {record.subject}"


def _default_next_actions(missing: list[str], blockers: list[str]) -> tuple[str, ...]:
    actions: list[str] = []
    if "evidence" in missing:
        actions.append("Attach at least one cited evidence record.")
    if "authority" in missing:
        actions.append("Attach the governing authority or approval record.")
    if "confidence" in missing:
        actions.append("Record calibrated confidence before presenting this as trusted.")
    if any(blocker.startswith("missing_provenance") for blocker in blockers):
        actions.append("Refresh evidence with provenance before use.")
    if any(blocker.startswith("policy_gate_") for blocker in blockers):
        actions.append("Resolve policy gate blockers before execution.")
    return tuple(actions or ("Review missing structured decision fields.",))


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be non-empty")
    if any(marker in value for marker in _TRAVERSAL_MARKERS):
        raise ValueError(f"{field_name} contains forbidden traversal marker")
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} contains unsupported characters")


__all__ = [
    "DecisionKind",
    "PolicyGateState",
    "PreferenceEffect",
    "WhyAuthorityRef",
    "WhyDecisionRecord",
    "WhyEvidenceRef",
    "WhyPanel",
    "WhyPanelBuilder",
    "WhyPanelStatus",
    "build_why_panel",
]
