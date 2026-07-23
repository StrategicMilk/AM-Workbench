"""Read-only in-memory registry and upstream adapters for context assets."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from vetinari.workbench.context_assets.contracts import (
    ContextAssetKind,
    ContextAssetPack,
    ContextAssetSource,
    ContextAssetValidationError,
    InvalidationTrigger,
    PromptSafetyStatus,
)
from vetinari.workbench.context_assets.freshness import evaluate_context_asset_freshness, score_context_asset_usefulness
from vetinari.workbench.evidence_assets import EvidenceAssetCard, ProofStatus
from vetinari.workbench.memory.spine.lineage import MemoryLineageError, MemoryValidationState, validate_memory_payload
from vetinari.workbench.rag_debugger import ContextAssembly, RetrievalTrace


class ContextAssetRegistry:
    """Import-safe, in-memory index over context asset pack revisions."""

    def __init__(self, packs: Iterable[ContextAssetPack] = ()) -> None:
        self._packs: tuple[ContextAssetPack, ...] = ()
        for pack in packs:
            self.register(pack)

    def register(self, pack: ContextAssetPack) -> ContextAssetPack:
        """Add a pack revision to the local index and return it.

        Returns:
            ContextAssetPack value produced by register().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(pack, ContextAssetPack):
            raise ContextAssetValidationError("registry accepts ContextAssetPack objects only")
        self._packs = (*self._packs, pack)
        return pack

    def list_packs(
        self,
        *,
        context_asset_id: str | None = None,
        kind: ContextAssetKind | None = None,
        intended_agent_profile: str | None = None,
        source_id: str | None = None,
    ) -> tuple[ContextAssetPack, ...]:
        """List revisions matching the requested index fields.

        Returns:
            Collection of packs values.
        """
        packs = self._packs
        if context_asset_id is not None:
            packs = tuple(pack for pack in packs if pack.context_asset_id == context_asset_id)
        if kind is not None:
            packs = tuple(pack for pack in packs if pack.kind is kind)
        if intended_agent_profile is not None:
            packs = tuple(pack for pack in packs if intended_agent_profile in pack.intended_agent_profiles)
        if source_id is not None:
            packs = tuple(
                pack for pack in packs if any(source.source_id == source_id for source in pack.source_coverage)
            )
        return tuple(sorted(packs, key=lambda pack: (pack.context_asset_id, pack.revision, pack.observed_at_utc)))

    def latest(self, context_asset_id: str) -> ContextAssetPack | None:
        """Return the deterministic latest revision by observed timestamp.

        Returns:
            ContextAssetPack | None value produced by latest().
        """
        matches = [pack for pack in self._packs if pack.context_asset_id == context_asset_id]
        if not matches:
            return None
        return max(matches, key=lambda pack: (pack.observed_at_utc, pack.revision))


def build_context_asset_from_evidence_card(
    card: EvidenceAssetCard,
    *,
    intended_agent_profiles: tuple[str, ...] = ("workbench",),
    token_budget: int = 512,
    max_age_seconds: int = 2_592_000,
) -> ContextAssetPack:
    """Normalize an evidence card into a context asset pack without mutating it.

    Returns:
        Newly constructed context asset from evidence card value.
    """
    provenance = tuple(card.provenance)
    source_id = dict(provenance).get("source", card.asset_card_id)
    trigger = InvalidationTrigger(
        trigger_id=f"{card.asset_card_id}:evidence-proof-change",
        description="Evidence card proof status, taints, or failure history changed.",
        source_id=card.asset_card_id,
        triggered_at_utc=card.created_at_utc if card.proof_status is not ProofStatus.VERIFIED else "",
    )
    source = ContextAssetSource(
        source_id=card.asset_card_id,
        source_kind=f"evidence_asset:{card.kind.value}",
        coverage_ratio=1.0 if card.proof_status is ProofStatus.VERIFIED else 0.45,
        observed_at_utc=card.created_at_utc,
        max_age_seconds=max_age_seconds,
        metadata=(("source", source_id),),
    )
    freshness = evaluate_context_asset_freshness(
        card.created_at_utc,
        max_age_seconds=max_age_seconds,
        invalidation_triggers=(trigger,),
    )
    pack = ContextAssetPack(
        context_asset_id=card.asset_card_id,
        kind=ContextAssetKind.EVIDENCE_ASSET,
        title=card.name,
        revision=card.revision,
        observed_at_utc=card.created_at_utc,
        source_coverage=(source,),
        freshness=freshness,
        contradiction_ledger=(),
        provenance=provenance,
        intended_agent_profiles=intended_agent_profiles,
        token_budget=token_budget,
        usefulness_score=0.0,
        invalidation_triggers=(trigger,),
        upstream_evidence_refs=(card.asset_card_id,),
        content_summary=f"{card.kind.value} evidence card with proof_status={card.proof_status.value}",
    )
    return replace(pack, usefulness_score=score_context_asset_usefulness(pack))


def build_context_asset_from_rag_trace(
    trace: RetrievalTrace,
    context_assembly: ContextAssembly,
    *,
    intended_agent_profiles: tuple[str, ...] = ("rag", "workbench"),
    observed_at_utc: str = "1970-01-01T00:00:00Z",
    token_budget: int | None = None,
    max_age_seconds: int = 604_800,
) -> ContextAssetPack:
    """Normalize a RAG trace and context assembly into a retrieval context pack.

    Args:
        trace: Trace value consumed by build_context_asset_from_rag_trace().
        context_assembly: Context assembly value consumed by build_context_asset_from_rag_trace().
        intended_agent_profiles: File path or file-like value consumed by the operation.
        observed_at_utc: Observed at utc value consumed by build_context_asset_from_rag_trace().
        token_budget: Token budget value consumed by build_context_asset_from_rag_trace().
        max_age_seconds: Max age seconds value consumed by build_context_asset_from_rag_trace().

    Returns:
        Newly constructed context asset from rag trace value.
    """
    total_chunks = len(trace.candidates) + len(trace.rejected_candidates)
    included = len(context_assembly.included_chunk_ids)
    coverage_ratio = included / total_chunks if total_chunks else 0.0
    source = ContextAssetSource(
        source_id=trace.collection,
        source_kind="rag_retrieval_collection",
        coverage_ratio=coverage_ratio,
        observed_at_utc=observed_at_utc,
        max_age_seconds=max_age_seconds,
        metadata=(("revision_id", trace.revision_id), ("embedding_model", trace.embedding_model)),
    )
    trigger = InvalidationTrigger(
        trigger_id=f"{trace.revision_id}:retrieval-index-change",
        description="Dataset revision retrieval index or embedding model changed.",
        source_id=trace.collection,
    )
    freshness = evaluate_context_asset_freshness(
        observed_at_utc,
        max_age_seconds=max_age_seconds,
        invalidation_triggers=(trigger,),
    )
    prompt_safety = _prompt_safety_status_for_text(context_assembly.context_text)
    pack = ContextAssetPack(
        context_asset_id=f"rag:{trace.revision_id}:{trace.collection}",
        kind=ContextAssetKind.RETRIEVAL_COLLECTION,
        title=f"RAG context for {trace.revision_id}",
        revision=trace.revision_id,
        observed_at_utc=observed_at_utc,
        source_coverage=(source,),
        freshness=freshness,
        contradiction_ledger=(),
        provenance=(("source", "rag_debugger"), ("collection", trace.collection)),
        intended_agent_profiles=intended_agent_profiles,
        token_budget=token_budget or max(1, context_assembly.token_count),
        usefulness_score=0.0,
        invalidation_triggers=(trigger,),
        upstream_evidence_refs=tuple(context_assembly.included_chunk_ids),
        content_summary=_safe_context_summary(context_assembly.context_text, prompt_safety),
        prompt_safety_status=prompt_safety,
    )
    return replace(pack, usefulness_score=score_context_asset_usefulness(pack))


def build_context_asset_from_memory_lineage_payload(
    payload: dict[str, object],
    *,
    intended_agent_profiles: tuple[str, ...] = ("memory", "workbench"),
    token_budget: int = 384,
    max_age_seconds: int = 2_592_000,
) -> ContextAssetPack:
    """Normalize verified memory lineage payloads and reject untrusted memory.

    Returns:
        Newly constructed context asset from memory lineage payload value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    try:
        record = validate_memory_payload(payload)
    except MemoryLineageError as exc:
        raise ContextAssetValidationError(f"memory context is not verified: {exc}") from exc
    if record.validation_state is not MemoryValidationState.VERIFIED:
        raise ContextAssetValidationError(f"memory context is not verified: {record.validation_state.value}")
    trigger = InvalidationTrigger(
        trigger_id=f"{record.memory_id}:memory-lineage-change",
        description="Memory lineage proof, policy, or prompt-injection scan changed.",
        source_id=record.memory_id,
    )
    source = ContextAssetSource(
        source_id=record.memory_id,
        source_kind="memory_lineage",
        coverage_ratio=1.0,
        observed_at_utc=record.created_at_utc,
        max_age_seconds=max_age_seconds,
        metadata=(("source_run_id", record.source_run_id), ("trace_id", record.trace_id)),
    )
    freshness = evaluate_context_asset_freshness(
        record.created_at_utc,
        max_age_seconds=max_age_seconds,
        invalidation_triggers=(trigger,),
    )
    pack = ContextAssetPack(
        context_asset_id=f"memory:{record.memory_id}",
        kind=ContextAssetKind.MEMORY_CONTEXT,
        title=f"Memory context {record.memory_id}",
        revision=record.asset_revision,
        observed_at_utc=record.created_at_utc,
        source_coverage=(source,),
        freshness=freshness,
        contradiction_ledger=(),
        provenance=tuple(record.provenance.items()),
        intended_agent_profiles=intended_agent_profiles,
        token_budget=token_budget,
        usefulness_score=0.0,
        invalidation_triggers=(trigger,),
        upstream_evidence_refs=record.evidence_asset_ids,
        content_summary=record.provenance["reason"],
    )
    return replace(pack, usefulness_score=score_context_asset_usefulness(pack))


def _prompt_safety_status_for_text(value: str) -> PromptSafetyStatus:
    lowered = value.lower()
    unsafe_markers = (
        "ignore previous instructions",
        "ignore all previous instructions",
        "system prompt",
        "developer message",
        "api_key",
        "secret",
        "sk-",
        "password",
    )
    if any(marker in lowered for marker in unsafe_markers):
        return PromptSafetyStatus.UNSAFE_BLOCKED
    return PromptSafetyStatus.UNTRUSTED_QUOTED if value.strip() else PromptSafetyStatus.UNSAFE_BLOCKED


def _safe_context_summary(value: str, status: PromptSafetyStatus) -> str:
    if status is PromptSafetyStatus.UNSAFE_BLOCKED:
        return "[blocked unsafe retrieval context]"
    return value[:500]
