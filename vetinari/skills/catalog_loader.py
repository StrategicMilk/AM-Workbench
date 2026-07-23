"""Skill Catalog Loader.

Discovers and parses all SKILL.md files from the catalog directory,
extracting YAML frontmatter into typed CatalogEntry dataclasses.
Provides lookup by agent, mode, capability, and tag.

Portable naming contract: a skill name is the same across every host that
loads it. The loader rejects names that collide with built-ins or that
violate the portable-naming contract so two skills cannot register the
same identifier — see ``docs/product-thesis.md`` Skills and Portable Naming.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vetinari.security.fail_closed import UntrustedInputError, sanitize_untrusted_text
from vetinari.utils.frontmatter import parse_frontmatter
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


# Catalog directory relative to this file
_CATALOG_ROOT = Path(__file__).parent / "catalog"
_PORTABLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Module-level lazy cache — populated on first access
_CATALOG: dict[str, CatalogEntry] | None = None
_CATALOG_LOCK = threading.Lock()


class SkillCatalogError(ValueError):
    """Raised when a skill catalog violates portable naming contracts."""


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """A single skill catalog entry parsed from a SKILL.md file."""

    skill_id: str  # "{agent}/{skill-name}" e.g. "worker/feature-implementation"
    name: str
    description: str
    mode: str
    agent: str  # foreman, worker, inspector
    version: str
    capabilities: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    file_path: str = ""

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"CatalogEntry(skill_id={self.skill_id!r}, agent={self.agent!r}, mode={self.mode!r})"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dictionary for API responses and JSON export."""
        return dataclass_to_dict(self)


def load_catalog(catalog_root: Path | None = None) -> dict[str, CatalogEntry]:
    """Walk the catalog directory and load all SKILL.md files.

    Each SKILL.md file's YAML frontmatter is extracted and converted into a
    :class:`CatalogEntry`. The ``skill_id`` is derived from the path as
    ``"{agent}/{skill-name}"``.

    Args:
        catalog_root: Root directory to search. Defaults to the bundled
            ``catalog/`` directory next to this module.

    Returns:
        Mapping from ``skill_id`` to :class:`CatalogEntry` for every
        successfully parsed SKILL.md file.

    Raises:
        SkillCatalogError: If a catalog path uses a non-portable agent or skill
            name, or if two files derive the same skill ID.
    """
    root = catalog_root if catalog_root is not None else _CATALOG_ROOT
    catalog: dict[str, CatalogEntry] = {}

    if not root.is_dir():
        logger.warning("Catalog root does not exist: %s", root)
        return catalog

    for skill_md in root.rglob("SKILL.md"):
        # Derive skill_id from the two path components above SKILL.md:
        # catalog/{agent}/{skill-name}/SKILL.md  →  "{agent}/{skill-name}"
        parts = skill_md.relative_to(root).parts
        if len(parts) < 3:
            logger.warning("Unexpected SKILL.md path structure: %s; skipping", skill_md)
            continue

        agent_dir, skill_dir = parts[0], parts[1]
        for label, value in (("agent", agent_dir), ("skill", skill_dir)):
            if not _PORTABLE_NAME_RE.fullmatch(value):
                raise SkillCatalogError(f"{skill_md}: non-portable {label} name {value!r}")
        skill_id = f"{agent_dir}/{skill_dir}"
        if skill_id in catalog:
            raise SkillCatalogError(f"{skill_md}: duplicate skill id {skill_id!r}")

        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read %s: %s", skill_md, exc)
            continue

        frontmatter, _ = parse_frontmatter(content)
        if not frontmatter:
            logger.warning("Skipping %s — empty or unparseable frontmatter", skill_md)
            continue

        try:
            name = sanitize_untrusted_text(str(frontmatter.get("name", skill_dir)), max_length=120)
            description = sanitize_untrusted_text(str(frontmatter.get("description", "")), max_length=1_000)
            mode = sanitize_untrusted_text(str(frontmatter.get("mode", "")), max_length=80)
            agent = sanitize_untrusted_text(str(frontmatter.get("agent", agent_dir)), max_length=80)
            capabilities = [
                sanitize_untrusted_text(str(value), max_length=120)
                for value in list(frontmatter.get("capabilities") or [])
            ]
            tags = [
                sanitize_untrusted_text(str(value), max_length=120) for value in list(frontmatter.get("tags") or [])
            ]
        except UntrustedInputError as exc:
            raise SkillCatalogError(f"{skill_md}: unsafe skill frontmatter") from exc
        for label, value in (("agent", agent), ("mode", mode)):
            if value and not _PORTABLE_NAME_RE.fullmatch(value):
                raise SkillCatalogError(f"{skill_md}: non-portable frontmatter {label} {value!r}")

        entry = CatalogEntry(
            skill_id=skill_id,
            name=name,
            description=description,
            mode=mode,
            agent=agent,
            version=str(frontmatter.get("version", "1.0.0")),
            capabilities=capabilities,
            tags=tags,
            file_path=skill_md.relative_to(root).as_posix(),
        )
        catalog[skill_id] = entry

    logger.info("Loaded %d skill catalog entries from %s", len(catalog), root)
    return catalog


def _ensure_loaded() -> dict[str, CatalogEntry]:
    """Return the module-level catalog, loading it on first access.

    Returns:
        The populated catalog mapping.
    """
    global _CATALOG
    if _CATALOG is None:
        with _CATALOG_LOCK:
            if _CATALOG is None:
                _CATALOG = load_catalog()
    return _CATALOG


def get_catalog_by_agent(
    agent: str,
    catalog: dict[str, CatalogEntry] | None = None,
) -> list[CatalogEntry]:
    """Return all catalog entries for a specific agent.

    Args:
        agent: Agent name to filter by (e.g. ``"worker"``, ``"foreman"``,
            ``"inspector"``).
        catalog: Optional pre-loaded catalog mapping. When ``None`` the
            module-level cache is used, loading it if necessary.

    Returns:
        List of :class:`CatalogEntry` objects whose ``agent`` field matches
        the given name. Empty list if none match.
    """
    entries = catalog if catalog is not None else _ensure_loaded()
    return [e for e in entries.values() if e.agent == agent]


def get_catalog_by_capability(
    capability: str,
    catalog: dict[str, CatalogEntry] | None = None,
) -> list[CatalogEntry]:
    """Return all catalog entries that declare a specific capability.

    Args:
        capability: Capability string to search for (e.g.
            ``"feature_implementation"``).
        catalog: Optional pre-loaded catalog mapping. When ``None`` the
            module-level cache is used, loading it if necessary.

    Returns:
        List of :class:`CatalogEntry` objects whose ``capabilities`` list
        contains the given capability. Empty list if none match.
    """
    entries = catalog if catalog is not None else _ensure_loaded()
    return [e for e in entries.values() if capability in e.capabilities]


def get_catalog_by_tag(
    tag: str,
    catalog: dict[str, CatalogEntry] | None = None,
) -> list[CatalogEntry]:
    """Return all catalog entries that carry a specific tag.

    Args:
        tag: Tag string to search for (e.g. ``"build"``).
        catalog: Optional pre-loaded catalog mapping. When ``None`` the
            module-level cache is used, loading it if necessary.

    Returns:
        List of :class:`CatalogEntry` objects whose ``tags`` list contains
        the given tag. Empty list if none match.
    """
    entries = catalog if catalog is not None else _ensure_loaded()
    return [e for e in entries.values() if tag in e.tags]
