"""Typed workbench asset cards consumed by the metadata spine.

Asset cards describe prompts, models, datasets, adapters, eval suites,
and tools. They are read-only value objects and perform no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from vetinari.utils.serialization import dataclass_to_dict


def _require_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


class AssetKind(str, Enum):
    """Kinds of assets tracked by the workbench spine."""

    PROMPT = "prompt"
    MODEL = "model"
    DATASET = "dataset"
    ADAPTER = "adapter"
    EVAL_SUITE = "eval_suite"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class AssetTaint:
    """A warning or blocker attached to an asset revision."""

    taint_id: str
    severity: str
    reason: str
    attached_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.taint_id, "taint_id")
        _require_non_empty(self.severity, "severity")
        _require_non_empty(self.reason, "reason")
        _require_non_empty(self.attached_at_utc, "attached_at_utc")
        if self.severity not in {"info", "warning", "blocker"}:
            raise ValueError("severity must be one of: info, warning, blocker")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AssetTaint(taint_id={self.taint_id!r}, severity={self.severity!r}, reason={self.reason!r})"


@dataclass(frozen=True, slots=True)
class WorkbenchAsset:
    """A revision-pinned asset card."""

    asset_id: str
    kind: AssetKind
    name: str
    revision: str
    created_at_utc: str
    taints: tuple[AssetTaint, ...] = ()
    provenance: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.asset_id, "asset_id")
        _require_non_empty(self.name, "name")
        _require_non_empty(self.revision, "revision")
        _require_non_empty(self.created_at_utc, "created_at_utc")
        if not self.provenance.get("source", "").strip():
            raise ValueError("provenance.source must be non-empty")
        for taint in self.taints:
            if not isinstance(taint, AssetTaint):
                raise ValueError("taints must contain AssetTaint instances")

    def __repr__(self) -> str:
        return (
            f"WorkbenchAsset(asset_id={self.asset_id!r}, "
            f"kind={self.kind.value!r}, revision={self.revision!r}, "
            f"taints={len(self.taints)})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the operator-console JSON contract for this asset."""
        return dataclass_to_dict(self)


class AssetCardKind(str, Enum):
    """How a binding-layer AssetCard relates to its WorkbenchAsset."""

    EVIDENCE = "evidence"
    SUMMARY = "summary"
    REFERENCE = "reference"
    DERIVED = "derived"


@dataclass(frozen=True, slots=True)
class AssetCard:
    """Binding-layer card for a stored WorkbenchAsset."""

    card_id: str
    asset_id: str
    card_kind: AssetCardKind
    summary: str
    revision: str
    evidence_links: tuple[str, ...]
    provenance: dict[str, str]
    created_at_utc: str

    def __post_init__(self) -> None:
        _require_non_empty(self.card_id, "card_id")
        _require_non_empty(self.asset_id, "asset_id")
        _require_non_empty(self.revision, "revision")
        _require_non_empty(self.created_at_utc, "created_at_utc")
        if not isinstance(self.card_kind, AssetCardKind):
            raise ValueError(f"card_kind must be AssetCardKind (got {type(self.card_kind).__name__})")
        if not isinstance(self.evidence_links, tuple):
            raise ValueError("evidence_links must be a tuple")
        if not all(isinstance(link, str) for link in self.evidence_links):
            raise ValueError("evidence_links must contain strings")
        if not isinstance(self.provenance, dict):
            raise ValueError("provenance must be a dict[str, str]")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in self.provenance.items()):
            raise ValueError("provenance must contain string keys and values")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"AssetCard(card_id={self.card_id!r}, asset_id={self.asset_id!r}, card_kind={self.card_kind!r})"


__all__ = ["AssetCard", "AssetCardKind", "AssetKind", "AssetTaint", "WorkbenchAsset"]
