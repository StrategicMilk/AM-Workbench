"""Surface-aware Workbench model quick-choice contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType


class Surface(str, Enum):
    """Workbench surfaces that can request curated model choices."""

    CHAT = "chat"
    VISION = "vision"
    IMAGE = "image"
    VIDEO = "video"
    EMBEDDING = "embedding"
    BATCH = "batch"
    ROUTE = "route"
    SPECIALIST_AGENT = "specialist_agent"


class InactiveReason(str, Enum):
    """Typed fail-closed reasons a quick-choice row is unavailable."""

    MISSING_CAPABILITY = "missing_capability"
    STAGE_NOT_SERVING = "stage_not_serving"
    DEPRECATED = "deprecated"
    PROVIDER_DISABLED = "provider_disabled"
    CAPABILITY_SNAPSHOT_UNAVAILABLE = "capability_snapshot_unavailable"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    MEDIA_NOT_ALLOWED_ON_CHAT = "media_not_allowed_on_chat"


@dataclass(frozen=True, slots=True)
class ProviderQualifiedModelRef:
    """A model identifier qualified by provider to avoid ID collisions."""

    provider: str
    model_id: str

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("provider must be non-empty")
        if not self.model_id.strip():
            raise ValueError("model_id must be non-empty")

    @property
    def qualified_id(self) -> str:
        """Return the stable provider-qualified identifier."""
        return f"{self.provider}::{self.model_id}"

    @classmethod
    def from_qualified_id(cls, qualified_id: str) -> ProviderQualifiedModelRef:
        """Parse ``provider::model_id`` into a typed reference.

        Returns:
            ProviderQualifiedModelRef value produced by from_qualified_id().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        provider, separator, model_id = qualified_id.partition("::")
        if not separator:
            raise ValueError("qualified_id must use provider::model_id format")
        return cls(provider=provider, model_id=model_id)


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Value snapshot of registry capability inputs for one model version."""

    capabilities: tuple[str, ...]
    provider: str
    stage: str
    deprecation_state: str
    card_id: str
    version_id: str
    captured_at_utc: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", tuple(str(item) for item in self.capabilities))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"CapabilitySnapshot(capabilities={self.capabilities!r}, provider={self.provider!r}, stage={self.stage!r})"
        )


@dataclass(frozen=True, slots=True)
class ModelQuickChoice:
    """One model row exposed to a Workbench surface."""

    surface: Surface
    display_name: str
    model_ref: ProviderQualifiedModelRef
    capability_snapshot: CapabilitySnapshot | None
    is_active: bool
    inactive_reasons: tuple[InactiveReason, ...]
    evidence_refs: tuple[str, ...]
    pinned_version_id: str | None = None

    def __post_init__(self) -> None:
        coerced_reasons = tuple(
            reason if isinstance(reason, InactiveReason) else InactiveReason(str(reason))
            for reason in self.inactive_reasons
        )
        object.__setattr__(self, "inactive_reasons", coerced_reasons)
        object.__setattr__(self, "evidence_refs", tuple(str(item) for item in self.evidence_refs))
        if self.is_active and coerced_reasons:
            raise ValueError("active quick choices cannot carry inactive reasons")

    @property
    def qualified_id(self) -> str:
        """Return the row key used by UI and API consumers."""
        return self.model_ref.qualified_id

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation.

        Returns:
            dict[str, object] value produced by to_dict().
        """
        snapshot = None
        if self.capability_snapshot is not None:
            snapshot = {
                "capabilities": list(self.capability_snapshot.capabilities),
                "provider": self.capability_snapshot.provider,
                "stage": self.capability_snapshot.stage,
                "deprecation_state": self.capability_snapshot.deprecation_state,
                "card_id": self.capability_snapshot.card_id,
                "version_id": self.capability_snapshot.version_id,
                "captured_at_utc": self.capability_snapshot.captured_at_utc,
            }
        return {
            "surface": self.surface.value,
            "display_name": self.display_name,
            "model_ref": {
                "provider": self.model_ref.provider,
                "model_id": self.model_ref.model_id,
                "qualified_id": self.model_ref.qualified_id,
            },
            "capability_snapshot": snapshot,
            "is_active": self.is_active,
            "inactive_reasons": [reason.value for reason in self.inactive_reasons],
            "evidence_refs": list(self.evidence_refs),
            "pinned_version_id": self.pinned_version_id,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ModelQuickChoice(surface={self.surface!r}, display_name={self.display_name!r}, model_ref={self.model_ref!r})"


@dataclass(frozen=True, slots=True)
class QuickChoiceCatalog:
    """A surface-specific quick-choice catalog."""

    surface: Surface
    choices: tuple[ModelQuickChoice, ...]
    generated_at_utc: str

    def choices_by_qualified_id(self) -> MappingProxyType[str, ModelQuickChoice]:
        """Return choices keyed by provider-qualified model ID."""
        return MappingProxyType({choice.model_ref.qualified_id: choice for choice in self.choices})

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serializable representation."""
        return {
            "surface": self.surface.value,
            "surfaces": [surface.value for surface in Surface],
            "choices": [choice.to_dict() for choice in self.choices],
            "generated_at_utc": self.generated_at_utc,
        }


__all__ = [
    "CapabilitySnapshot",
    "InactiveReason",
    "ModelQuickChoice",
    "ProviderQualifiedModelRef",
    "QuickChoiceCatalog",
    "Surface",
]
