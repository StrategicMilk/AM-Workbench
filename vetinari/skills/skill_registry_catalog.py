"""Agent, context, workflow, search, and validation behavior for SkillRegistry."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from vetinari.skills.skill_definitions import SKILL_REGISTRY
from vetinari.skills.skill_registry_paths import _CENTRAL_REGISTRY


class SkillRegistryCatalogMixin:
    """Query registry catalog data beyond single-skill metadata."""

    if TYPE_CHECKING:
        _agent_map: Any
        _contexts: Any
        _registry: Any
        get_skill: Any
        get_skill_manifest: Any
        is_loaded: Any
        list_skills: Any
        load: Any

    def list_agents(self) -> list[str]:
        """List all registered agent types that have at least one skill.

        Merges disk-based agent ids (from the agent-skill map) with the
        canonical agent type strings in the programmatic ``SKILL_REGISTRY``.

        Returns:
            Sorted list of agent type strings.
        """
        if not self.is_loaded:
            self.load()
        agent_set = set(self._agent_map.get("agents", {}).keys())
        agent_set.update(spec.agent_type for spec in SKILL_REGISTRY.values())
        return sorted(agent_set)

    def get_agent_skills(
        self,
        agent_id: str,
        env: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get skills mapped to a specific agent.

        Checks the disk-based agent-skill map first (with optional
        environment-specific overrides), then falls back to the programmatic
        ``SKILL_REGISTRY`` via ``get_skill_for_agent_type()``.

        Args:
            agent_id: The agent id or canonical agent type (e.g. ``'WORKER'``).
            env: Optional environment name for environment-specific overrides.

        Returns:
            List of skill mapping dicts.  Each dict contains at least
            ``skill_id``; disk entries may include additional metadata.
        """
        from vetinari.skills.skill_registry import get_skill_for_agent_type

        if not self.is_loaded:
            self.load()

        agents = self._agent_map.get("agents", {})
        agent_config = agents.get(agent_id, {})

        if env:
            overrides = self._agent_map.get("environment_overrides", {})
            env_overrides = overrides.get(env, {})
            agent_config = env_overrides.get("agents", {}).get(agent_id, agent_config)

        disk_skills = agent_config.get("default_skills", [])
        if disk_skills:
            return disk_skills

        spec = get_skill_for_agent_type(agent_id)
        if spec:
            return [{"skill_id": spec.skill_id, "source": "programmatic"}]
        return []

    def get_context(self, context_id: str) -> dict[str, Any] | None:
        """Get a sample context by ID.

        Args:
            context_id: The context identifier.

        Returns:
            Context dict, or None if not found.
        """
        if not self.is_loaded:
            self.load()
        return self._contexts.get(context_id)

    def get_contexts_for_skill(self, skill_id: str) -> list[dict[str, Any]]:
        """Get all sample contexts available for a specific skill.

        Args:
            skill_id: The skill identifier.

        Returns:
            List of context dicts whose ``skill_ids`` field includes
            ``skill_id``.
        """
        if not self.is_loaded:
            self.load()
        return [ctx for ctx in self._contexts.values() if skill_id in ctx.get("skill_ids", [])]

    def list_workflows(self) -> dict[str, list[dict[str, str]]]:
        """List predefined skill workflows from the agent-skill map.

        Returns:
            Dict mapping workflow name to ordered list of step dicts.
        """
        if not self.is_loaded:
            self.load()
        return self._agent_map.get("workflows", {})

    def get_compatibility_matrix(self) -> dict[str, Any]:
        """Get the version compatibility matrix from the central registry.

        Returns:
            Dict containing version compatibility information, or an empty
            dict when no matrix is defined.
        """
        if not self.is_loaded:
            self.load()
        return self._registry.get("version_matrix", {})

    def search_skills(self, query: str) -> list[dict[str, Any]]:
        """Search skills by name, description, tags, or capabilities.

        Searches the merged skill list (disk + programmatic) via
        ``list_skills()``. Results are ranked by match quality so that name
        matches appear before description matches, which appear before
        capability/tag matches.

        Args:
            query: Case-insensitive search string matched against skill name,
                description, capabilities, and tags.

        Returns:
            List of matching skill metadata dicts, ordered best-match first.
        """
        if not self.is_loaded:
            self.load()

        query_lower = query.lower()
        scored: list[tuple[int, dict[str, Any]]] = []
        for skill in self.list_skills():
            score = 0
            if query_lower in skill.get("name", "").lower():
                score += 3
            if query_lower in skill.get("description", "").lower():
                score += 2
            for cap in skill.get("capabilities", []):
                if query_lower in cap.lower():
                    score += 1
            for tag in skill.get("tags", []):
                if query_lower in tag.lower():
                    score += 1
            if score > 0:
                scored.append((score, skill))

        scored.sort(key=lambda t: t[0], reverse=True)
        return [skill for _, skill in scored]

    def validate(self) -> dict[str, list[str]]:
        """Validate registry integrity.

        Checks that the central registry file exists, that all listed skills
        have manifests, and that agent mappings reference known skill ids.

        Returns:
            Dict with two keys: ``'errors'`` (list of blocking problems) and
            ``'warnings'`` (list of non-blocking advisories).
        """
        errors: list[str] = []
        warnings: list[str] = []

        if not self.is_loaded:
            self.load()

        if not _CENTRAL_REGISTRY.exists():
            errors.append(f"Central registry not found: {_CENTRAL_REGISTRY}")

        for skill in self.list_skills():
            skill_id = skill.get("id") or skill.get("skill_id", "")
            manifest = self.get_skill_manifest(skill_id)
            if not manifest:
                warnings.append(f"Manifest missing for skill: {skill_id}")

        agents = self._agent_map.get("agents", {})
        for agent_id, config in agents.items():
            for skill_mapping in config.get("default_skills", []):
                ref_skill_id = skill_mapping.get("skill_id")
                if not self.get_skill(ref_skill_id):
                    errors.append(
                        f"Agent '{agent_id}' references unknown skill: {ref_skill_id}",
                    )

        return {"errors": errors, "warnings": warnings}
