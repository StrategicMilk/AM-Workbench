"""Freshness and usefulness scoring for context asset packs."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from vetinari.workbench.context_assets.contracts import ContextAssetPack, FreshnessState, InvalidationTrigger

logger = logging.getLogger(__name__)


PUBLISH_USEFULNESS_THRESHOLD = 0.65


def evaluate_context_asset_freshness(
    observed_at_utc: str,
    *,
    max_age_seconds: int,
    invalidation_triggers: tuple[InvalidationTrigger, ...] = (),
    now_utc: datetime | None = None,
) -> FreshnessState:
    """Evaluate freshness from observation time and invalidation evidence.

    Returns:
        FreshnessState value produced by evaluate_context_asset_freshness().
    """
    if any(trigger.is_active for trigger in invalidation_triggers):
        return FreshnessState.STALE
    if max_age_seconds < 0:
        return FreshnessState.UNKNOWN
    try:
        observed = _parse_utc(observed_at_utc)
    except ValueError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return FreshnessState.UNKNOWN
    now = _coerce_utc(now_utc or datetime.now(timezone.utc))
    age_seconds = int((now - observed).total_seconds())
    if age_seconds < 0:
        return FreshnessState.UNKNOWN
    if age_seconds > max_age_seconds:
        return FreshnessState.STALE
    if age_seconds > max_age_seconds * 0.75:
        return FreshnessState.AGING
    return FreshnessState.FRESH


def score_context_asset_usefulness(
    pack: ContextAssetPack,
    *,
    requested_token_budget: int | None = None,
) -> float:
    """Score usefulness while demoting unknown, incomplete, or oversized context.

    Returns:
        Scored context asset usefulness result.
    """
    coverage_score = sum(source.coverage_ratio for source in pack.source_coverage) / len(pack.source_coverage)
    provenance_score = min(len(dict(pack.provenance)) / 2.0, 1.0)
    freshness_score = {
        FreshnessState.FRESH: 1.0,
        FreshnessState.AGING: 0.65,
        FreshnessState.STALE: 0.0,
        FreshnessState.UNKNOWN: 0.0,
    }[pack.freshness]
    contradiction_penalty = min(0.35, 0.12 * len(pack.contradiction_ledger))
    invalidation_penalty = 0.25 if any(trigger.is_active for trigger in pack.invalidation_triggers) else 0.0
    budget = requested_token_budget or pack.token_budget
    token_score = 1.0 if pack.token_budget <= budget else max(0.0, budget / pack.token_budget)
    raw = (
        coverage_score * 0.35
        + provenance_score * 0.2
        + freshness_score * 0.25
        + token_score * 0.2
        - contradiction_penalty
        - invalidation_penalty
    )
    return round(max(0.0, min(1.0, raw)), 4)


def is_publishable_context_asset(pack: ContextAssetPack) -> bool:
    """Return true only for fresh enough, useful enough context."""
    return (
        pack.freshness in {FreshnessState.FRESH, FreshnessState.AGING}
        and score_context_asset_usefulness(pack) >= PUBLISH_USEFULNESS_THRESHOLD
        and not any(trigger.is_active for trigger in pack.invalidation_triggers)
    )


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _coerce_utc(parsed)


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
