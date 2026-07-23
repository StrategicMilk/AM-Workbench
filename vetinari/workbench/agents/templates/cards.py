"""Fail-closed Workbench agent-template gallery loader."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from vetinari.workbench.capability_packs import load_capability_pack_catalog
from vetinari.workbench.modes.templates import load_mode_template_catalog

DEFAULT_AGENT_TEMPLATE_GALLERY_PATH = Path("config/workbench_agent_templates.yaml")
_AGENT_TEMPLATE_SCHEMA_VERSION = 1
_AGENT_TEMPLATE_GALLERY_LOCK = threading.Lock()
_AGENT_TEMPLATE_GALLERY_CACHE: tuple[AgentTemplateCard, ...] | None = None


class AgentTemplateCatalogError(RuntimeError):
    """Raised when the Workbench agent-template gallery cannot be trusted."""


@dataclass(frozen=True, slots=True)
class AgentTemplateTrustBadges:
    """Structured trust signals shown before an agent template is spawned."""

    local_only: bool
    networked: bool
    secret_accessing: bool
    model_training: bool
    writes_files: bool
    deploys_code: bool
    incident_response_capable: bool

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AgentTemplateTrustBadges(local_only={self.local_only!r}, networked={self.networked!r}, secret_accessing={self.secret_accessing!r})"


@dataclass(frozen=True, slots=True)
class AgentTemplateRiskPosture:
    """Explicit risk posture for a gallery template."""

    risk_level: str
    review_required: bool
    approval_required: bool
    isolation_profile: str

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AgentTemplateRiskPosture(risk_level={self.risk_level!r}, review_required={self.review_required!r}, approval_required={self.approval_required!r})"


@dataclass(frozen=True, slots=True)
class AgentTemplateCard:
    """Immutable contract row for one installable Workbench agent template."""

    template_id: str
    name: str
    role: str
    purpose: str
    allowed_tools: tuple[str, ...]
    permissions: tuple[str, ...]
    memory_scope: tuple[str, ...]
    model_policy: dict[str, Any]
    cost_profile: dict[str, Any]
    eval_history: tuple[str, ...]
    failure_history: tuple[str, ...]
    allowed_data_classes: tuple[str, ...]
    compatibility_requirements: tuple[str, ...]
    capability_pack_ids: tuple[str, ...]
    mode_template_ids: tuple[str, ...]
    sandbox_profile: str
    trust_badges: AgentTemplateTrustBadges
    risk_posture: AgentTemplateRiskPosture
    revision: str
    provenance: dict[str, str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the card."""
        return {
            "template_id": self.template_id,
            "name": self.name,
            "role": self.role,
            "purpose": self.purpose,
            "allowed_tools": list(self.allowed_tools),
            "permissions": list(self.permissions),
            "memory_scope": list(self.memory_scope),
            "model_policy": dict(self.model_policy),
            "cost_profile": dict(self.cost_profile),
            "eval_history": list(self.eval_history),
            "failure_history": list(self.failure_history),
            "allowed_data_classes": list(self.allowed_data_classes),
            "compatibility_requirements": list(self.compatibility_requirements),
            "capability_pack_ids": list(self.capability_pack_ids),
            "mode_template_ids": list(self.mode_template_ids),
            "sandbox_profile": self.sandbox_profile,
            "trust_badges": {
                "local_only": self.trust_badges.local_only,
                "networked": self.trust_badges.networked,
                "secret_accessing": self.trust_badges.secret_accessing,
                "model_training": self.trust_badges.model_training,
                "writes_files": self.trust_badges.writes_files,
                "deploys_code": self.trust_badges.deploys_code,
                "incident_response_capable": self.trust_badges.incident_response_capable,
            },
            "risk_posture": {
                "risk_level": self.risk_posture.risk_level,
                "review_required": self.risk_posture.review_required,
                "approval_required": self.risk_posture.approval_required,
                "isolation_profile": self.risk_posture.isolation_profile,
            },
            "revision": self.revision,
            "provenance": dict(self.provenance),
            "metadata": dict(self.metadata),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AgentTemplateCard(template_id={self.template_id!r}, name={self.name!r}, role={self.role!r})"


def load_agent_template_gallery(path: Path | str | None = None) -> tuple[AgentTemplateCard, ...]:
    """
    Load validated agent-template cards, failing closed on untrusted data.

    Returns:
        The operation result.
    """
    global _AGENT_TEMPLATE_GALLERY_CACHE
    if path is not None:
        return _load_agent_template_gallery_uncached(Path(path))
    with _AGENT_TEMPLATE_GALLERY_LOCK:
        if _AGENT_TEMPLATE_GALLERY_CACHE is None:
            loaded = _load_agent_template_gallery_uncached(DEFAULT_AGENT_TEMPLATE_GALLERY_PATH)
            _AGENT_TEMPLATE_GALLERY_CACHE = loaded
        return _AGENT_TEMPLATE_GALLERY_CACHE


def reset_agent_template_gallery_for_test() -> None:
    """Clear the process-local gallery cache for deterministic tests."""
    global _AGENT_TEMPLATE_GALLERY_CACHE
    with _AGENT_TEMPLATE_GALLERY_LOCK:
        _AGENT_TEMPLATE_GALLERY_CACHE = None


def _load_agent_template_gallery_uncached(path: Path) -> tuple[AgentTemplateCard, ...]:
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AgentTemplateCatalogError(f"agent template gallery unreadable: {path}") from exc
    if not isinstance(doc, dict):
        raise AgentTemplateCatalogError("agent template gallery must be a mapping")
    if doc.get("schema_version") != _AGENT_TEMPLATE_SCHEMA_VERSION:
        raise AgentTemplateCatalogError(
            f"agent template gallery schema mismatch: expected {_AGENT_TEMPLATE_SCHEMA_VERSION}, "
            f"got {doc.get('schema_version')!r}",
        )
    raw_templates = doc.get("templates")
    if not isinstance(raw_templates, list) or not raw_templates:
        raise AgentTemplateCatalogError("agent template gallery must contain non-empty templates")
    live_mode_ids = {row.template_id for row in load_mode_template_catalog()}
    live_pack_ids = {row.pack_id for row in load_capability_pack_catalog()}
    seen: set[str] = set()
    rows = tuple(_card_from_mapping(raw, live_mode_ids, live_pack_ids) for raw in raw_templates)
    for row in rows:
        if row.template_id in seen:
            raise AgentTemplateCatalogError(f"duplicate agent template id {row.template_id}")
        seen.add(row.template_id)
    return rows


def _card_from_mapping(
    raw: Any,
    live_mode_ids: set[str],
    live_pack_ids: set[str],
) -> AgentTemplateCard:
    if not isinstance(raw, dict):
        raise AgentTemplateCatalogError("agent template row must be a mapping")
    template_id = _non_empty(raw.get("template_id"), "template_id")
    capability_pack_ids = _non_empty_tuple(raw.get("capability_pack_ids"), "capability_pack_ids", template_id)
    mode_template_ids = _non_empty_tuple(raw.get("mode_template_ids"), "mode_template_ids", template_id)
    unknown_packs = tuple(sorted(set(capability_pack_ids) - live_pack_ids))
    if unknown_packs:
        raise AgentTemplateCatalogError(f"unknown capability packs for {template_id}: {unknown_packs}")
    unknown_modes = tuple(sorted(set(mode_template_ids) - live_mode_ids))
    if unknown_modes:
        raise AgentTemplateCatalogError(f"unknown mode templates for {template_id}: {unknown_modes}")
    return AgentTemplateCard(
        template_id=template_id,
        name=_non_empty(raw.get("name"), "name", template_id),
        role=_non_empty(raw.get("role"), "role", template_id),
        purpose=_non_empty(raw.get("purpose"), "purpose", template_id),
        allowed_tools=_non_empty_tuple(raw.get("allowed_tools"), "allowed_tools", template_id),
        permissions=_non_empty_tuple(raw.get("permissions"), "permissions", template_id),
        memory_scope=_non_empty_tuple(raw.get("memory_scope"), "memory_scope", template_id),
        model_policy=_non_empty_mapping(raw.get("model_policy"), "model_policy", template_id),
        cost_profile=_non_empty_mapping(raw.get("cost_profile"), "cost_profile", template_id),
        eval_history=_non_empty_tuple(raw.get("eval_history"), "eval_history", template_id),
        failure_history=_non_empty_tuple(raw.get("failure_history"), "failure_history", template_id),
        allowed_data_classes=_non_empty_tuple(raw.get("allowed_data_classes"), "allowed_data_classes", template_id),
        compatibility_requirements=_non_empty_tuple(
            raw.get("compatibility_requirements"),
            "compatibility_requirements",
            template_id,
        ),
        capability_pack_ids=capability_pack_ids,
        mode_template_ids=mode_template_ids,
        sandbox_profile=_non_empty(raw.get("sandbox_profile"), "sandbox_profile", template_id),
        trust_badges=_trust_badges_from_mapping(raw.get("trust_badges"), template_id),
        risk_posture=_risk_posture_from_mapping(raw.get("risk_posture"), template_id),
        revision=_non_empty(raw.get("revision"), "revision", template_id),
        provenance=_string_mapping(raw.get("provenance"), "provenance", template_id),
        metadata=dict(raw.get("metadata", {}) if isinstance(raw.get("metadata", {}), dict) else {}),
    )


def _non_empty(value: Any, field_name: str, template_id: str | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        suffix = f" for {template_id}" if template_id else ""
        raise AgentTemplateCatalogError(f"missing {field_name}{suffix}")
    return value.strip()


def _non_empty_tuple(value: Any, field_name: str, template_id: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise AgentTemplateCatalogError(f"{field_name} must be a non-empty list for {template_id}")
    rows = tuple(str(item).strip() for item in value if str(item).strip())
    if not rows:
        raise AgentTemplateCatalogError(f"missing {field_name} for {template_id}")
    return rows


def _non_empty_mapping(value: Any, field_name: str, template_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise AgentTemplateCatalogError(f"missing {field_name} for {template_id}")
    return dict(value)


def _string_mapping(value: Any, field_name: str, template_id: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise AgentTemplateCatalogError(f"missing {field_name} for {template_id}")
    rows = {str(key): str(item).strip() for key, item in value.items() if str(key).strip() and str(item).strip()}
    if not rows:
        raise AgentTemplateCatalogError(f"missing {field_name} for {template_id}")
    return rows


def _trust_badges_from_mapping(value: Any, template_id: str) -> AgentTemplateTrustBadges:
    if not isinstance(value, dict):
        raise AgentTemplateCatalogError(f"missing trust_badges for {template_id}")
    required = (
        "local_only",
        "networked",
        "secret_accessing",
        "model_training",
        "writes_files",
        "deploys_code",
        "incident_response_capable",
    )
    for field_name in required:
        if not isinstance(value.get(field_name), bool):
            raise AgentTemplateCatalogError(f"trust_badges.{field_name} must be bool for {template_id}")
    return AgentTemplateTrustBadges(
        local_only=value["local_only"],
        networked=value["networked"],
        secret_accessing=value["secret_accessing"],
        model_training=value["model_training"],
        writes_files=value["writes_files"],
        deploys_code=value["deploys_code"],
        incident_response_capable=value["incident_response_capable"],
    )


def _risk_posture_from_mapping(value: Any, template_id: str) -> AgentTemplateRiskPosture:
    if not isinstance(value, dict):
        raise AgentTemplateCatalogError(f"missing risk_posture for {template_id}")
    if not isinstance(value.get("review_required"), bool) or not isinstance(value.get("approval_required"), bool):
        raise AgentTemplateCatalogError(f"risk_posture review and approval flags must be bool for {template_id}")
    return AgentTemplateRiskPosture(
        risk_level=_non_empty(value.get("risk_level"), "risk_posture.risk_level", template_id),
        review_required=value["review_required"],
        approval_required=value["approval_required"],
        isolation_profile=_non_empty(value.get("isolation_profile"), "risk_posture.isolation_profile", template_id),
    )
