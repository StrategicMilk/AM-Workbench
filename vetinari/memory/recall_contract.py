"""Deterministic memory recall contract for prompt eligibility."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1


class RecallContractError(ValueError):
    """Raised when a recall profile or payload is malformed."""


class RecallStatus(str, Enum):
    """Pack-level recall states."""

    PACKED = "packed"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"


class AuthorityTier(str, Enum):
    """Prompt authority tier carried by a recalled memory."""

    NONE = "none"
    CANDIDATE = "candidate"
    MEMORY = "memory"
    PROMPT = "prompt"
    PLANNING = "planning"
    ROUTING = "routing"
    POLICY = "policy"


class TaintSignal(str, Enum):
    """Prompt-contamination state for a recalled memory."""

    CLEAN = "clean"
    TAINTED = "tainted"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RecallTokenBudget:
    """Token budget used to decide whether a memory can be packed."""

    estimated_tokens: int
    allocated_tokens: int

    def __post_init__(self) -> None:
        if self.estimated_tokens < 0:
            raise RecallContractError("estimated_tokens must be >= 0")
        if self.allocated_tokens < 0:
            raise RecallContractError("allocated_tokens must be >= 0")

    def to_dict(self) -> dict[str, int]:
        """Return a JSON-safe token-budget payload."""
        return {
            "estimated_tokens": self.estimated_tokens,
            "allocated_tokens": self.allocated_tokens,
        }


@dataclass(frozen=True, slots=True)
class RecallProfile:
    """Agent-specific deterministic memory packing policy."""

    profile_id: str
    agent_types: tuple[str, ...]
    max_items: int
    max_tokens: int
    min_relevance: float
    min_confidence: float
    authority_tiers: tuple[AuthorityTier, ...]
    scopes: tuple[str, ...]
    include_stale: bool = False
    include_tainted: bool = False
    include_superseded: bool = False

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise RecallContractError("profile_id must be non-empty")
        if not self.agent_types:
            raise RecallContractError("agent_types must contain at least one entry")
        if self.max_items < 0:
            raise RecallContractError("max_items must be >= 0")
        if self.max_tokens < 0:
            raise RecallContractError("max_tokens must be >= 0")
        if not 0.0 <= self.min_relevance <= 1.0:
            raise RecallContractError("min_relevance must be between 0 and 1")
        if not 0.0 <= self.min_confidence <= 1.0:
            raise RecallContractError("min_confidence must be between 0 and 1")
        if not self.authority_tiers:
            raise RecallContractError("authority_tiers must contain at least one entry")
        if not self.scopes:
            raise RecallContractError("scopes must contain at least one entry")

    @classmethod
    def from_mapping(cls, profile_id: str, payload: Mapping[str, Any]) -> RecallProfile:
        """Parse a profile mapping from ``config/agent_memory_profiles.yaml``.

        Args:
            profile_id: File path or file-like value consumed by the operation.
            payload: Payload data validated or transformed by the operation.

        Returns:
            RecallProfile value produced by from_mapping().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        try:
            return cls(
                profile_id=profile_id,
                agent_types=tuple(str(value).upper() for value in payload["agent_types"]),
                max_items=int(payload["max_items"]),
                max_tokens=int(payload["max_tokens"]),
                min_relevance=float(payload["min_relevance"]),
                min_confidence=float(payload["min_confidence"]),
                authority_tiers=tuple(
                    AuthorityTier(value.value if hasattr(value, "value") else value)
                    for value in payload["authority_tiers"]
                ),
                scopes=tuple(str(value) for value in payload["scopes"]),
                include_stale=bool(payload.get("include_stale", False)),
                include_tainted=bool(payload.get("include_tainted", False)),
                include_superseded=bool(payload.get("include_superseded", False)),
            )
        except KeyError as exc:
            raise RecallContractError(f"profile {profile_id!r} missing key {exc.args[0]!r}") from exc
        except (TypeError, ValueError) as exc:
            raise RecallContractError(f"profile {profile_id!r} is invalid: {exc}") from exc

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe profile payload."""
        return {
            "profile_id": self.profile_id,
            "agent_types": list(self.agent_types),
            "max_items": self.max_items,
            "max_tokens": self.max_tokens,
            "min_relevance": self.min_relevance,
            "min_confidence": self.min_confidence,
            "authority_tiers": [tier.value for tier in self.authority_tiers],
            "scopes": list(self.scopes),
            "include_stale": self.include_stale,
            "include_tainted": self.include_tainted,
            "include_superseded": self.include_superseded,
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"RecallProfile(profile_id={self.profile_id!r}, agent_types={self.agent_types!r}, max_items={self.max_items!r})"


@dataclass(frozen=True, slots=True)
class MemoryRecallItem:
    """One recalled memory with all prompt-eligibility signals explicit."""

    memory_id: str
    text: str
    relevance: float
    confidence: float
    authority_tier: AuthorityTier
    scope: str
    source: str
    age_seconds: int | None
    stale: bool
    taint: TaintSignal
    superseded_by: str | None
    token_budget: RecallTokenBudget
    prompt_eligible: bool
    blockers: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.memory_id.strip():
            raise RecallContractError("memory_id must be non-empty")
        if not 0.0 <= self.relevance <= 1.0:
            raise RecallContractError("relevance must be between 0 and 1")
        if not 0.0 <= self.confidence <= 1.0:
            raise RecallContractError("confidence must be between 0 and 1")
        if self.age_seconds is not None and self.age_seconds < 0:
            raise RecallContractError("age_seconds must be >= 0 when present")
        if self.blockers and self.prompt_eligible:
            raise RecallContractError("blocked recall items cannot be prompt eligible")

    def to_prompt_line(self) -> str:
        """Render the item as quoted, non-instructional prompt data.

        Returns:
            str value produced by to_prompt_line().
        """
        age = "unknown" if self.age_seconds is None else f"{self.age_seconds}s"
        quoted_text = self.text.replace("\\", "\\\\").replace('"', '\\"')
        return (
            f"- id={self.memory_id}; authority={self.authority_tier.value}; scope={self.scope}; "
            f"source={self.source}; relevance={self.relevance:.2f}; confidence={self.confidence:.2f}; "
            f"age={age}; stale={str(self.stale).lower()}; taint={self.taint.value}; "
            f"tokens={self.token_budget.estimated_tokens}/{self.token_budget.allocated_tokens}; "
            f'untrusted_memory_text="{quoted_text}"'
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe recall item payload."""
        return {
            "memory_id": self.memory_id,
            "text": self.text,
            "relevance": self.relevance,
            "confidence": self.confidence,
            "authority_tier": self.authority_tier.value,
            "scope": self.scope,
            "source": self.source,
            "age_seconds": self.age_seconds,
            "stale": self.stale,
            "taint": self.taint.value,
            "superseded_by": self.superseded_by,
            "token_budget": self.token_budget.to_dict(),
            "prompt_eligible": self.prompt_eligible,
            "blockers": list(self.blockers),
            "metadata": dict(self.metadata),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryRecallItem(memory_id={self.memory_id!r}, text={self.text!r}, relevance={self.relevance!r})"


@dataclass(frozen=True, slots=True)
class MemoryRecallPack:
    """Agent-specific packed memory context and audit payload."""

    agent_type: str
    task_type: str
    profile: RecallProfile
    query: str
    status: RecallStatus
    items: tuple[MemoryRecallItem, ...]
    prompt_text: str
    diagnostics: tuple[str, ...] = ()

    @property
    def eligible_items(self) -> tuple[MemoryRecallItem, ...]:
        """Return items that are allowed into prompts."""
        return tuple(item for item in self.items if item.prompt_eligible)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe recall pack payload."""
        return {
            "schema_version": SCHEMA_VERSION,
            "agent_type": self.agent_type,
            "task_type": self.task_type,
            "profile": self.profile.to_dict(),
            "query": self.query,
            "status": self.status.value,
            "eligible_count": len(self.eligible_items),
            "items": [item.to_dict() for item in self.items],
            "prompt_text": self.prompt_text,
            "diagnostics": list(self.diagnostics),
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return (
            f"MemoryRecallPack(agent_type={self.agent_type!r}, task_type={self.task_type!r}, profile={self.profile!r})"
        )


def build_unavailable_pack(
    *,
    agent_type: str,
    task_type: str,
    profile: RecallProfile,
    query: str,
    reason: str,
) -> MemoryRecallPack:
    """Return a fail-closed pack for unreadable or unavailable memory state.

    Returns:
        MemoryRecallPack with unavailable status and no prompt text.
    """
    from .recall_contract_builder import build_unavailable_pack as _build_unavailable_pack

    return _build_unavailable_pack(
        agent_type=agent_type,
        task_type=task_type,
        profile=profile,
        query=query,
        reason=reason,
    )


def build_recall_pack(
    *,
    raw_memories: Iterable[Any],
    agent_type: str,
    task_type: str,
    profile: RecallProfile,
    query: str,
    now: datetime | None = None,
) -> MemoryRecallPack:
    """Normalize, filter, and pack raw memory results for one agent prompt.

    Returns:
        MemoryRecallPack containing selected prompt-eligible memories.
    """
    from .recall_contract_builder import build_recall_pack as _build_recall_pack

    return _build_recall_pack(
        raw_memories=raw_memories,
        agent_type=agent_type,
        task_type=task_type,
        profile=profile,
        query=query,
        now=now,
    )
