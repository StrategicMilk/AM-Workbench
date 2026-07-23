"""YAML-backed sensitive-domain requirement policies."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT
from vetinari.workbench.life_admin.contracts import (
    SensitiveDomainKind,
    SensitiveWorkflowError,
    WorkflowOutcomeKind,
)
from vetinari.workbench.professional.contracts import PromotedArtifactKind
from vetinari.workbench.rigor import RigorLevel

_POLICY_CATALOG_CONFIG_PATH = PROJECT_ROOT / "config" / "workbench" / "sensitive_workflow_policies.yaml"
_POLICY_CATALOG_LOCK = threading.Lock()
_POLICY_CATALOG_CACHE: dict[SensitiveDomainKind, SensitiveDomainRequirementPolicy] = {}
_HIGH_STAKES_DOMAINS = frozenset({
    SensitiveDomainKind.TAX,
    SensitiveDomainKind.FINANCE,
    SensitiveDomainKind.LEGAL,
    SensitiveDomainKind.MEDICAL,
    SensitiveDomainKind.EMPLOYMENT,
    SensitiveDomainKind.HOUSING,
    SensitiveDomainKind.SAFETY,
})


@dataclass(frozen=True, slots=True)
class SensitiveDomainRequirementPolicy:
    """Requirements that must be satisfied before a sensitive workflow is allowed."""

    domain_kind: SensitiveDomainKind
    requires_jurisdiction: bool
    requires_tax_year: bool
    requires_authority: bool
    requires_evidence: bool
    min_rigor_level: RigorLevel
    default_workflow_outcome_kind: WorkflowOutcomeKind
    permitted_promotion_kinds: tuple[PromotedArtifactKind, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.domain_kind, SensitiveDomainKind):
            raise SensitiveWorkflowError("domain_kind must be SensitiveDomainKind")
        if not isinstance(self.min_rigor_level, RigorLevel):
            raise SensitiveWorkflowError("min_rigor_level must be RigorLevel")
        if not isinstance(self.default_workflow_outcome_kind, WorkflowOutcomeKind):
            raise SensitiveWorkflowError("default_workflow_outcome_kind must be WorkflowOutcomeKind")
        if any(not isinstance(kind, PromotedArtifactKind) for kind in self.permitted_promotion_kinds):
            raise SensitiveWorkflowError("permitted_promotion_kinds must be PromotedArtifactKind values")
        if self.domain_kind in _HIGH_STAKES_DOMAINS and self.min_rigor_level is RigorLevel.JUST_TALK:
            raise SensitiveWorkflowError(f"{self.domain_kind.value} cannot use min_rigor_level=just_talk")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"SensitiveDomainRequirementPolicy(domain_kind={self.domain_kind!r}, requires_jurisdiction={self.requires_jurisdiction!r}, requires_tax_year={self.requires_tax_year!r})"


def load_sensitive_domain_policies() -> Mapping[SensitiveDomainKind, SensitiveDomainRequirementPolicy]:
    """Return the policy catalog from a double-checked read-mostly cache.

    Returns:
        Resolved sensitive domain policies value.
    """
    if _POLICY_CATALOG_CACHE:
        return MappingProxyType(dict(_POLICY_CATALOG_CACHE))
    with _POLICY_CATALOG_LOCK:
        if not _POLICY_CATALOG_CACHE:
            _POLICY_CATALOG_CACHE.update(_load_sensitive_domain_policies_uncached())
        return MappingProxyType(dict(_POLICY_CATALOG_CACHE))


def _load_sensitive_domain_policies_uncached() -> dict[SensitiveDomainKind, SensitiveDomainRequirementPolicy]:
    try:
        raw = yaml.safe_load(_POLICY_CATALOG_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SensitiveWorkflowError("sensitive workflow policy catalog unavailable") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise SensitiveWorkflowError("sensitive workflow policy catalog schema_version must be 1")
    rows = raw.get("domains")
    if not isinstance(rows, list):
        raise SensitiveWorkflowError("sensitive workflow policy catalog missing domains list")
    policies: dict[SensitiveDomainKind, SensitiveDomainRequirementPolicy] = {}
    for row in rows:
        policy = _parse_policy_row(row)
        if policy.domain_kind in policies:
            raise SensitiveWorkflowError(f"duplicate sensitive domain policy: {policy.domain_kind.value}")
        policies[policy.domain_kind] = policy
    if set(policies) != set(SensitiveDomainKind):
        missing = sorted(member.value for member in set(SensitiveDomainKind) - set(policies))
        extra = sorted(str(member) for member in set(policies) - set(SensitiveDomainKind))
        raise SensitiveWorkflowError(f"sensitive domain policy coverage mismatch missing={missing} extra={extra}")
    return policies


def _parse_policy_row(row: object) -> SensitiveDomainRequirementPolicy:
    if not isinstance(row, dict):
        raise SensitiveWorkflowError("sensitive domain policy row must be a mapping")
    required = {
        "domain_kind",
        "requires_jurisdiction",
        "requires_tax_year",
        "requires_authority",
        "requires_evidence",
        "min_rigor_level",
        "default_workflow_outcome_kind",
        "permitted_promotion_kinds",
    }
    missing = required - set(row)
    if missing:
        raise SensitiveWorkflowError(f"sensitive domain policy row missing keys: {sorted(missing)}")
    try:
        return SensitiveDomainRequirementPolicy(
            domain_kind=SensitiveDomainKind(str(row["domain_kind"])),
            requires_jurisdiction=_bool(row["requires_jurisdiction"]),
            requires_tax_year=_bool(row["requires_tax_year"]),
            requires_authority=_bool(row["requires_authority"]),
            requires_evidence=_bool(row["requires_evidence"]),
            min_rigor_level=RigorLevel(str(row["min_rigor_level"])),
            default_workflow_outcome_kind=WorkflowOutcomeKind(str(row["default_workflow_outcome_kind"])),
            permitted_promotion_kinds=tuple(
                PromotedArtifactKind(str(kind)) for kind in _list(row["permitted_promotion_kinds"])
            ),
        )
    except ValueError as exc:
        raise SensitiveWorkflowError("sensitive domain policy row has unknown enum value") from exc


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise SensitiveWorkflowError("policy boolean field must be boolean")
    return value


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise SensitiveWorkflowError("policy list field must be a list")
    return value


__all__ = [
    "SensitiveDomainRequirementPolicy",
    "load_sensitive_domain_policies",
]
