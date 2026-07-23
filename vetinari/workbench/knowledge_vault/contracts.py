"""Immutable contracts for deterministic Workbench Knowledge Vault exports."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from vetinari.memory.governance import BoundaryClass, MemoryAuthority, MemoryLifecycleState
from vetinari.workbench.knowledge_graph import ProvenanceRef


class KnowledgeVaultError(ValueError):
    """Raised when a vault contract cannot be trusted."""


class VaultEntryKind(str, Enum):
    """Runtime contract for VaultEntryKind."""

    MEMORY = "memory"
    PROJECT_KNOWLEDGE = "project_knowledge"
    DECISION = "decision"
    ARTIFACT = "artifact"
    GRAPH_NODE = "graph_node"


@dataclass(frozen=True, slots=True)
class RejectedVaultEntry:
    """Runtime contract for RejectedVaultEntry."""

    entry_id: str
    slug: str
    title: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        _non_empty(self.entry_id, "entry_id")
        _non_empty(self.slug, "slug")
        _non_empty(self.title, "title")
        object.__setattr__(self, "reasons", _strings(self.reasons, "reasons"))

    def to_dict(self) -> dict[str, Any]:
        return {"entry_id": self.entry_id, "slug": self.slug, "title": self.title, "reasons": list(self.reasons)}

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RejectedVaultEntry(entry_id={self.entry_id!r}, slug={self.slug!r}, title={self.title!r})"


@dataclass(frozen=True, slots=True)
class VaultEntry:
    """Runtime contract for VaultEntry."""

    entry_id: str
    kind: VaultEntryKind
    title: str
    slug: str
    frontmatter: Mapping[str, Any]
    wiki_links: tuple[str, ...]
    provenance_refs: tuple[ProvenanceRef, ...]
    confidence: float
    authority: MemoryAuthority
    lifecycle_state: MemoryLifecycleState | str
    boundary_class: BoundaryClass
    source_links: tuple[str, ...]
    content_hash: str = ""

    def __post_init__(self) -> None:
        for name in ("entry_id", "title", "slug"):
            _non_empty(getattr(self, name), name)
        object.__setattr__(self, "kind", VaultEntryKind(self.kind))
        object.__setattr__(self, "frontmatter", dict(self.frontmatter))
        object.__setattr__(self, "wiki_links", _strings(self.wiki_links, "wiki_links", allow_empty=True))
        refs = tuple(self.provenance_refs)
        if not refs or not all(isinstance(ref, ProvenanceRef) for ref in refs):
            raise KnowledgeVaultError("provenance_refs must contain ProvenanceRef values")
        object.__setattr__(self, "provenance_refs", refs)
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise KnowledgeVaultError("confidence must be between 0.0 and 1.0")
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "authority", MemoryAuthority(self.authority))
        object.__setattr__(self, "boundary_class", BoundaryClass(self.boundary_class))
        object.__setattr__(self, "source_links", _strings(self.source_links, "source_links"))
        if not isinstance(self.lifecycle_state, MemoryLifecycleState):
            value = str(self.lifecycle_state)
            try:
                object.__setattr__(self, "lifecycle_state", MemoryLifecycleState(value))
            except ValueError:
                if value != "forgotten":
                    raise KnowledgeVaultError(f"unknown lifecycle state: {value!r}") from None
        if self.content_hash and (
            len(self.content_hash) != 64 or any(char not in "0123456789abcdef" for char in self.content_hash)
        ):
            raise KnowledgeVaultError("content_hash must be lowercase SHA-256")

    def to_dict(self) -> dict[str, Any]:
        """Execute the to dict operation.

        Returns:
            dict[str, Any] value produced by to_dict().
        """
        lifecycle = (
            self.lifecycle_state.value
            if isinstance(self.lifecycle_state, MemoryLifecycleState)
            else self.lifecycle_state
        )
        return {
            "entry_id": self.entry_id,
            "kind": self.kind.value,
            "slug": self.slug,
            "title": self.title,
            "frontmatter": dict(self.frontmatter),
            "wiki_links": list(self.wiki_links),
            "provenance_refs": [
                {"ref_id": ref.ref_id, "ref_type": ref.ref_type, "evidence": ref.evidence}
                for ref in self.provenance_refs
            ],
            "confidence": self.confidence,
            "authority": self.authority.value,
            "lifecycle_state": lifecycle,
            "boundary_class": self.boundary_class.value,
            "source_links": list(self.source_links),
            "content_hash": self.content_hash,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"VaultEntry(entry_id={self.entry_id!r}, kind={self.kind!r}, title={self.title!r})"


@dataclass(frozen=True, slots=True)
class VaultRebuildPlan:
    """Runtime contract for VaultRebuildPlan."""

    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, list[str]]:
        return {name: list(getattr(self, name)) for name in ("created", "updated", "removed", "unchanged")}

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"VaultRebuildPlan(created={self.created!r}, updated={self.updated!r}, removed={self.removed!r})"


@dataclass(frozen=True, slots=True)
class VaultManifest:
    """Runtime contract for VaultManifest."""

    entries: tuple[VaultEntry, ...]
    rejected: tuple[RejectedVaultEntry, ...] = ()
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    manifest_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize this manifest for API and storage payloads.

        Returns:
            JSON-compatible manifest payload.
        """
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "rejected": [entry.to_dict() for entry in self.rejected],
            "created": list(self.created),
            "updated": list(self.updated),
            "removed": list(self.removed),
            "unchanged": list(self.unchanged),
            "manifest_hash": self.manifest_hash,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"VaultManifest(entries={self.entries!r}, rejected={self.rejected!r}, created={self.created!r})"


@dataclass(frozen=True, slots=True)
class VaultIndex:
    """Runtime contract for VaultIndex."""

    entries: tuple[VaultEntry, ...]
    rejected: tuple[RejectedVaultEntry, ...] = ()
    generated_path: Path = Path("INDEX.md")
    body: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "rejected": [entry.to_dict() for entry in self.rejected],
            "generated_path": str(self.generated_path),
            "body": self.body,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"VaultIndex(entries={self.entries!r}, rejected={self.rejected!r}, generated_path={self.generated_path!r})"
        )


@dataclass(frozen=True, slots=True)
class VaultConfig:
    """Runtime contract for VaultConfig."""

    vault_root: Path = Path("outputs") / "workbench" / "spine" / "knowledge_vault"
    index_file: str = "INDEX.md"
    refinement_journal_path: Path = Path("outputs") / "workbench" / "spine" / "memory_refinement" / "journal.jsonl"
    default_export_scope: str = "shareable"
    allowed_export_scopes: Mapping[str, bool] = field(
        default_factory=lambda: {"shareable": True, "private": True, "sensitive": False}
    )
    confidence_decay: Mapping[str, float] = field(default_factory=lambda: {"half_life_days": 30.0, "floor": 0.1})
    quiet_window: Mapping[str, int] = field(default_factory=lambda: {"start_hour": 1, "end_hour": 5})
    resource_busy_thresholds: Mapping[str, float] = field(default_factory=lambda: {"cpu_pct": 80.0, "gpu_pct": 85.0})
    wiki_link_style: str = "obsidian-double-bracket"

    def __post_init__(self) -> None:
        object.__setattr__(self, "vault_root", Path(self.vault_root))
        object.__setattr__(self, "refinement_journal_path", Path(self.refinement_journal_path))
        object.__setattr__(self, "allowed_export_scopes", dict(self.allowed_export_scopes))
        object.__setattr__(self, "confidence_decay", dict(self.confidence_decay))

    @property
    def confidence_floor(self) -> float:
        return float(self.confidence_decay.get("floor", 0.1))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"VaultConfig(vault_root={self.vault_root!r}, index_file={self.index_file!r}, refinement_journal_path={self.refinement_journal_path!r})"


@dataclass(frozen=True, slots=True)
class VaultEntryCandidate:
    """Runtime contract for VaultEntryCandidate."""

    entry_id: str
    kind: VaultEntryKind
    title: str
    slug: str
    frontmatter: Mapping[str, Any]
    wiki_links: tuple[str, ...]
    provenance_refs: tuple[ProvenanceRef, ...]
    confidence: float | None
    authority: MemoryAuthority
    lifecycle_state: MemoryLifecycleState | str
    boundary_class: BoundaryClass | None
    source_links: tuple[str, ...]
    retention_class: Any = None
    sensitivity_tags: tuple[str, ...] = ()

    def to_rejected(self, reasons: Sequence[str]) -> RejectedVaultEntry:
        return RejectedVaultEntry(
            self.entry_id or "unknown-entry", self.slug or "unknown", self.title or "Untitled", tuple(reasons)
        )

    def to_entry(self, *, content_hash: str = "") -> VaultEntry:
        """Execute the to entry operation.

        Returns:
            VaultEntry value produced by to_entry().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if self.confidence is None:
            raise KnowledgeVaultError("candidate confidence is required")
        if self.boundary_class is None:
            raise KnowledgeVaultError("candidate boundary_class is required")
        return VaultEntry(
            entry_id=self.entry_id,
            kind=self.kind,
            title=self.title,
            slug=self.slug,
            frontmatter=self.frontmatter,
            wiki_links=self.wiki_links,
            provenance_refs=self.provenance_refs,
            confidence=self.confidence,
            authority=self.authority,
            lifecycle_state=self.lifecycle_state,
            boundary_class=self.boundary_class,
            source_links=self.source_links,
            content_hash=content_hash,
        )

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"VaultEntryCandidate(entry_id={self.entry_id!r}, kind={self.kind!r}, title={self.title!r})"


class VaultSchemaValidator:
    """Runtime contract for VaultSchemaValidator."""

    def __init__(self, schema_path: Path = Path("schemas/workbench_knowledge_vault.schema.json")) -> None:
        self.schema_path = schema_path
        self._validator: Any | None = None

    def validate(self, payload: Mapping[str, Any]) -> None:
        """Execute the validate operation."""
        if self._validator is None:
            import json

            import jsonschema

            self._validator = jsonschema.Draft202012Validator(json.loads(self.schema_path.read_text(encoding="utf-8")))
        self._validator.validate(dict(payload))


def compute_decayed_confidence(
    observed_confidence: float, age_days: float, half_life_days: float, floor: float
) -> float:
    """Execute the compute decayed confidence operation.

    Args:
        observed_confidence: Observed confidence value consumed by compute_decayed_confidence().
        age_days: Age days value consumed by compute_decayed_confidence().
        half_life_days: Half life days value consumed by compute_decayed_confidence().
        floor: Floor value consumed by compute_decayed_confidence().

    Returns:
        Computed decayed confidence result.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    observed = float(observed_confidence)
    floor_value = float(floor)
    if half_life_days <= 0 or age_days < 0:
        raise KnowledgeVaultError("age and half-life must be valid")
    if not 0.0 <= observed <= 1.0 or not 0.0 <= floor_value <= 1.0:
        raise KnowledgeVaultError("confidence values must be between 0.0 and 1.0")
    return max(floor_value, min(observed, observed * math.pow(0.5, age_days / half_life_days)))


def _strings(values: Sequence[Any], field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    result = tuple(str(value) for value in values if str(value).strip())
    if not allow_empty and not result:
        raise KnowledgeVaultError(f"{field_name} must contain a non-empty value")
    return result


def _non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeVaultError(f"{field_name} must be non-empty")


__all__ = [
    "KnowledgeVaultError",
    "RejectedVaultEntry",
    "VaultConfig",
    "VaultEntry",
    "VaultEntryCandidate",
    "VaultEntryKind",
    "VaultIndex",
    "VaultManifest",
    "VaultRebuildPlan",
    "VaultSchemaValidator",
    "compute_decayed_confidence",
]
