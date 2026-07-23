"""Typed context asset packs for prompt and routing context selection."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ContextAssetValidationError(ValueError):
    """Raised when a context asset pack cannot be trusted."""


class ContextAssetKind(str, Enum):
    """Context surfaces that can be promoted into inspectable packs."""

    ACCELERATOR = "accelerator"
    WIKI_PAGE = "wiki_page"
    CONTEXT_BUNDLE = "context_bundle"
    CODE_MAP = "code_map"
    RETRIEVAL_COLLECTION = "retrieval_collection"
    DOMAIN_CONTEXT = "domain_context"
    EVIDENCE_ASSET = "evidence_asset"
    MEMORY_CONTEXT = "memory_context"


class FreshnessState(str, Enum):
    """Fail-closed freshness state for a context asset pack."""

    FRESH = "fresh"
    AGING = "aging"
    STALE = "stale"
    UNKNOWN = "unknown"


class PromptSafetyStatus(str, Enum):
    """Prompt-safety verdict for context rendered into model input."""

    SAFE = "safe"
    UNTRUSTED_QUOTED = "untrusted_quoted"
    UNSAFE_BLOCKED = "unsafe_blocked"


@dataclass(frozen=True, slots=True)
class ContextAssetSource:
    """One source covered by a context asset pack."""

    source_id: str
    source_kind: str
    coverage_ratio: float
    observed_at_utc: str
    max_age_seconds: int = 2_592_000
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty(self.source_id, "source_id")
        _require_non_empty(self.source_kind, "source_kind")
        _require_non_empty(self.observed_at_utc, "observed_at_utc")
        _require_ratio(self.coverage_ratio, "coverage_ratio")
        if self.max_age_seconds < 0:
            raise ContextAssetValidationError("max_age_seconds must be non-negative")
        _require_pairs(self.metadata, "metadata", allow_empty=True)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ContextAssetSource(source_id={self.source_id!r}, source_kind={self.source_kind!r}, coverage_ratio={self.coverage_ratio!r})"


@dataclass(frozen=True, slots=True)
class ContradictionRecord:
    """Known contradiction that should penalize context selection."""

    contradiction_id: str
    summary: str
    severity: str
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_non_empty(self.contradiction_id, "contradiction_id")
        _require_non_empty(self.summary, "summary")
        _require_non_empty(self.severity, "severity")
        _require_non_empty_tuple(self.evidence_refs, "evidence_refs")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ContradictionRecord(contradiction_id={self.contradiction_id!r}, summary={self.summary!r}, severity={self.severity!r})"


@dataclass(frozen=True, slots=True)
class InvalidationTrigger:
    """Condition that makes a context asset stale until rebuilt."""

    trigger_id: str
    description: str
    source_id: str
    triggered_at_utc: str = ""

    def __post_init__(self) -> None:
        _require_non_empty(self.trigger_id, "trigger_id")
        _require_non_empty(self.description, "description")
        _require_non_empty(self.source_id, "source_id")

    @property
    def is_active(self) -> bool:
        """Return true when the trigger has observed invalidation evidence."""
        return bool(self.triggered_at_utc.strip())

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"InvalidationTrigger(trigger_id={self.trigger_id!r}, description={self.description!r}, source_id={self.source_id!r})"


@dataclass(frozen=True, slots=True)
class ContextAssetPack:
    """Inspectable, scoreable prompt context unit."""

    context_asset_id: str
    kind: ContextAssetKind
    title: str
    revision: str
    observed_at_utc: str
    source_coverage: tuple[ContextAssetSource, ...]
    freshness: FreshnessState
    contradiction_ledger: tuple[ContradictionRecord, ...]
    provenance: tuple[tuple[str, str], ...]
    intended_agent_profiles: tuple[str, ...]
    token_budget: int
    usefulness_score: float
    invalidation_triggers: tuple[InvalidationTrigger, ...]
    upstream_evidence_refs: tuple[str, ...] = ()
    content_summary: str = ""
    prompt_safety_status: PromptSafetyStatus = PromptSafetyStatus.SAFE
    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_non_empty(self.context_asset_id, "context_asset_id")
        _require_non_empty(self.title, "title")
        _require_non_empty(self.revision, "revision")
        _require_non_empty(self.observed_at_utc, "observed_at_utc")
        object.__setattr__(self, "kind", _coerce_enum(ContextAssetKind, self.kind, "kind"))
        object.__setattr__(self, "freshness", _coerce_enum(FreshnessState, self.freshness, "freshness"))
        _require_non_empty_tuple(self.source_coverage, "source_coverage")
        _require_pairs(self.provenance, "provenance")
        if not dict(self.provenance).get("source", "").strip():
            raise ContextAssetValidationError("provenance.source must be non-empty")
        _require_non_empty_tuple(self.intended_agent_profiles, "intended_agent_profiles")
        _require_non_empty_tuple(self.invalidation_triggers, "invalidation_triggers")
        if self.token_budget <= 0:
            raise ContextAssetValidationError("token_budget must be positive")
        _require_ratio(self.usefulness_score, "usefulness_score")
        object.__setattr__(
            self,
            "prompt_safety_status",
            _coerce_enum(PromptSafetyStatus, self.prompt_safety_status, "prompt_safety_status"),
        )
        _require_pairs(self.metadata, "metadata", allow_empty=True)
        for record in self.contradiction_ledger:
            if not isinstance(record, ContradictionRecord):
                raise ContextAssetValidationError("contradiction_ledger must contain ContradictionRecord values")

    def to_payload(self) -> dict[str, object]:
        """Return schema-shaped JSON data for the pack."""
        return {
            "schema_version": 1,
            "context_asset_id": self.context_asset_id,
            "kind": self.kind.value,
            "title": self.title,
            "revision": self.revision,
            "observed_at_utc": self.observed_at_utc,
            "source_coverage": [
                {
                    "source_id": source.source_id,
                    "source_kind": source.source_kind,
                    "coverage_ratio": source.coverage_ratio,
                    "observed_at_utc": source.observed_at_utc,
                    "max_age_seconds": source.max_age_seconds,
                    "metadata": dict(source.metadata),
                }
                for source in self.source_coverage
            ],
            "freshness": self.freshness.value,
            "contradiction_ledger": [
                {
                    "contradiction_id": record.contradiction_id,
                    "summary": record.summary,
                    "severity": record.severity,
                    "evidence_refs": list(record.evidence_refs),
                }
                for record in self.contradiction_ledger
            ],
            "provenance": dict(self.provenance),
            "intended_agent_profiles": list(self.intended_agent_profiles),
            "token_budget": self.token_budget,
            "usefulness_score": self.usefulness_score,
            "invalidation_triggers": [
                {
                    "trigger_id": trigger.trigger_id,
                    "description": trigger.description,
                    "source_id": trigger.source_id,
                    "triggered_at_utc": trigger.triggered_at_utc,
                    "active": trigger.is_active,
                }
                for trigger in self.invalidation_triggers
            ],
            "upstream_evidence_refs": list(self.upstream_evidence_refs),
            "content_summary": self.content_summary,
            "prompt_safety_status": self.prompt_safety_status.value,
            "metadata": dict(self.metadata),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"ContextAssetPack(context_asset_id={self.context_asset_id!r}, kind={self.kind!r}, title={self.title!r})"


def _require_non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ContextAssetValidationError(f"{field_name} must be non-empty")


def _require_non_empty_tuple(values: tuple[object, ...], field_name: str) -> None:
    if not isinstance(values, tuple) or not values:
        raise ContextAssetValidationError(f"{field_name} must contain at least one value")
    if all(isinstance(value, str) for value in values) and not all(value.strip() for value in values):
        raise ContextAssetValidationError(f"{field_name} must contain non-empty strings")


def _require_pairs(values: tuple[tuple[str, str], ...], field_name: str, *, allow_empty: bool = False) -> None:
    if not isinstance(values, tuple):
        raise ContextAssetValidationError(f"{field_name} must be a tuple")
    if not values and not allow_empty:
        raise ContextAssetValidationError(f"{field_name} must be non-empty")
    for key, value in values:
        _require_non_empty(key, f"{field_name} key")
        _require_non_empty(value, f"{field_name} value")


def _require_ratio(value: float, field_name: str) -> None:
    if not isinstance(value, (float, int)) or not 0.0 <= float(value) <= 1.0:
        raise ContextAssetValidationError(f"{field_name} must be between 0.0 and 1.0")


def _coerce_enum(enum_type: type[Enum], value: Any, field_name: str) -> Enum:
    raw_value = value.value if isinstance(value, Enum) else value
    try:
        return value if isinstance(value, enum_type) else enum_type(raw_value)
    except ValueError as exc:
        raise ContextAssetValidationError(f"{field_name} must be {enum_type.__name__}") from exc
