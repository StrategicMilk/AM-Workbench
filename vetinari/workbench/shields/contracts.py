"""Immutable contracts for Workbench shield packs."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1.0"


class ShieldRiskDomain(str, Enum):
    """Concrete risk domains covered by Workbench shield packs."""

    DLP = "dlp"
    SHELL_SAFETY = "shell_safety"
    GIT_RELEASE_SAFETY = "git_release_safety"
    BROWSER_NETWORK_EGRESS = "browser_network_egress"
    PACKAGE_INSTALL = "package_install"
    SECRETS = "secrets"
    DESTRUCTIVE_FILESYSTEM = "destructive_filesystem"
    MCP_PROMPT_INJECTION = "mcp_prompt_injection"
    PUBLIC_EXPORT_BOUNDARY = "public_export_boundary"
    MOBILE_REMOTE_CONTROL = "mobile_remote_control"


class ShieldMode(str, Enum):
    """How a selected shield is enforced."""

    OBSERVE = "observe"
    WARN = "warn"
    STRICT = "strict"


class ShieldDecisionValue(str, Enum):
    """Stable shield decision vocabulary."""

    ALLOW = "allow"
    WARN = "warn"
    BLOCK = "block"
    ESCALATE = "escalate"
    DEGRADED = "degraded"


class ShieldFixtureKind(str, Enum):
    """Fixture branch used to prove known-good and known-bad behavior."""

    KNOWN_GOOD = "known_good"
    KNOWN_BAD = "known_bad"


class ShieldRolloutState(str, Enum):
    """Selectable lifecycle state for a shield pack."""

    ACTIVE = "active"
    CANARY = "canary"
    DISABLED = "disabled"
    DEPRECATED = "deprecated"


class WorkbenchShieldPackError(ValueError):
    """Typed fail-closed signal for shield pack loading or evaluation."""

    def __init__(self, reason_code: str, message: str = "") -> None:
        self.reason_code = _non_empty_text(reason_code, "reason_code")
        self.message = message or self.reason_code
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"WorkbenchShieldPackError[{self.reason_code}]: {self.message}"


def _non_empty_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkbenchShieldPackError("missing_required_field", f"{field_name} must be non-empty")
    return value.strip()


def _string_tuple(value: object, field_name: str, *, required: bool = False) -> tuple[str, ...]:
    if value is None:
        rows: tuple[str, ...] = ()
    elif isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise WorkbenchShieldPackError("invalid_field", f"{field_name} must be a list of strings")
    else:
        rows = tuple(str(item).strip() for item in value if str(item).strip())
    if required and not rows:
        raise WorkbenchShieldPackError("missing_required_field", f"{field_name} must be non-empty")
    if len(rows) != len(set(rows)):
        raise WorkbenchShieldPackError("duplicate_value", f"{field_name} must not contain duplicates")
    return rows


def _mapping(value: object, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkbenchShieldPackError("invalid_field", f"{field_name} must be a mapping")
    return value


@dataclass(frozen=True, slots=True)
class ShieldProtectedSurface:
    """One surface protected by a shield rule."""

    surface_id: str
    surface_kind: str
    authority_refs: tuple[str, ...]
    description: str
    tool_surface_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "surface_id", _non_empty_text(self.surface_id, "surface_id"))
        object.__setattr__(self, "surface_kind", _non_empty_text(self.surface_kind, "surface_kind"))
        object.__setattr__(self, "description", _non_empty_text(self.description, "description"))
        object.__setattr__(self, "authority_refs", _string_tuple(self.authority_refs, "authority_refs", required=True))
        object.__setattr__(self, "tool_surface_id", str(self.tool_surface_id).strip())

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ShieldProtectedSurface:
        return cls(
            surface_id=str(payload.get("surface_id", "")),
            surface_kind=str(payload.get("surface_kind", "")),
            authority_refs=_string_tuple(payload.get("authority_refs"), "authority_refs", required=True),
            description=str(payload.get("description", "")),
            tool_surface_id=str(payload.get("tool_surface_id", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface_id": self.surface_id,
            "surface_kind": self.surface_kind,
            "authority_refs": list(self.authority_refs),
            "description": self.description,
            "tool_surface_id": self.tool_surface_id,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShieldProtectedSurface(surface_id={self.surface_id!r}, surface_kind={self.surface_kind!r}, authority_refs={self.authority_refs!r})"


@dataclass(frozen=True, slots=True)
class ShieldFixture:
    """Known-good or known-bad fixture descriptor for one shield pack."""

    fixture_id: str
    kind: ShieldFixtureKind
    summary: str
    action_type: str
    expected_decision: ShieldDecisionValue
    match_patterns: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fixture_id", _non_empty_text(self.fixture_id, "fixture_id"))
        object.__setattr__(self, "kind", ShieldFixtureKind(self.kind))
        object.__setattr__(self, "summary", _non_empty_text(self.summary, "summary"))
        object.__setattr__(self, "action_type", _non_empty_text(self.action_type, "action_type"))
        object.__setattr__(self, "expected_decision", ShieldDecisionValue(self.expected_decision))
        object.__setattr__(self, "match_patterns", _string_tuple(self.match_patterns, "match_patterns", required=True))
        object.__setattr__(self, "evidence_refs", _string_tuple(self.evidence_refs, "evidence_refs", required=True))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ShieldFixture:
        return cls(
            fixture_id=str(payload.get("fixture_id", "")),
            kind=ShieldFixtureKind(str(payload.get("kind", ""))),
            summary=str(payload.get("summary", "")),
            action_type=str(payload.get("action_type", "")),
            expected_decision=ShieldDecisionValue(str(payload.get("expected_decision", ""))),
            match_patterns=_string_tuple(payload.get("match_patterns"), "match_patterns", required=True),
            evidence_refs=_string_tuple(payload.get("evidence_refs"), "evidence_refs", required=True),
            metadata=dict(payload.get("metadata", {}) if isinstance(payload.get("metadata", {}), Mapping) else {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fixture_id": self.fixture_id,
            "kind": self.kind.value,
            "summary": self.summary,
            "action_type": self.action_type,
            "expected_decision": self.expected_decision.value,
            "match_patterns": list(self.match_patterns),
            "evidence_refs": list(self.evidence_refs),
            "metadata": dict(self.metadata),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShieldFixture(fixture_id={self.fixture_id!r}, kind={self.kind!r}, summary={self.summary!r})"


@dataclass(frozen=True, slots=True)
class ShieldRule:
    """Explainable rule that selects fixture-backed shield behavior."""

    rule_id: str
    reason_code: str
    description: str
    protected_surface_refs: tuple[str, ...]
    good_fixture_refs: tuple[str, ...]
    bad_fixture_refs: tuple[str, ...]
    block_patterns: tuple[str, ...]
    capability_pack_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", _non_empty_text(self.rule_id, "rule_id"))
        object.__setattr__(self, "reason_code", _non_empty_text(self.reason_code, "reason_code"))
        object.__setattr__(self, "description", _non_empty_text(self.description, "description"))
        object.__setattr__(
            self,
            "protected_surface_refs",
            _string_tuple(self.protected_surface_refs, "protected_surface_refs", required=True),
        )
        object.__setattr__(
            self, "good_fixture_refs", _string_tuple(self.good_fixture_refs, "good_fixture_refs", required=True)
        )
        object.__setattr__(
            self, "bad_fixture_refs", _string_tuple(self.bad_fixture_refs, "bad_fixture_refs", required=True)
        )
        object.__setattr__(self, "block_patterns", _string_tuple(self.block_patterns, "block_patterns", required=True))
        object.__setattr__(
            self, "capability_pack_refs", _string_tuple(self.capability_pack_refs, "capability_pack_refs")
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> ShieldRule:
        return cls(
            rule_id=str(payload.get("rule_id", "")),
            reason_code=str(payload.get("reason_code", "")),
            description=str(payload.get("description", "")),
            protected_surface_refs=_string_tuple(
                payload.get("protected_surface_refs"),
                "protected_surface_refs",
                required=True,
            ),
            good_fixture_refs=_string_tuple(payload.get("good_fixture_refs"), "good_fixture_refs", required=True),
            bad_fixture_refs=_string_tuple(payload.get("bad_fixture_refs"), "bad_fixture_refs", required=True),
            block_patterns=_string_tuple(payload.get("block_patterns"), "block_patterns", required=True),
            capability_pack_refs=_string_tuple(payload.get("capability_pack_refs"), "capability_pack_refs"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "reason_code": self.reason_code,
            "description": self.description,
            "protected_surface_refs": list(self.protected_surface_refs),
            "good_fixture_refs": list(self.good_fixture_refs),
            "bad_fixture_refs": list(self.bad_fixture_refs),
            "block_patterns": list(self.block_patterns),
            "capability_pack_refs": list(self.capability_pack_refs),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"ShieldRule(rule_id={self.rule_id!r}, reason_code={self.reason_code!r}, description={self.description!r})"
        )


@dataclass(frozen=True, slots=True)
class WorkbenchShieldPack:
    """Versioned, testable, and explainable Workbench shield pack."""

    pack_id: str
    version: str
    risk_domain: ShieldRiskDomain
    protected_surfaces: tuple[ShieldProtectedSurface, ...]
    default_mode: ShieldMode
    selectable_scopes: tuple[str, ...]
    rules: tuple[ShieldRule, ...]
    fixtures: tuple[ShieldFixture, ...]
    owner: str
    rollout_state: ShieldRolloutState
    policy_version: str
    reason_codes: tuple[str, ...]
    capability_pack_refs: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise WorkbenchShieldPackError("schema_version_mismatch", f"expected {SCHEMA_VERSION}")
        object.__setattr__(self, "pack_id", _non_empty_text(self.pack_id, "pack_id"))
        object.__setattr__(self, "version", _non_empty_text(self.version, "version"))
        object.__setattr__(self, "risk_domain", ShieldRiskDomain(self.risk_domain))
        object.__setattr__(self, "default_mode", ShieldMode(self.default_mode))
        object.__setattr__(self, "owner", _non_empty_text(self.owner, "owner"))
        object.__setattr__(self, "rollout_state", ShieldRolloutState(self.rollout_state))
        object.__setattr__(self, "policy_version", _non_empty_text(self.policy_version, "policy_version"))
        object.__setattr__(
            self, "selectable_scopes", _string_tuple(self.selectable_scopes, "selectable_scopes", required=True)
        )
        object.__setattr__(self, "reason_codes", _string_tuple(self.reason_codes, "reason_codes", required=True))
        object.__setattr__(
            self, "capability_pack_refs", _string_tuple(self.capability_pack_refs, "capability_pack_refs")
        )
        object.__setattr__(self, "protected_surfaces", tuple(self.protected_surfaces))
        object.__setattr__(self, "rules", tuple(self.rules))
        object.__setattr__(self, "fixtures", tuple(self.fixtures))
        if not self.protected_surfaces:
            raise WorkbenchShieldPackError("missing_protected_surface", f"{self.pack_id} has no protected surfaces")
        if not self.rules:
            raise WorkbenchShieldPackError("missing_rule", f"{self.pack_id} has no rules")
        if not self.fixtures:
            raise WorkbenchShieldPackError("missing_fixture", f"{self.pack_id} has no fixtures")
        self._validate_references()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> WorkbenchShieldPack:
        return cls(
            schema_version=str(payload.get("schema_version", "")),
            pack_id=str(payload.get("pack_id", "")),
            version=str(payload.get("version", "")),
            risk_domain=ShieldRiskDomain(str(payload.get("risk_domain", ""))),
            protected_surfaces=tuple(
                ShieldProtectedSurface.from_mapping(_mapping(row, "protected_surface"))
                for row in payload.get("protected_surfaces", ())
            ),
            default_mode=ShieldMode(str(payload.get("default_mode", ""))),
            selectable_scopes=_string_tuple(payload.get("selectable_scopes"), "selectable_scopes", required=True),
            rules=tuple(ShieldRule.from_mapping(_mapping(row, "rule")) for row in payload.get("rules", ())),
            fixtures=tuple(ShieldFixture.from_mapping(_mapping(row, "fixture")) for row in payload.get("fixtures", ())),
            owner=str(payload.get("owner", "")),
            rollout_state=ShieldRolloutState(str(payload.get("rollout_state", ""))),
            policy_version=str(payload.get("policy_version", "")),
            reason_codes=_string_tuple(payload.get("reason_codes"), "reason_codes", required=True),
            capability_pack_refs=_string_tuple(payload.get("capability_pack_refs"), "capability_pack_refs"),
        )

    def _validate_references(self) -> None:
        surface_ids = {surface.surface_id for surface in self.protected_surfaces}
        fixture_ids = {fixture.fixture_id for fixture in self.fixtures}
        kinds = {fixture.kind for fixture in self.fixtures}
        if ShieldFixtureKind.KNOWN_GOOD not in kinds or ShieldFixtureKind.KNOWN_BAD not in kinds:
            raise WorkbenchShieldPackError(
                "missing_fixture_pair", f"{self.pack_id} needs known-good and known-bad fixtures"
            )
        for rule in self.rules:
            if rule.reason_code not in self.reason_codes:
                raise WorkbenchShieldPackError("unknown_reason_code", f"{rule.reason_code} is not declared")
            if not set(rule.protected_surface_refs).issubset(surface_ids):
                raise WorkbenchShieldPackError(
                    "unknown_protected_surface", f"{rule.rule_id} references unknown surface"
                )
            if not set(rule.good_fixture_refs + rule.bad_fixture_refs).issubset(fixture_ids):
                raise WorkbenchShieldPackError("unknown_fixture", f"{rule.rule_id} references unknown fixture")

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        payload = asdict(self)
        payload["risk_domain"] = self.risk_domain.value
        payload["default_mode"] = self.default_mode.value
        payload["rollout_state"] = self.rollout_state.value
        payload["protected_surfaces"] = [surface.to_dict() for surface in self.protected_surfaces]
        payload["rules"] = [rule.to_dict() for rule in self.rules]
        payload["fixtures"] = [fixture.to_dict() for fixture in self.fixtures]
        payload["selectable_scopes"] = list(self.selectable_scopes)
        payload["reason_codes"] = list(self.reason_codes)
        payload["capability_pack_refs"] = list(self.capability_pack_refs)
        return payload

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"WorkbenchShieldPack(pack_id={self.pack_id!r}, version={self.version!r}, risk_domain={self.risk_domain!r})"
        )


@dataclass(frozen=True, slots=True)
class ShieldEvaluationRequest:
    """Input facts used by the shield evaluator."""

    pack_id: str
    action_summary: str
    actor_id: str
    run_id: str
    scope: str
    action_type: str = "action"
    fixture_id: str = ""
    risk_domain: ShieldRiskDomain | str | None = None
    tool_surface_ids: tuple[str, ...] = ()
    capability_pack_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    authority_refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "pack_id", _non_empty_text(self.pack_id, "pack_id"))
        object.__setattr__(self, "action_summary", _non_empty_text(self.action_summary, "action_summary"))
        object.__setattr__(self, "actor_id", _non_empty_text(self.actor_id, "actor_id"))
        object.__setattr__(self, "run_id", _non_empty_text(self.run_id, "run_id"))
        object.__setattr__(self, "scope", _non_empty_text(self.scope, "scope"))
        object.__setattr__(self, "action_type", _non_empty_text(self.action_type, "action_type"))
        object.__setattr__(self, "fixture_id", str(self.fixture_id).strip())
        object.__setattr__(self, "tool_surface_ids", _string_tuple(self.tool_surface_ids, "tool_surface_ids"))
        object.__setattr__(self, "capability_pack_ids", _string_tuple(self.capability_pack_ids, "capability_pack_ids"))
        object.__setattr__(self, "evidence_refs", _string_tuple(self.evidence_refs, "evidence_refs"))
        object.__setattr__(self, "authority_refs", _string_tuple(self.authority_refs, "authority_refs"))
        object.__setattr__(self, "metadata", dict(self.metadata))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShieldEvaluationRequest(pack_id={self.pack_id!r}, action_summary={self.action_summary!r}, actor_id={self.actor_id!r})"


@dataclass(frozen=True, slots=True)
class ShieldDecision:
    """Explainable output from one shield evaluation."""

    value: ShieldDecisionValue
    pack_id: str
    rule_id: str
    reason_code: str
    policy_version: str
    evidence_refs: tuple[str, ...]
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", ShieldDecisionValue(self.value))
        object.__setattr__(self, "pack_id", _non_empty_text(self.pack_id, "pack_id"))
        object.__setattr__(self, "rule_id", _non_empty_text(self.rule_id, "rule_id"))
        object.__setattr__(self, "reason_code", _non_empty_text(self.reason_code, "reason_code"))
        object.__setattr__(self, "policy_version", _non_empty_text(self.policy_version, "policy_version"))
        object.__setattr__(self, "evidence_refs", _string_tuple(self.evidence_refs, "evidence_refs", required=True))
        object.__setattr__(self, "details", dict(self.details))

    @property
    def allowed(self) -> bool:
        return self.value in {ShieldDecisionValue.ALLOW, ShieldDecisionValue.WARN}

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value.value,
            "pack_id": self.pack_id,
            "rule_id": self.rule_id,
            "reason_code": self.reason_code,
            "policy_version": self.policy_version,
            "evidence_refs": list(self.evidence_refs),
            "details": dict(self.details),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ShieldDecision(value={self.value!r}, pack_id={self.pack_id!r}, rule_id={self.rule_id!r})"
