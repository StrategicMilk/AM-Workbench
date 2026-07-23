"""Fail-closed mode lens catalog and transition runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.conversation import Conversation, ConversationMode, ConversationSafetyContext
from vetinari.workbench.modes.templates import load_mode_template_catalog
from vetinari.workbench.rigor import RigorLevel, apply_rigor_level

DEFAULT_MODE_LENSES_PATH = PROJECT_ROOT / "config" / "workbench" / "mode_lenses.yaml"
REQUIRED_MODE_LENSES = {
    "casual_chat",
    "creative_exploration",
    "professional_assistance",
    "life_admin",
    "research",
    "structured_workbench",
}


class ModeLensCatalogError(ValueError):
    """Raised when mode lens configuration cannot be trusted."""


class ModeLensTransitionRejected(ValueError):
    """Raised when a transition request is malformed before a decision can be built."""


@dataclass(frozen=True, slots=True)
class MemoryDefaults:
    """Memory policy defaults exposed by a lens."""

    ephemeral_scope: tuple[str, ...]
    persistent_scope: tuple[str, ...]
    retention_policy: str
    provenance_required: bool

    def __post_init__(self) -> None:
        if not self.ephemeral_scope and not self.persistent_scope:
            raise ModeLensCatalogError("memory defaults must declare at least one scope")
        _require_text(self.retention_policy, "memory_defaults.retention_policy")
        if self.provenance_required is not True:
            raise ModeLensCatalogError("memory defaults must require provenance")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryDefaults(ephemeral_scope={self.ephemeral_scope!r}, persistent_scope={self.persistent_scope!r}, retention_policy={self.retention_policy!r})"


@dataclass(frozen=True, slots=True)
class SensitiveDomainPolicy:
    """Fail-closed behavior for sensitive domains inside a lens."""

    domains: tuple[str, ...]
    requires_authority: bool
    requires_evidence: bool
    behavior: str

    def __post_init__(self) -> None:
        _require_string_tuple(self.domains, "sensitive_domain_policy.domains")
        if self.requires_authority is not True:
            raise ModeLensCatalogError("sensitive domain policy must require authority")
        if self.requires_evidence is not True:
            raise ModeLensCatalogError("sensitive domain policy must require evidence")
        _require_text(self.behavior, "sensitive_domain_policy.behavior")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SensitiveDomainPolicy(domains={self.domains!r}, requires_authority={self.requires_authority!r}, requires_evidence={self.requires_evidence!r})"


@dataclass(frozen=True, slots=True)
class ModeLens:
    """A user-facing lens over conversation, rigor, and mode-template contracts."""

    lens_id: str
    label: str
    conversation_mode: ConversationMode
    mode_template_id: str
    tone: str
    memory_defaults: MemoryDefaults
    allowed_tools: tuple[str, ...]
    artifact_suggestions: tuple[str, ...]
    rigor_default: RigorLevel
    transition_targets: tuple[str, ...]
    sensitive_domain_policy: SensitiveDomainPolicy
    authority_ref: str
    provenance_ref: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_text(self.lens_id, "lens_id")
        _require_text(self.label, "label")
        if not isinstance(self.conversation_mode, ConversationMode):
            raise ModeLensCatalogError("conversation_mode must be ConversationMode")
        _require_text(self.mode_template_id, "mode_template_id")
        _require_text(self.tone, "tone")
        _require_string_tuple(self.allowed_tools, "allowed_tools")
        _require_string_tuple(self.artifact_suggestions, "artifact_suggestions")
        if not isinstance(self.rigor_default, RigorLevel):
            raise ModeLensCatalogError("rigor_default must be RigorLevel")
        _require_string_tuple(self.transition_targets, "transition_targets")
        _require_text(self.authority_ref, "authority_ref")
        _require_text(self.provenance_ref, "provenance_ref")
        _require_string_tuple(self.evidence_refs, "evidence_refs")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable lens payload for UI/API callers."""
        return {
            "lens_id": self.lens_id,
            "label": self.label,
            "conversation_mode": self.conversation_mode.value,
            "mode_template_id": self.mode_template_id,
            "tone": self.tone,
            "memory_defaults": {
                "ephemeral_scope": list(self.memory_defaults.ephemeral_scope),
                "persistent_scope": list(self.memory_defaults.persistent_scope),
                "retention_policy": self.memory_defaults.retention_policy,
                "provenance_required": self.memory_defaults.provenance_required,
            },
            "allowed_tools": list(self.allowed_tools),
            "artifact_suggestions": list(self.artifact_suggestions),
            "rigor_default": self.rigor_default.value,
            "transition_targets": list(self.transition_targets),
            "sensitive_domain_policy": {
                "domains": list(self.sensitive_domain_policy.domains),
                "requires_authority": self.sensitive_domain_policy.requires_authority,
                "requires_evidence": self.sensitive_domain_policy.requires_evidence,
                "behavior": self.sensitive_domain_policy.behavior,
            },
            "authority_ref": self.authority_ref,
            "provenance_ref": self.provenance_ref,
            "evidence_refs": list(self.evidence_refs),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ModeLens(lens_id={self.lens_id!r}, label={self.label!r}, conversation_mode={self.conversation_mode!r})"


@dataclass(frozen=True, slots=True)
class ModeLensTransitionDecision:
    """Typed transition result that preserves conversation continuity."""

    status: str
    target_lens_id: str
    conversation_id: str
    active_branch_id: str
    source_mode: ConversationMode
    target_mode: ConversationMode | None
    rigor_default: RigorLevel | None
    mode_template_id: str | None
    transition_evidence_refs: tuple[str, ...]
    blocked_reason: str | None = None

    @property
    def accepted(self) -> bool:
        """Return whether the transition may proceed."""
        return self.status == "accepted"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable decision."""
        return {
            "status": self.status,
            "target_lens_id": self.target_lens_id,
            "conversation_id": self.conversation_id,
            "active_branch_id": self.active_branch_id,
            "source_mode": self.source_mode.value,
            "target_mode": self.target_mode.value if self.target_mode is not None else None,
            "rigor_default": self.rigor_default.value if self.rigor_default is not None else None,
            "mode_template_id": self.mode_template_id,
            "transition_evidence_refs": list(self.transition_evidence_refs),
            "blocked_reason": self.blocked_reason,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ModeLensTransitionDecision(status={self.status!r}, target_lens_id={self.target_lens_id!r}, conversation_id={self.conversation_id!r})"


def load_mode_lenses(path: Path | str = DEFAULT_MODE_LENSES_PATH) -> tuple[ModeLens, ...]:
    """Load and validate the six-lens catalog.

    Returns:
        Resolved mode lenses value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    catalog_path = Path(path)
    try:
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModeLensCatalogError(f"mode lens catalog unreadable: {catalog_path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ModeLensCatalogError("mode lens catalog schema_version must be 1")
    rows = raw.get("lenses")
    if not isinstance(rows, list) or not rows:
        raise ModeLensCatalogError("mode lens catalog must contain lenses")

    lenses = tuple(_parse_lens_row(row) for row in rows)
    lens_ids = [lens.lens_id for lens in lenses]
    if len(set(lens_ids)) != len(lens_ids):
        raise ModeLensCatalogError("mode lens ids must be unique")
    missing = REQUIRED_MODE_LENSES - set(lens_ids)
    if missing:
        raise ModeLensCatalogError(f"mode lens catalog missing required lenses: {sorted(missing)}")
    extra = set(lens_ids) - REQUIRED_MODE_LENSES
    if extra:
        raise ModeLensCatalogError(f"mode lens catalog contains unknown lenses: {sorted(extra)}")

    template_ids = {template.template_id for template in load_mode_template_catalog(use_cache=False)}
    for lens in lenses:
        if lens.mode_template_id not in template_ids:
            raise ModeLensCatalogError(f"mode template not found for lens {lens.lens_id!r}")
        unknown_targets = set(lens.transition_targets) - set(lens_ids)
        if unknown_targets:
            raise ModeLensCatalogError(
                f"lens {lens.lens_id!r} has unknown transition targets: {sorted(unknown_targets)}"
            )
        apply_rigor_level(level=lens.rigor_default, mode=lens.conversation_mode.value)
    return lenses


def list_mode_lenses() -> tuple[ModeLens, ...]:
    """List all configured mode lenses."""
    return load_mode_lenses()


def get_mode_lens(lens_id: str, *, catalog: tuple[ModeLens, ...] | None = None) -> ModeLens | None:
    """Return one configured lens by id.

    Returns:
        Resolved mode lens value.
    """
    _require_text(lens_id, "lens_id")
    lenses = catalog if catalog is not None else load_mode_lenses()
    for lens in lenses:
        if lens.lens_id == lens_id:
            return lens
    return None


def apply_mode_lens_transition(
    conversation: Conversation,
    target_lens_id: str,
    *,
    safety_context: ConversationSafetyContext,
    source_lens_id: str | None = None,
    sensitive_domain: str | None = None,
) -> ModeLensTransitionDecision:
    """Return a typed transition decision without mutating the conversation.

    Args:
        conversation: Conversation value consumed by apply_mode_lens_transition().
        target_lens_id: Target object or path updated by the operation.
        safety_context: Safety context value consumed by apply_mode_lens_transition().
        source_lens_id: Source object or text processed by the operation.
        sensitive_domain: Sensitive domain value consumed by apply_mode_lens_transition().

    Returns:
        ModeLensTransitionDecision value produced by apply_mode_lens_transition().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(conversation, Conversation):
        raise ModeLensTransitionRejected("conversation must be a Conversation")
    _require_text(target_lens_id, "target_lens_id")

    catalog = load_mode_lenses()
    target = get_mode_lens(target_lens_id, catalog=catalog)
    if target is None:
        return _rejected(conversation, target_lens_id, f"unknown mode lens: {target_lens_id}")

    if source_lens_id:
        source = get_mode_lens(source_lens_id, catalog=catalog)
        if source is None:
            return _rejected(conversation, target_lens_id, f"unknown source mode lens: {source_lens_id}")
        if target_lens_id not in source.transition_targets:
            return _rejected(conversation, target_lens_id, "transition target not allowed by source lens")

    missing = _missing_context(safety_context)
    if missing:
        return _rejected(conversation, target_lens_id, "missing " + ", ".join(missing))

    if sensitive_domain:
        domain = sensitive_domain.strip()
        if not domain:
            return _rejected(conversation, target_lens_id, "sensitive_domain must be non-empty")
        if domain not in target.sensitive_domain_policy.domains:
            return _rejected(conversation, target_lens_id, f"sensitive-domain policy gap: {domain}")
        if target.sensitive_domain_policy.requires_authority and not safety_context.authority_ref:
            return _rejected(conversation, target_lens_id, "sensitive-domain transition requires authority")
        if target.sensitive_domain_policy.requires_evidence and not safety_context.evidence_refs:
            return _rejected(conversation, target_lens_id, "sensitive-domain transition requires evidence")

    return ModeLensTransitionDecision(
        status="accepted",
        target_lens_id=target.lens_id,
        conversation_id=conversation.conversation_id,
        active_branch_id=conversation.active_branch_id,
        source_mode=conversation.active_mode,
        target_mode=target.conversation_mode,
        rigor_default=target.rigor_default,
        mode_template_id=target.mode_template_id,
        transition_evidence_refs=(
            f"conversation:{conversation.conversation_id}",
            f"branch:{conversation.active_branch_id}",
            *safety_context.evidence_refs,
        ),
    )


def _parse_lens_row(row: object) -> ModeLens:
    if not isinstance(row, dict):
        raise ModeLensCatalogError("mode lens row must be a mapping")
    required = {
        "id",
        "label",
        "conversation_mode",
        "mode_template_id",
        "tone",
        "memory_defaults",
        "allowed_tools",
        "artifact_suggestions",
        "rigor_default",
        "transition_targets",
        "sensitive_domain_policy",
        "authority_ref",
        "provenance_ref",
        "evidence_refs",
    }
    missing = required - set(row)
    if missing:
        raise ModeLensCatalogError(f"mode lens row missing keys: {sorted(missing)}")
    return ModeLens(
        lens_id=str(row["id"]),
        label=str(row["label"]),
        conversation_mode=ConversationMode(str(row["conversation_mode"])),
        mode_template_id=str(row["mode_template_id"]),
        tone=str(row["tone"]),
        memory_defaults=_parse_memory_defaults(row["memory_defaults"]),
        allowed_tools=_string_tuple(row["allowed_tools"]),
        artifact_suggestions=_string_tuple(row["artifact_suggestions"]),
        rigor_default=RigorLevel(str(row["rigor_default"])),
        transition_targets=_string_tuple(row["transition_targets"]),
        sensitive_domain_policy=_parse_sensitive_policy(row["sensitive_domain_policy"]),
        authority_ref=str(row["authority_ref"]),
        provenance_ref=str(row["provenance_ref"]),
        evidence_refs=_string_tuple(row["evidence_refs"]),
    )


def _parse_memory_defaults(raw: object) -> MemoryDefaults:
    if not isinstance(raw, dict):
        raise ModeLensCatalogError("memory_defaults must be a mapping")
    return MemoryDefaults(
        ephemeral_scope=_string_tuple(raw.get("ephemeral_scope", ())),
        persistent_scope=_string_tuple(raw.get("persistent_scope", ())),
        retention_policy=str(raw.get("retention_policy", "")),
        provenance_required=bool(raw.get("provenance_required", False)),
    )


def _parse_sensitive_policy(raw: object) -> SensitiveDomainPolicy:
    if not isinstance(raw, dict):
        raise ModeLensCatalogError("sensitive_domain_policy must be a mapping")
    return SensitiveDomainPolicy(
        domains=_string_tuple(raw.get("domains", ())),
        requires_authority=bool(raw.get("requires_authority", False)),
        requires_evidence=bool(raw.get("requires_evidence", False)),
        behavior=str(raw.get("behavior", "")),
    )


def _rejected(conversation: Conversation, target_lens_id: str, reason: str) -> ModeLensTransitionDecision:
    return ModeLensTransitionDecision(
        status="rejected",
        target_lens_id=target_lens_id,
        conversation_id=conversation.conversation_id,
        active_branch_id=conversation.active_branch_id,
        source_mode=conversation.active_mode,
        target_mode=None,
        rigor_default=None,
        mode_template_id=None,
        transition_evidence_refs=(),
        blocked_reason=reason,
    )


def _missing_context(context: ConversationSafetyContext) -> tuple[str, ...]:
    missing: list[str] = []
    if not context.consent_ref:
        missing.append("consent")
    if not context.authority_ref:
        missing.append("authority")
    if not context.project_id:
        missing.append("project context")
    if not context.evidence_refs:
        missing.append("evidence")
    return tuple(missing)


def _string_tuple(raw: object) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw if str(item).strip())
    return (str(raw),)


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ModeLensCatalogError(f"{field_name} must be non-empty")


def _require_string_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise ModeLensCatalogError(f"{field_name} must be a non-empty tuple")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ModeLensCatalogError(f"{field_name} must contain non-empty strings")


__all__ = [
    "DEFAULT_MODE_LENSES_PATH",
    "ModeLens",
    "ModeLensCatalogError",
    "ModeLensTransitionDecision",
    "ModeLensTransitionRejected",
    "SensitiveDomainPolicy",
    "apply_mode_lens_transition",
    "get_mode_lens",
    "list_mode_lenses",
    "load_mode_lenses",
]
