"""Shared file paths for the disk-aware skill registry."""

from __future__ import annotations

from pathlib import Path

_VETINARI_PKG = Path(__file__).parent.parent
_CENTRAL_REGISTRY = _VETINARI_PKG / "skills_registry.json"
_AGENT_SKILL_MAP = _VETINARI_PKG / "config" / "agent_skill_map.json"
_CONTEXT_REGISTRY = _VETINARI_PKG / "context_registry.json"
