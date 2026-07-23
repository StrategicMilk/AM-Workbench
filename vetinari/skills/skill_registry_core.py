"""Core disk and programmatic lookup behavior for ``SkillRegistry``."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari.skills.skill_definitions import SKILL_REGISTRY
from vetinari.skills.skill_registry_paths import (
    _AGENT_SKILL_MAP,
    _CENTRAL_REGISTRY,
    _CONTEXT_REGISTRY,
    _VETINARI_PKG,
)

logger = logging.getLogger(__name__)


class SkillRegistryCoreMixin:
    """Load registry files and resolve skill metadata from all sources."""

    if TYPE_CHECKING:
        _agent_map: Any
        _contexts: Any
        _manifests: Any
        _registry: Any
        is_loaded: Any

    def load(self) -> None:
        """Load all registry data from disk.

        Raises:
            Exception: Re-raises any exception encountered while reading
                registry files from disk.
        """
        try:
            if _CENTRAL_REGISTRY.exists():
                with Path(_CENTRAL_REGISTRY).open(encoding="utf-8") as f:
                    self._registry = json.load(f)
                logger.info(
                    "Loaded central registry with %s skills",
                    len(self._registry.get("skills", [])),
                )
            else:
                logger.warning("Central registry not found: %s", _CENTRAL_REGISTRY)

            if _AGENT_SKILL_MAP.exists():
                with Path(_AGENT_SKILL_MAP).open(encoding="utf-8") as f:
                    self._agent_map = json.load(f)
                logger.info(
                    "Loaded agent skill mappings for %s agents",
                    len(self._agent_map.get("agents", {})),
                )
            else:
                logger.warning("Agent skill map not found: %s", _AGENT_SKILL_MAP)

            if _CONTEXT_REGISTRY.exists():
                with Path(_CONTEXT_REGISTRY).open(encoding="utf-8") as f:
                    context_data = json.load(f)
                    self._contexts = {ctx["id"]: ctx for ctx in context_data.get("contexts", [])}
                logger.info("Loaded %s context entries", len(self._contexts))
            else:
                logger.warning("Context registry not found: %s", _CONTEXT_REGISTRY)

            self.is_loaded = True

        except Exception as exc:
            logger.error("Failed to load registry: %s", exc)
            raise

    def list_skills(self) -> list[dict[str, Any]]:
        """List all available skills with basic metadata.

        Merges disk-based skills with programmatic ``SkillSpec`` entries,
        deduplicating by skill id.

        Returns:
            List of skill metadata dicts.  Disk entries come first; programmatic
            entries are appended for any skill id not already present.
        """
        if not self.is_loaded:
            self.load()
        disk_skills = self._registry.get("skills", [])
        seen_ids = {s.get("id") for s in disk_skills}
        merged = list(disk_skills)
        for spec in SKILL_REGISTRY.values():
            if spec.skill_id not in seen_ids:
                merged.append(spec.to_dict())
                seen_ids.add(spec.skill_id)
        return merged

    def get_skill(self, skill_id: str) -> dict[str, Any] | None:
        """Get skill metadata by ID.

        Falls back to the programmatic ``SKILL_REGISTRY`` when the disk-based
        registry does not contain the requested skill.

        Args:
            skill_id: The skill identifier to look up.

        Returns:
            Skill metadata dict, or None if not found in either source.
        """
        if not self.is_loaded:
            self.load()
        skills = self._registry.get("skills", [])
        result = next((s for s in skills if s["id"] == skill_id), None)
        if result is not None:
            return result
        spec = SKILL_REGISTRY.get(skill_id)
        if spec:
            return spec.to_dict()
        return None

    def get_skill_manifest(self, skill_id: str) -> dict[str, Any] | None:
        """Get full manifest for a skill.

        Checks the manifest cache first, then tries a per-skill JSON file on
        disk, then falls back to the programmatic ``SKILL_REGISTRY``, and
        finally synthesises a minimal manifest from the disk skill list entry.

        Args:
            skill_id: The skill identifier.

        Returns:
            Manifest dict, or None if no source provides one.
        """
        if skill_id in self._manifests:
            return self._manifests[skill_id]

        manifest_path = _VETINARI_PKG / "skills" / skill_id / "manifest.json"
        if manifest_path.exists():
            with Path(manifest_path).open(encoding="utf-8") as f:
                manifest = json.load(f)
                self._manifests[skill_id] = manifest
                return manifest

        spec = SKILL_REGISTRY.get(skill_id)
        if spec:
            manifest = spec.to_dict()
            self._manifests[skill_id] = manifest
            return manifest

        skill = self.get_skill(skill_id)
        if skill:
            manifest = {
                "skill_id": skill.get("skill_id") or skill.get("id"),
                "name": skill.get("name", skill_id),
                "description": skill.get("description", ""),
                "capabilities": skill.get("capabilities", []),
                "permissions": skill.get("permissions_required", []),
            }
            self._manifests[skill_id] = manifest
            return manifest
        return None

    def get_skill_capabilities(self, skill_id: str) -> list[str]:
        """Get the list of capabilities declared for a skill.

        Args:
            skill_id: The skill identifier.

        Returns:
            List of capability strings, or an empty list when the skill is
            not found.
        """
        skill = self.get_skill(skill_id)
        if skill:
            return skill.get("capabilities", [])
        manifest = self.get_skill_manifest(skill_id)
        if manifest:
            return manifest.get("capabilities", [])
        return []

    def get_skill_permissions(self, skill_id: str) -> list[str]:
        """Get required permissions for a skill.

        Args:
            skill_id: The skill identifier.

        Returns:
            List of required permission strings, or an empty list when the
            skill is not found.
        """
        skill = self.get_skill(skill_id)
        if skill:
            return skill.get("permissions_required", [])
        manifest = self.get_skill_manifest(skill_id)
        if manifest:
            return manifest.get("required_permissions", [])
        return []

    def get_skill_by_capability(self, capability: str) -> list[dict[str, Any]]:
        """Find skills that support a specific capability.

        Searches both the disk-based registry and the programmatic
        ``SKILL_REGISTRY``, deduplicating by skill id.

        Args:
            capability: The capability string to search for.

        Returns:
            List of skill metadata dicts that declare the given capability.
        """
        from vetinari.skills.skill_registry import get_skills_by_capability

        seen_ids: set[str] = set()
        matching: list[dict[str, Any]] = []

        for skill in self.list_skills():
            if capability in skill.get("capabilities", []):
                matching.append(skill)
                seen_ids.add(str(skill.get("id") or skill.get("skill_id")))

        for spec in get_skills_by_capability(capability):
            if spec.skill_id not in seen_ids:
                matching.append(spec.to_dict())
                seen_ids.add(spec.skill_id)

        return matching
