"""Programmatic Skill Registry.

==============================
Typed, validated registry of all Vetinari skills aligned to the 3-agent
factory pipeline (ADR-0061): Foreman, Worker, Inspector.

Each entry uses ``SkillSpec`` to declare capabilities, schemas, constraints,
and quality standards for one of the three canonical agents.

Also exposes ``SkillRegistry`` — a class that loads disk-based JSON skill
manifests and merges them with the programmatic ``SKILL_REGISTRY``.  The
module-level convenience functions (``list_skills``, ``get_skill``, etc.)
delegate to a singleton ``SkillRegistry`` instance.

Usage::

    from vetinari.skills.skill_registry import SKILL_REGISTRY, get_skill, validate_all

    spec = get_skill("worker")
    errors = validate_all()     # [] if all specs are valid

    # Disk-aware registry (JSON manifests + programmatic specs)
    from vetinari.skills.skill_registry import SkillRegistry, get_registry
    reg = get_registry()
    skills = reg.list_skills()
"""

from __future__ import annotations

import logging
from typing import Any

from vetinari.skills.skill_definitions import (
    _AGENT_TO_SKILL,
    SKILL_REGISTRY,
)
from vetinari.skills.skill_registry_class import (
    SkillRegistry as _DiskSkillRegistry,
)
from vetinari.skills.skill_registry_class import (
    get_registry,
)
from vetinari.skills.skill_spec import SkillSpec

logger = logging.getLogger(__name__)


SkillRegistry = _DiskSkillRegistry


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════


def get_skill(skill_id: str) -> SkillSpec | None:
    """Get a skill spec by ID.

    Args:
        skill_id: The skill identifier (foreman, worker, or inspector).

    Returns:
        The matching SkillSpec, or None if not found.
    """
    return SKILL_REGISTRY.get(skill_id)


def get_skill_for_agent_type(agent_type: str) -> SkillSpec | None:
    """Map a canonical agent type string to its skill spec.

    Args:
        agent_type: Agent type value (e.g. 'FOREMAN', 'WORKER', 'INSPECTOR').

    Returns:
        The matching SkillSpec, or None if the type is unknown.
    """
    skill_id = _AGENT_TO_SKILL.get(agent_type.upper())
    if skill_id:
        return SKILL_REGISTRY.get(skill_id)
    return None


def get_all_skills() -> dict[str, SkillSpec]:
    """Return the full registry.

    Returns:
        Dictionary mapping skill_id to SkillSpec for all 3 skills.
    """
    return dict(SKILL_REGISTRY)


def get_skills_by_capability(capability: str) -> list[SkillSpec]:
    """Find all skills that declare a given capability.

    Args:
        capability: Capability string to search for.

    Returns:
        List of SkillSpec instances declaring that capability.
    """
    return [spec for spec in SKILL_REGISTRY.values() if capability in spec.capabilities]


def get_skills_by_tag(tag: str) -> list[SkillSpec]:
    """Find all skills with a given tag.

    Args:
        tag: Tag string to search for.

    Returns:
        List of SkillSpec instances tagged with the given value.
    """
    return [spec for spec in SKILL_REGISTRY.values() if tag in spec.tags]


def get_skills_by_standard_category(category: str) -> list[SkillSpec]:
    """Find all skills that have standards in a given category.

    Args:
        category: Standard category (e.g. 'security', 'code_quality').

    Returns:
        List of SkillSpec instances with standards in that category.
    """
    return [spec for spec in SKILL_REGISTRY.values() if any(s.category == category for s in spec.standards)]


def validate_all() -> list[str]:
    """Validate every skill spec in the registry.

    Returns:
        List of error strings. Empty list means all specs are valid.
    """
    errors = []
    for skill_id, spec in SKILL_REGISTRY.items():
        if spec.skill_id != skill_id:
            errors.append(f"Mismatched key '{skill_id}' vs spec.skill_id '{spec.skill_id}'")
        errors.extend(spec.validate())
        # Surface hard constraints and error-severity standards so callers get
        # a complete picture of each spec's enforcement surface.
        hard = spec.get_hard_constraints()
        error_standards = spec.get_error_standards()
        if hard:
            logger.debug(
                "Skill '%s' has %d hard constraint(s): %s",
                skill_id,
                len(hard),
                [c.id for c in hard],
            )
        if error_standards:
            logger.debug(
                "Skill '%s' has %d error-severity standard(s): %s",
                skill_id,
                len(error_standards),
                [s.id for s in error_standards],
            )
    return errors


def get_skill_validation_detail(skill_id: str) -> dict[str, Any] | None:
    """Return a structured validation detail report for a single skill spec.

    Collects hard constraints, error-severity standards, and per-category
    breakdowns from the spec's helper methods so callers can inspect
    enforcement rules without re-implementing the filtering logic.

    Args:
        skill_id: The skill identifier (foreman, worker, or inspector).

    Returns:
        Dict with keys ``hard_constraints``, ``error_standards``,
        ``standards_by_category``, and ``constraints_by_category`` — or
        None if the skill is not found.
    """
    spec = SKILL_REGISTRY.get(skill_id)
    if spec is None:
        return None

    # Collect all unique categories present in the spec
    standard_categories = {s.category for s in spec.standards}
    constraint_categories = {c.category for c in spec.constraints}

    return {
        "hard_constraints": [
            {"id": c.id, "description": c.description, "category": c.category} for c in spec.get_hard_constraints()
        ],
        "error_standards": [
            {"id": s.id, "description": s.description, "category": s.category} for s in spec.get_error_standards()
        ],
        "standards_by_category": {
            cat: [
                {"id": s.id, "description": s.description, "severity": s.severity}
                for s in spec.get_standards_by_category(cat)
            ]
            for cat in sorted(standard_categories)
        },
        "constraints_by_category": {
            cat: [
                {"id": c.id, "description": c.description, "enforcement": c.enforcement}
                for c in spec.get_constraints_by_category(cat)
            ]
            for cat in sorted(constraint_categories)
        },
    }


def auto_populate_from_agents() -> dict[str, SkillSpec]:
    """Auto-derive SkillSpecs from all MultiModeAgent subclasses.

    For each agent class, calls ``to_skill_spec()`` to generate a baseline
    spec.  If a hand-written spec already exists in SKILL_REGISTRY, the
    auto-derived fields (modes, capabilities) are merged *under* the
    hand-written spec — i.e., hand-written standards, constraints, and
    schemas always take precedence.

    Returns:
        Dict of skill_id -> merged SkillSpec for all discovered agents.
    """
    try:
        from vetinari.agents.multi_mode_agent import MultiModeAgent
    except ImportError:
        logger.debug("MultiModeAgent not available for auto-population")
        return {}

    _import_agent_modules_for_auto_population()

    result: dict[str, SkillSpec] = {}

    for subclass in MultiModeAgent.__subclasses__():
        if not subclass.MODES:
            continue
        try:
            auto_spec = subclass.to_skill_spec()
        except Exception as exc:
            logger.warning("Failed to derive SkillSpec from %s: %s", subclass.__name__, exc)
            continue

        existing = SKILL_REGISTRY.get(auto_spec.skill_id)
        if existing is not None:
            # Merge: auto-derived modes/capabilities fill gaps,
            # hand-written standards/constraints/schemas always win
            merged_modes = existing.modes or auto_spec.modes
            merged_caps = existing.capabilities or auto_spec.capabilities
            merged = SkillSpec(
                skill_id=existing.skill_id,
                name=existing.name,
                description=existing.description,
                version=existing.version,
                agent_type=existing.agent_type or auto_spec.agent_type,
                modes=merged_modes,
                capabilities=merged_caps,
                input_schema=existing.input_schema or auto_spec.input_schema,
                output_schema=existing.output_schema or auto_spec.output_schema,
                max_tokens=existing.max_tokens,
                max_retries=existing.max_retries,
                timeout_seconds=existing.timeout_seconds,
                max_cost_usd=existing.max_cost_usd,
                requires_tools=existing.requires_tools,
                min_verification_score=existing.min_verification_score,
                require_schema_validation=existing.require_schema_validation,
                forbidden_patterns=existing.forbidden_patterns,
                standards=existing.standards,
                guidelines=existing.guidelines,
                constraints=existing.constraints,
                author=existing.author,
                tags=list(set(existing.tags + auto_spec.tags)),
                enabled=existing.enabled,
                deprecated=existing.deprecated,
                deprecated_by=existing.deprecated_by,
            )
            result[merged.skill_id] = merged
        else:
            result[auto_spec.skill_id] = auto_spec

    return result


def _import_agent_modules_for_auto_population() -> None:
    """Import agent modules so MultiModeAgent subclasses are registered."""
    import importlib

    agent_modules = [
        "vetinari.agents.planner_agent",
        "vetinari.agents.builder_agent",
        "vetinari.agents.consolidated.worker_agent",
        "vetinari.agents.consolidated.quality_agent",
        "vetinari.agents.consolidated.operations_agent",
        "vetinari.agents.consolidated.oracle_agent",
        "vetinari.agents.consolidated.researcher_agent",
    ]
    for mod_name in agent_modules:
        try:
            importlib.import_module(mod_name)
        except ImportError:
            logger.debug("Could not import %s for auto-population", mod_name)


# ---------------------------------------------------------------------------
# Disk-aware convenience functions (thin wrappers around get_registry())
# ---------------------------------------------------------------------------


def list_registry_skills() -> list[dict[str, Any]]:
    """List all skills from the disk-aware registry.

    Returns:
        List of skill metadata dicts (disk + programmatic merged).
    """
    return get_registry().list_skills()


def get_registry_skill(skill_id: str) -> dict[str, Any] | None:
    """Get skill metadata by ID from the disk-aware registry.

    Args:
        skill_id: The skill identifier.

    Returns:
        Skill metadata dict, or None if not found.
    """
    return get_registry().get_skill(skill_id)


def get_skill_manifest(skill_id: str) -> dict[str, Any] | None:
    """Get the full manifest for a skill from the disk-aware registry.

    Args:
        skill_id: The skill identifier.

    Returns:
        Manifest dict, or None if not found.
    """
    return get_registry().get_skill_manifest(skill_id)


def get_agent_skills(
    agent_id: str,
    env: str | None = None,
) -> list[dict[str, Any]]:
    """Get skills mapped to a specific agent from the disk-aware registry.

    Args:
        agent_id: The agent id or canonical agent type.
        env: Optional environment name for environment-specific overrides.

    Returns:
        List of skill mapping dicts.
    """
    return get_registry().get_agent_skills(agent_id, env)


def get_context(context_id: str) -> dict[str, Any] | None:
    """Get a sample context by ID from the disk-aware registry.

    Args:
        context_id: The context identifier.

    Returns:
        Context dict, or None if not found.
    """
    return get_registry().get_context(context_id)


def get_contexts_for_skill(skill_id: str) -> list[dict[str, Any]]:
    """Get all sample contexts for a specific skill from the disk-aware registry.

    Args:
        skill_id: The skill identifier.

    Returns:
        List of context dicts.
    """
    return get_registry().get_contexts_for_skill(skill_id)


def validate_registry() -> dict[str, list[str]]:
    """Validate the disk-aware registry and return errors and warnings.

    Returns:
        Dict with ``'errors'`` and ``'warnings'`` lists.
    """
    return get_registry().validate()


def search_skills(query: str) -> list[dict[str, Any]]:
    """Search skills by name, description, tags, or capabilities.

    Args:
        query: Case-insensitive search string.

    Returns:
        List of matching skill metadata dicts.
    """
    return get_registry().search_skills(query)


def list_agents() -> list[str]:
    """List all registered agent types from the disk-aware registry.

    Returns:
        Sorted list of agent type strings.
    """
    return get_registry().list_agents()


def get_workflow_template(template_name: str) -> dict[str, Any] | None:
    """Get a predefined workflow template by name.

    Args:
        template_name: The workflow template identifier.

    Returns:
        Template dict, or None if the template is not registered.
    """
    from vetinari.skills.skill_workflows import get_workflow_template as _get_workflow_template

    return _get_workflow_template(template_name)


def list_workflow_templates() -> list[str]:
    """List the names of all available workflow templates.

    Returns:
        List of workflow template names.
    """
    from vetinari.skills.skill_workflows import list_workflow_templates as _list_workflow_templates

    return _list_workflow_templates()


def get_orchestration_config() -> dict[str, Any]:
    """Get orchestration feature flags from the central registry.

    Returns:
        Orchestration feature flag mapping, or an empty dict.
    """
    from vetinari.skills.skill_workflows import get_orchestration_config as _get_orchestration_config

    return _get_orchestration_config()


def get_skill_dependencies(skill_id: str) -> list[str]:
    """Get the list of skill ids that a given skill depends on.

    Args:
        skill_id: The skill identifier.

    Returns:
        List of dependency skill ids.
    """
    from vetinari.skills.skill_workflows import get_skill_dependencies as _get_skill_dependencies

    return _get_skill_dependencies(skill_id)


def get_skills_for_workflow_stage(stage_purpose: str) -> list[dict[str, Any]]:
    """Find skills matching a specific workflow stage purpose string.

    Args:
        stage_purpose: Substring to match against workflow stage purpose text.

    Returns:
        List of matching workflow stage records.
    """
    from vetinari.skills.skill_workflows import get_skills_for_workflow_stage as _get_skills_for_workflow_stage

    return _get_skills_for_workflow_stage(stage_purpose)
