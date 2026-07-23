"""Catalog-driven agent default model resolver."""

from __future__ import annotations

import threading
from typing import Any

import yaml

from vetinari.config_paths import resolve_config_path
from vetinari.types import AgentType

# Double-checked locking singleton — thread-safe lazy init without lru_cache.
# lru_cache does not acquire a lock before the first call so two threads can
# race to call the underlying function simultaneously.
_DEFAULTS_CACHE: dict[str, Any] | None = None
_DEFAULTS_LOCK = threading.Lock()


def _agent_key(agent_type: AgentType | str) -> str:
    if isinstance(agent_type, AgentType):
        return agent_type.name
    raw = str(agent_type)
    return raw.split(".")[-1].upper()


def _load_defaults() -> dict[str, Any]:
    """Load agent model defaults from YAML using double-checked locking.

    Returns:
        Parsed YAML data dict containing the ``defaults`` block.

    Raises:
        ValueError: If the YAML file has no ``defaults`` block.
    """
    global _DEFAULTS_CACHE
    if _DEFAULTS_CACHE is None:
        with _DEFAULTS_LOCK:
            if _DEFAULTS_CACHE is None:
                path = resolve_config_path("agent_model_defaults.yaml")
                with path.open(encoding="utf-8") as handle:
                    data = yaml.safe_load(handle) or {}
                if "defaults" not in data:
                    raise ValueError(f"agent defaults file has no defaults block: {path}")
                _DEFAULTS_CACHE = data
    return _DEFAULTS_CACHE


def _blocked_reason(row: dict[str, Any], model_id: str) -> str | None:
    status = str(row.get("status", "")).lower()
    if status in {"blocked", "disabled", "rejected"}:
        return f"agent default row status is {status}"
    reviews = row.get("release_license_review")
    if isinstance(reviews, dict):
        review = reviews.get(model_id)
        if isinstance(review, dict) and str(review.get("status", "")).lower() == "blocked":
            return str(review.get("license_ref") or "release/license review blocks this model")
    return None


def _resolve_model_from_row(row: dict[str, Any], model_id: Any, agent_type: AgentType | str) -> str:
    if not model_id:
        raise ValueError(f"agent default for {agent_type} has no resolved_model_id")
    resolved = str(model_id)
    reason = _blocked_reason(row, resolved)
    if reason:
        raise ValueError(f"agent default for {agent_type} resolves to blocked model {resolved}: {reason}")
    return resolved


def resolve(agent_type: AgentType | str, mode: str | None = None) -> str:
    """Resolve an agent default model id from config/agent_model_defaults.yaml.

    Args:
        agent_type: Agent enum or string key to resolve.
        mode: Optional mode-specific default override.

    Returns:
        Resolved model id string.

    Raises:
        KeyError: If the agent has no configured default.
        ValueError: If the configured row lacks a resolved model id.
    """
    data = _load_defaults()
    row = data["defaults"].get(_agent_key(agent_type))
    if not row:
        raise KeyError(f"no agent default for {agent_type}")
    if mode and isinstance(row.get("modes"), dict) and mode in row["modes"]:
        return _resolve_model_from_row(row, row["modes"][mode], agent_type)
    return _resolve_model_from_row(row, row.get("resolved_model_id"), agent_type)
