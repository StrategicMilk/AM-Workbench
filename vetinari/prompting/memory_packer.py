"""Agent-profile-aware memory recall prompt packer."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import yaml

from vetinari.memory.recall_contract import (
    AuthorityTier,
    MemoryRecallPack,
    RecallProfile,
    build_recall_pack,
    build_unavailable_pack,
)

logger = logging.getLogger(__name__)

_FAIL_CLOSED_MAX_TOKENS = 0


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE_PATH = _PROJECT_ROOT / "config" / "agent_memory_profiles.yaml"


def load_memory_profiles(profile_path: str | Path | None = None) -> dict[str, RecallProfile]:
    """Load and validate memory packing profiles from YAML.

    Returns:
        Resolved memory profiles value.
    """
    path = Path(profile_path) if profile_path is not None else DEFAULT_PROFILE_PATH
    try:
        with path.open(encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.warning("Memory recall profile config unavailable: %s", exc)
        return {"fail_closed": _fail_closed_profile()}

    raw_profiles = payload.get("profiles") if isinstance(payload, Mapping) else None
    if not isinstance(raw_profiles, Mapping):
        logger.warning("Memory recall profile config has no profiles mapping")
        return {"fail_closed": _fail_closed_profile()}

    profiles: dict[str, RecallProfile] = {}
    for profile_id, raw_profile in raw_profiles.items():
        if not isinstance(raw_profile, Mapping):
            logger.warning("Skipping malformed memory profile %s", profile_id)
            continue
        try:
            profiles[str(profile_id)] = RecallProfile.from_mapping(str(profile_id), raw_profile)
        except Exception as exc:
            logger.warning("Skipping invalid memory profile %s: %s", profile_id, exc)
    return profiles or {"fail_closed": _fail_closed_profile()}


def resolve_memory_profile(
    agent_type: str,
    *,
    profiles: Mapping[str, RecallProfile] | None = None,
    profile_path: str | Path | None = None,
) -> RecallProfile:
    """Resolve an agent type to its configured memory recall profile.

    Returns:
        Resolved memory profile value.
    """
    loaded = dict(profiles or load_memory_profiles(profile_path))
    normalized = agent_type.upper()
    for profile in loaded.values():
        if normalized in profile.agent_types:
            return profile
    return loaded.get("default") or next(iter(loaded.values()), _fail_closed_profile())


def build_memory_recall_pack(
    *,
    agent_type: str,
    task_type: str,
    query: str,
    prior_memories: Iterable[Any] | None = None,
    store_provider: Callable[[], Any] | None = None,
    profile_path: str | Path | None = None,
    profiles: Mapping[str, RecallProfile] | None = None,
) -> MemoryRecallPack:
    """Return a deterministic memory recall pack for a live prompt path.

        Missing config, unavailable stores, and unreadable memory state return an
        ``unavailable`` pack with no prompt text. That is intentional fail-closed
        behavior: callers may include the audit metadata, but they must not inject
        unvetted memory content.

    Returns:
        Newly constructed memory recall pack value.
    """
    profile = resolve_memory_profile(agent_type, profiles=profiles, profile_path=profile_path)
    if profile.max_items == 0 or profile.max_tokens == 0:
        return build_unavailable_pack(
            agent_type=agent_type,
            task_type=task_type,
            profile=profile,
            query=query,
            reason="memory_recall_profile_fail_closed",
        )

    if prior_memories is not None:
        return build_recall_pack(
            raw_memories=prior_memories,
            agent_type=agent_type,
            task_type=task_type,
            profile=profile,
            query=query,
        )

    try:
        provider = store_provider or _default_store_provider
        store = provider()
        raw_memories = _search_store(store, query=query, agent_type=agent_type, limit=max(profile.max_items * 2, 1))
    except Exception as exc:
        logger.warning("Memory recall store unavailable for %s/%s: %s", agent_type, task_type, exc)
        return build_unavailable_pack(
            agent_type=agent_type,
            task_type=task_type,
            profile=profile,
            query=query,
            reason=f"memory_recall_unavailable:{exc.__class__.__name__}",
        )

    return build_recall_pack(
        raw_memories=raw_memories,
        agent_type=agent_type,
        task_type=task_type,
        profile=profile,
        query=query,
    )


def pack_memory_prompt(
    *,
    agent_type: str,
    task_type: str,
    query: str,
    prior_memories: Iterable[Any] | None = None,
    store_provider: Callable[[], Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return prompt text plus audit metadata for callers that need both.

    Returns:
        tuple[str, dict[str, Any]] value produced by pack_memory_prompt().
    """
    pack = build_memory_recall_pack(
        agent_type=agent_type,
        task_type=task_type,
        query=query,
        prior_memories=prior_memories,
        store_provider=store_provider,
    )
    return pack.prompt_text, pack.to_dict()


def _search_store(store: Any, *, query: str, agent_type: str, limit: int) -> list[Any]:
    try:
        return list(store.search(query, agent=agent_type, limit=limit))
    except TypeError:
        logger.warning("Handled recoverable failure before fallback.", exc_info=True)
        return list(store.search(query, limit=limit))


def _default_store_provider() -> Any:
    from vetinari.memory import get_unified_memory_store

    return get_unified_memory_store()


def _fail_closed_profile() -> RecallProfile:
    return RecallProfile(
        profile_id="fail_closed",
        agent_types=("*",),
        max_items=0,
        max_tokens=_FAIL_CLOSED_MAX_TOKENS,
        min_relevance=1.0,
        min_confidence=1.0,
        authority_tiers=(AuthorityTier.POLICY,),
        scopes=("*",),
    )
