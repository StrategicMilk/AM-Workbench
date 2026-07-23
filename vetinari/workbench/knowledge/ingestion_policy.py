"""Knowledge ingestion and codebase-sync freshness policy contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ChunkingStrategy(str, Enum):
    """Supported Workbench knowledge chunking strategies."""

    RECURSIVE = "recursive"
    SEMANTIC = "semantic"
    FIXED = "fixed"


class KnowledgePolicyStatus(str, Enum):
    """Trust outcome for a knowledge ingestion or sync asset."""

    ALLOWED = "allowed"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class KnowledgeIngestionPolicy:
    """Chunking, embedding, and incremental reindex policy."""

    policy_id: str
    strategy: ChunkingStrategy
    split_boundaries: tuple[str, ...]
    overlap_tokens: int
    similarity_threshold: float
    preserve_code_blocks: bool
    incremental_reindex: bool
    embedding_provider: str
    embedding_model: str

    def __post_init__(self) -> None:
        _require_text(self.policy_id, "policy_id")
        object.__setattr__(self, "strategy", ChunkingStrategy(self.strategy))
        if not self.split_boundaries:
            raise ValueError("split boundaries are required")
        if self.overlap_tokens < 0:
            raise ValueError("overlap_tokens must be non-negative")
        if not 0.0 <= self.similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be between 0 and 1")
        for field_name in ("embedding_provider", "embedding_model"):
            _require_text(getattr(self, field_name), field_name)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"KnowledgeIngestionPolicy(policy_id={self.policy_id!r}, strategy={self.strategy!r}, split_boundaries={self.split_boundaries!r})"


@dataclass(frozen=True, slots=True)
class CodebaseSyncAsset:
    """Freshness-tracked codebase context asset."""

    repository_id: str
    branch: str
    content_hash: str
    indexed_hash: str
    freshness: str
    evidence_ref: str

    def __post_init__(self) -> None:
        for field_name in ("repository_id", "branch", "content_hash", "indexed_hash", "freshness", "evidence_ref"):
            _require_text(getattr(self, field_name), field_name)

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"CodebaseSyncAsset(repository_id={self.repository_id!r}, branch={self.branch!r}, content_hash={self.content_hash!r})"


@dataclass(frozen=True, slots=True)
class KnowledgePolicyDecision:
    """Decision emitted for ingestion policy and codebase sync freshness."""

    status: KnowledgePolicyStatus
    trusted_current: bool
    reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", KnowledgePolicyStatus(self.status))

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"KnowledgePolicyDecision(status={self.status!r}, trusted_current={self.trusted_current!r}, reasons={self.reasons!r})"


def evaluate_knowledge_ingestion_policy(
    policy: KnowledgeIngestionPolicy,
    *,
    sync_asset: CodebaseSyncAsset | None = None,
) -> KnowledgePolicyDecision:
    """Allow current context only when chunking policy and sync freshness are safe.

    Returns:
        KnowledgePolicyDecision value produced by evaluate_knowledge_ingestion_policy().
    """
    reasons: list[str] = []
    if policy.strategy is ChunkingStrategy.SEMANTIC and policy.similarity_threshold <= 0.0:
        reasons.append("semantic_threshold_missing")
    if not policy.preserve_code_blocks:
        reasons.append("code_block_preservation_required")
    evidence_refs = (f"knowledge-policy:{policy.policy_id}",)
    if sync_asset is not None:
        evidence_refs = (*evidence_refs, sync_asset.evidence_ref)
        if sync_asset.content_hash != sync_asset.indexed_hash:
            reasons.append("codebase_sync_hash_drift")
        if sync_asset.freshness != "fresh":
            reasons.append(f"codebase_sync_{sync_asset.freshness}")
    if any(reason.endswith(("hash_drift", "_stale")) for reason in reasons):
        return KnowledgePolicyDecision(KnowledgePolicyStatus.BLOCKED, False, tuple(reasons), evidence_refs)
    if reasons:
        return KnowledgePolicyDecision(KnowledgePolicyStatus.DEGRADED, False, tuple(reasons), evidence_refs)
    return KnowledgePolicyDecision(KnowledgePolicyStatus.ALLOWED, True, ("knowledge-policy-allowed",), evidence_refs)


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")


__all__ = [
    "ChunkingStrategy",
    "CodebaseSyncAsset",
    "KnowledgeIngestionPolicy",
    "KnowledgePolicyDecision",
    "KnowledgePolicyStatus",
    "evaluate_knowledge_ingestion_policy",
]
