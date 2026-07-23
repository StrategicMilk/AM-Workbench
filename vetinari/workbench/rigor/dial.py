"""Mode-aware rigor policies for the Workbench seriousness dial."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.constants import PROJECT_ROOT

DEFAULT_SERIOUSNESS_DIAL_PATH = PROJECT_ROOT / "config" / "workbench" / "seriousness_dial.yaml"


class RigorPolicyError(ValueError):
    """Raised when rigor policy is unavailable or unsafe."""


class RigorLevel(str, Enum):
    """User-facing rigor gradient."""

    JUST_TALK = "just_talk"
    HELP_ME_THINK = "help_me_think"
    MAKE_SOMETHING = "make_something"
    CHECK_IT_CAREFULLY = "check_it_carefully"
    MAKE_IT_REUSABLE = "make_it_reusable"


@dataclass(frozen=True, slots=True)
class RigorPolicy:
    """Runtime policy selected by mode and seriousness level."""

    level: RigorLevel
    label: str
    mode: str
    clarification_pressure: int
    citation_required: bool
    memory_policy: str
    tool_approval: str
    artifact_creation: str
    evidence_visibility: str
    reversible_to: tuple[RigorLevel, ...]
    authority_ref: str
    provenance_ref: str
    persisted_state_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.level, RigorLevel):
            raise RigorPolicyError("level must be RigorLevel")
        for field_name in (
            "label",
            "mode",
            "memory_policy",
            "tool_approval",
            "artifact_creation",
            "evidence_visibility",
            "authority_ref",
            "provenance_ref",
            "persisted_state_ref",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.clarification_pressure < 0 or self.clarification_pressure > 4:
            raise RigorPolicyError("clarification_pressure must be between 0 and 4")
        if any(not isinstance(level, RigorLevel) for level in self.reversible_to):
            raise RigorPolicyError("reversible_to must contain RigorLevel values")

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "label": self.label,
            "mode": self.mode,
            "clarification_pressure": self.clarification_pressure,
            "citation_required": self.citation_required,
            "memory_policy": self.memory_policy,
            "tool_approval": self.tool_approval,
            "artifact_creation": self.artifact_creation,
            "evidence_visibility": self.evidence_visibility,
            "reversible_to": [level.value for level in self.reversible_to],
            "authority_ref": self.authority_ref,
            "provenance_ref": self.provenance_ref,
            "persisted_state_ref": self.persisted_state_ref,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RigorPolicy(level={self.level!r}, label={self.label!r}, mode={self.mode!r})"


def load_rigor_policies(path: Path | str = DEFAULT_SERIOUSNESS_DIAL_PATH) -> tuple[RigorPolicy, ...]:
    """Load mode-aware rigor policies from YAML.

    Returns:
        Resolved rigor policies value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    policy_path = Path(path)
    if not policy_path.exists():
        raise RigorPolicyError(f"seriousness dial config not found: {policy_path}")
    raw = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("policies"), list):
        raise RigorPolicyError("seriousness dial config must contain policies")
    policies = tuple(_policy_from_mapping(item) for item in raw["policies"])
    levels = {policy.level for policy in policies}
    if levels != set(RigorLevel):
        missing = sorted(level.value for level in set(RigorLevel) - levels)
        raise RigorPolicyError(f"rigor level coverage mismatch missing={missing}")
    return policies


def apply_rigor_level(
    *,
    level: RigorLevel | str,
    mode: str,
    policies: tuple[RigorPolicy, ...] | None = None,
) -> RigorPolicy:
    """Select a reversible rigor policy for the active mode.

    Returns:
        RigorPolicy value produced by apply_rigor_level().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    selected = RigorLevel(level)
    _require_text(mode, "mode")
    candidates = policies if policies is not None else load_rigor_policies()
    for policy in candidates:
        if policy.level == selected and (policy.mode == mode or policy.mode == "default"):
            return policy
    raise RigorPolicyError(f"no rigor policy for level={selected.value!r} mode={mode!r}")


def _policy_from_mapping(raw: object) -> RigorPolicy:
    if not isinstance(raw, dict):
        raise RigorPolicyError("policy row must be a mapping")
    return RigorPolicy(
        level=RigorLevel(str(raw.get("level", ""))),
        label=str(raw.get("label", "")),
        mode=str(raw.get("mode", "")),
        clarification_pressure=int(raw.get("clarification_pressure", -1)),
        citation_required=bool(raw.get("citation_required", False)),
        memory_policy=str(raw.get("memory_policy", "")),
        tool_approval=str(raw.get("tool_approval", "")),
        artifact_creation=str(raw.get("artifact_creation", "")),
        evidence_visibility=str(raw.get("evidence_visibility", "")),
        reversible_to=tuple(RigorLevel(str(item)) for item in raw.get("reversible_to", ())),
        authority_ref=str(raw.get("authority_ref", "")),
        provenance_ref=str(raw.get("provenance_ref", "")),
        persisted_state_ref=str(raw.get("persisted_state_ref", "")),
    )


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RigorPolicyError(f"{field_name} must be non-empty")


__all__ = [
    "DEFAULT_SERIOUSNESS_DIAL_PATH",
    "RigorLevel",
    "RigorPolicy",
    "RigorPolicyError",
    "apply_rigor_level",
    "load_rigor_policies",
]
