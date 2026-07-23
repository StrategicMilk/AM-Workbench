"""Prompt-ready context selection from structured ContextAsset packs."""

from __future__ import annotations

import json
from collections.abc import Iterable

from vetinari.workbench.context_assets.contracts import ContextAssetPack, FreshnessState, PromptSafetyStatus
from vetinari.workbench.context_assets.freshness import PUBLISH_USEFULNESS_THRESHOLD, score_context_asset_usefulness
from vetinari.workbench.context_assets.registry import ContextAssetRegistry


def select_context_assets_for_prompt(
    source: ContextAssetRegistry | Iterable[ContextAssetPack],
    *,
    intended_agent_profile: str,
    token_budget: int,
    min_usefulness: float = PUBLISH_USEFULNESS_THRESHOLD,
) -> tuple[ContextAssetPack, ...]:
    """Return fresh useful packs that fit the requested prompt budget.

    Returns:
        Resolved context assets for prompt value.
    """
    if token_budget <= 0:
        return ()
    packs = (
        source.list_packs(intended_agent_profile=intended_agent_profile)
        if isinstance(source, ContextAssetRegistry)
        else tuple(source)
    )
    candidates = [
        pack
        for pack in packs
        if intended_agent_profile in pack.intended_agent_profiles
        and pack.freshness in {FreshnessState.FRESH, FreshnessState.AGING}
        and pack.prompt_safety_status is not PromptSafetyStatus.UNSAFE_BLOCKED
        and pack.usefulness_score >= min_usefulness
        and score_context_asset_usefulness(pack, requested_token_budget=token_budget) >= min_usefulness
        and not any(trigger.is_active for trigger in pack.invalidation_triggers)
        and pack.token_budget <= token_budget
    ]
    candidates.sort(
        key=lambda pack: (
            pack.freshness is FreshnessState.FRESH,
            pack.usefulness_score,
            -len(pack.contradiction_ledger),
            -pack.token_budget,
            pack.observed_at_utc,
            pack.context_asset_id,
        ),
        reverse=True,
    )
    selected: list[ContextAssetPack] = []
    used_tokens = 0
    for pack in candidates:
        if used_tokens + pack.token_budget > token_budget:
            continue
        selected.append(pack)
        used_tokens += pack.token_budget
    return tuple(selected)


def render_prompt_context(packs: Iterable[ContextAssetPack]) -> str:
    """Render selected context as compact structured JSON with citations.

    Returns:
        str value produced by render_prompt_context().
    """
    payload = {
        "context_assets": [
            {
                "context_asset_id": pack.context_asset_id,
                "kind": pack.kind.value,
                "revision": pack.revision,
                "freshness": pack.freshness.value,
                "usefulness_score": pack.usefulness_score,
                "token_budget": pack.token_budget,
                "source_ids": [source.source_id for source in pack.source_coverage],
                "invalidation_triggers": [
                    {
                        "trigger_id": trigger.trigger_id,
                        "source_id": trigger.source_id,
                        "active": trigger.is_active,
                    }
                    for trigger in pack.invalidation_triggers
                ],
                "upstream_evidence_refs": list(pack.upstream_evidence_refs),
                "content_role": "quoted_untrusted_data"
                if pack.prompt_safety_status is PromptSafetyStatus.UNTRUSTED_QUOTED
                else "trusted_summary",
                "prompt_safety_status": pack.prompt_safety_status.value,
                "content_summary": pack.content_summary,
            }
            for pack in packs
        ]
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
