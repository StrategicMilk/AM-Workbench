"""Immutable contracts for Workbench governance modes."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1.0"


class GovernanceMode(str, Enum):
    """Governance mode vocabulary for current and retrospective policy views."""

    OBSERVE = "observe"
    WARN = "warn"
    STRICT = "strict"
    RETROSPECTIVE_SCAN = "retrospective_scan"


class GovernanceEnforcementEffect(str, Enum):
    """Effect vocabulary separated from upstream verdict values."""

    ALLOW = "allow"
    ADVISORY_WARNING = "advisory_warning"
    REQUIRES_REVIEW = "requires_review"
    BLOCKED = "blocked"
    WOULD_HAVE_WARNED = "would_have_warned"
    WOULD_HAVE_BLOCKED = "would_have_blocked"


class GovernanceModeError(ValueError):
    """Typed fail-closed signal for governance-mode contract violations."""

    def __init__(self, reason_code: str, message: str = "") -> None:
        self.reason_code = _required_text(reason_code, "reason_code")
        self.message = message or self.reason_code
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"GovernanceModeError[{self.reason_code}]: {self.message}"


@dataclass(frozen=True, slots=True)
class GovernanceModeDecision:
    """Current-action projection of upstream safety outputs into a governance mode."""

    decision_id: str
    mode: GovernanceMode | str
    enforcement_effect: GovernanceEnforcementEffect | str
    verdict_value: str
    upstream_verdict_id: str
    action_id: str
    run_id: str
    policy_version: str
    shield_version: str
    evidence_refs: tuple[str, ...]
    advisory_only: bool
    enforced: bool
    history_mutated: bool
    summary: str
    shield_decision_value: str = ""
    watcher_action: str = ""
    risk_context_decision: str = ""
    evaluated_at_utc: str = ""
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise GovernanceModeError("schema_version_mismatch", f"expected {SCHEMA_VERSION}")
        mode = GovernanceMode(self.mode)
        effect = GovernanceEnforcementEffect(self.enforcement_effect)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "enforcement_effect", effect)
        for field_name in (
            "decision_id",
            "verdict_value",
            "upstream_verdict_id",
            "action_id",
            "run_id",
            "policy_version",
            "shield_version",
            "summary",
        ):
            _required_text(getattr(self, field_name), field_name)
        object.__setattr__(self, "evidence_refs", _string_tuple(self.evidence_refs, "evidence_refs", required=True))
        if self.history_mutated:
            raise GovernanceModeError("history_mutation_forbidden", "current decisions must not mutate history")
        if mode in {GovernanceMode.OBSERVE, GovernanceMode.WARN, GovernanceMode.RETROSPECTIVE_SCAN} and self.enforced:
            raise GovernanceModeError("advisory_mode_claimed_enforcement", f"{mode.value} cannot enforce")
        if mode is GovernanceMode.RETROSPECTIVE_SCAN:
            raise GovernanceModeError(
                "retrospective_scan_not_current_decision",
                "use RetrospectiveScanReport for retrospective scans",
            )
        if self.advisory_only and self.enforced:
            raise GovernanceModeError("invalid_advisory_enforcement", "advisory decisions cannot be enforced")

    def to_schema_payload(self) -> dict[str, Any]:
        """Return a JSON-schema-ready representation.

        Returns:
            dict[str, Any] value produced by to_schema_payload().
        """
        payload = asdict(self)
        payload["mode"] = GovernanceMode(self.mode).value
        payload["enforcement_effect"] = GovernanceEnforcementEffect(self.enforcement_effect).value
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"GovernanceModeDecision(decision_id={self.decision_id!r}, mode={self.mode!r}, enforcement_effect={self.enforcement_effect!r})"


@dataclass(frozen=True, slots=True)
class RetrospectiveScanInput:
    """Caller-provided immutable snapshots for diagnostic historical replay."""

    trace_ref: str
    action_ref: str
    historical_policy_version: str
    historical_shield_version: str
    candidate_policy_version: str
    candidate_shield_version: str
    historical_action_trace: Mapping[str, Any]
    historical_verdict_payload: Mapping[str, Any]
    run_history_payload: Mapping[str, Any]
    receipt_refs: tuple[str, ...]
    candidate_mode: GovernanceMode | str = GovernanceMode.STRICT
    candidate_rule_refs: tuple[str, ...] = ()
    likely_false_positive_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "trace_ref",
            "action_ref",
            "historical_policy_version",
            "historical_shield_version",
            "candidate_policy_version",
            "candidate_shield_version",
        ):
            _required_text(getattr(self, field_name), field_name)
        mode = GovernanceMode(self.candidate_mode)
        if mode is GovernanceMode.OBSERVE:
            raise GovernanceModeError("invalid_candidate_mode", "retrospective scans require warn or stricter mode")
        object.__setattr__(self, "candidate_mode", mode)
        object.__setattr__(self, "historical_action_trace", dict(self.historical_action_trace))
        object.__setattr__(self, "historical_verdict_payload", dict(self.historical_verdict_payload))
        object.__setattr__(self, "run_history_payload", dict(self.run_history_payload))
        object.__setattr__(self, "receipt_refs", _string_tuple(self.receipt_refs, "receipt_refs", required=True))
        object.__setattr__(self, "candidate_rule_refs", _string_tuple(self.candidate_rule_refs, "candidate_rule_refs"))
        object.__setattr__(
            self,
            "likely_false_positive_notes",
            _string_tuple(self.likely_false_positive_notes, "likely_false_positive_notes"),
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetrospectiveScanInput(trace_ref={self.trace_ref!r}, action_ref={self.action_ref!r}, historical_policy_version={self.historical_policy_version!r})"


@dataclass(frozen=True, slots=True)
class RetrospectiveFinding:
    """Advisory-only finding produced by a retrospective scan."""

    finding_id: str
    enforcement_effect: GovernanceEnforcementEffect | str
    historical_trace_ref: str
    historical_action_ref: str
    historical_verdict_ref: str
    historical_receipt_refs: tuple[str, ...]
    historical_policy_version: str
    historical_shield_version: str
    candidate_policy_version: str
    candidate_shield_version: str
    candidate_rule_refs: tuple[str, ...]
    would_have_warned: bool
    would_have_blocked: bool
    advisory_only: bool
    enforced: bool
    history_mutated: bool
    recommended_shield_packs: tuple[str, ...]
    likely_false_positive_notes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    summary: str
    mode: GovernanceMode | str = GovernanceMode.RETROSPECTIVE_SCAN
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise GovernanceModeError("schema_version_mismatch", f"expected {SCHEMA_VERSION}")
        mode = GovernanceMode(self.mode)
        effect = GovernanceEnforcementEffect(self.enforcement_effect)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "enforcement_effect", effect)
        if mode is not GovernanceMode.RETROSPECTIVE_SCAN:
            raise GovernanceModeError("invalid_retrospective_mode", "findings must use retrospective_scan mode")
        if self.enforced or not self.advisory_only or self.history_mutated:
            raise GovernanceModeError(
                "retrospective_enforcement_forbidden",
                "retrospective findings must be advisory-only, unenforced, and history-immutable",
            )
        for field_name in (
            "finding_id",
            "historical_trace_ref",
            "historical_action_ref",
            "historical_verdict_ref",
            "historical_policy_version",
            "historical_shield_version",
            "candidate_policy_version",
            "candidate_shield_version",
            "summary",
        ):
            _required_text(getattr(self, field_name), field_name)
        object.__setattr__(
            self,
            "historical_receipt_refs",
            _string_tuple(self.historical_receipt_refs, "historical_receipt_refs", required=True),
        )
        object.__setattr__(
            self,
            "candidate_rule_refs",
            _string_tuple(self.candidate_rule_refs, "candidate_rule_refs", required=True),
        )
        object.__setattr__(
            self,
            "recommended_shield_packs",
            _string_tuple(self.recommended_shield_packs, "recommended_shield_packs"),
        )
        object.__setattr__(
            self,
            "likely_false_positive_notes",
            _string_tuple(self.likely_false_positive_notes, "likely_false_positive_notes"),
        )
        object.__setattr__(self, "evidence_refs", _string_tuple(self.evidence_refs, "evidence_refs", required=True))
        if not (self.would_have_warned or self.would_have_blocked):
            raise GovernanceModeError("missing_retrospective_preview", "finding must preview warn or block impact")
        if self.would_have_blocked and effect is not GovernanceEnforcementEffect.WOULD_HAVE_BLOCKED:
            raise GovernanceModeError("invalid_retrospective_effect", "blocked previews require would_have_blocked")
        if not self.would_have_blocked and effect is not GovernanceEnforcementEffect.WOULD_HAVE_WARNED:
            raise GovernanceModeError("invalid_retrospective_effect", "warning previews require would_have_warned")

    def to_schema_payload(self) -> dict[str, Any]:
        """Return a JSON-schema-ready representation.

        Returns:
            dict[str, Any] value produced by to_schema_payload().
        """
        payload = asdict(self)
        payload["mode"] = GovernanceMode(self.mode).value
        payload["enforcement_effect"] = GovernanceEnforcementEffect(self.enforcement_effect).value
        for key in (
            "historical_receipt_refs",
            "candidate_rule_refs",
            "recommended_shield_packs",
            "likely_false_positive_notes",
            "evidence_refs",
        ):
            payload[key] = list(payload[key])
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetrospectiveFinding(finding_id={self.finding_id!r}, enforcement_effect={self.enforcement_effect!r}, historical_trace_ref={self.historical_trace_ref!r})"


@dataclass(frozen=True, slots=True)
class RetrospectiveScanReport:
    """Advisory report summarizing historical replay against candidate policy context."""

    scan_id: str
    candidate_policy_version: str
    candidate_shield_version: str
    scanned_trace_refs: tuple[str, ...]
    findings: tuple[RetrospectiveFinding, ...]
    blast_radius_summary: Mapping[str, Any]
    generated_at_utc: str
    advisory_only: bool = True
    history_mutated: bool = False
    mode: GovernanceMode | str = GovernanceMode.RETROSPECTIVE_SCAN
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise GovernanceModeError("schema_version_mismatch", f"expected {SCHEMA_VERSION}")
        mode = GovernanceMode(self.mode)
        object.__setattr__(self, "mode", mode)
        if mode is not GovernanceMode.RETROSPECTIVE_SCAN:
            raise GovernanceModeError("invalid_retrospective_mode", "reports must use retrospective_scan mode")
        if not self.advisory_only or self.history_mutated:
            raise GovernanceModeError("retrospective_report_not_advisory", "reports cannot claim enforcement")
        for field_name in ("scan_id", "candidate_policy_version", "candidate_shield_version", "generated_at_utc"):
            _required_text(getattr(self, field_name), field_name)
        object.__setattr__(self, "scanned_trace_refs", _string_tuple(self.scanned_trace_refs, "scanned_trace_refs"))
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "blast_radius_summary", dict(self.blast_radius_summary))
        for finding in self.findings:
            if finding.enforced or not finding.advisory_only or finding.history_mutated:
                raise GovernanceModeError("retrospective_enforcement_forbidden")

    def to_schema_payload(self) -> dict[str, Any]:
        """Return a JSON-schema-ready representation."""
        return {
            "schema_version": self.schema_version,
            "mode": GovernanceMode(self.mode).value,
            "scan_id": self.scan_id,
            "candidate_policy_version": self.candidate_policy_version,
            "candidate_shield_version": self.candidate_shield_version,
            "scanned_trace_refs": list(self.scanned_trace_refs),
            "findings": [finding.to_schema_payload() for finding in self.findings],
            "blast_radius_summary": dict(self.blast_radius_summary),
            "generated_at_utc": self.generated_at_utc,
            "advisory_only": self.advisory_only,
            "history_mutated": self.history_mutated,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RetrospectiveScanReport(scan_id={self.scan_id!r}, candidate_policy_version={self.candidate_policy_version!r}, candidate_shield_version={self.candidate_shield_version!r})"


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GovernanceModeError("missing_required_field", f"{field_name} must be non-empty")
    return value.strip()


def _string_tuple(value: object, field_name: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None:
        rows: tuple[str, ...] = ()
    elif isinstance(value, (str, bytes)):
        rows = (str(value).strip(),)
    else:
        if not isinstance(value, Iterable):
            raise GovernanceModeError("invalid_field", f"{field_name} must be a list of strings")
        rows = tuple(str(item).strip() for item in value if str(item).strip())
    if required and not rows:
        raise GovernanceModeError("missing_required_field", f"{field_name} must be non-empty")
    return rows
