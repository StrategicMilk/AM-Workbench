"""Template loader for versioned agent prompt templates.

Loads JSON template files from the templates/ directory, organized by version and
agent type. The default runtime surface is the Foreman, Worker, Inspector
pipeline; retired consolidated names remain explicit compatibility aliases only.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


BASE = Path(__file__).resolve().parent.parent / "templates"

# Maps canonical agent names and compatibility aliases to their template files.
_AGENT_FILE_MAP: dict[str, str] = {
    # Canonical runtime roles.
    "foreman": "planner.json",
    "worker": "builder.json",
    "inspector": "quality.json",
    # Legacy consolidated names.
    "planner": "planner.json",
    "researcher": "researcher.json",
    "oracle": "oracle.json",
    "builder": "builder.json",
    "quality": "quality.json",
    "operations": "operations.json",
    # Legacy agent names to consolidated equivalents.
    "explorer": "researcher.json",
    "librarian": "researcher.json",
    "evaluator": "quality.json",
    "synthesizer": "operations.json",
    "ui_planner": "planner.json",
}

# Compatibility export for older callers that inspect the previous consolidated set.
_CONSOLIDATED_AGENTS = ("planner", "researcher", "oracle", "builder", "quality", "operations")
_CANONICAL_AGENTS = ("foreman", "worker", "inspector")


class TemplateLoader:
    """Load versioned prompt templates for Vetinari agents.

    Templates are stored as JSON files under templates/{version}/{agent}.json. A
    versions.json manifest lists available versions. Unfiltered loads return only
    canonical Foreman, Worker, and Inspector templates; compatibility aliases are
    loaded only when requested by name.
    """

    def __init__(self, base_path: Path | None = None):
        self.base_path = base_path or BASE

    def list_versions(self) -> list[str]:
        """Return available template versions from the manifest.

        Returns:
            List of version strings (e.g. ["v1", "v2"]) read from
            versions.json; falls back to ["v1"] if the manifest is missing
            or unreadable.
        """
        manifest = self.base_path / "versions.json"
        if not manifest.exists():
            return ["v1"]
        try:
            with Path(manifest).open(encoding="utf-8") as f:
                data = json.load(f)
                return data.get("versions", ["v1"])
        except Exception:
            logger.warning("Failed to read template versions manifest")
            return ["v1"]

    def load_templates_for_agent(self, version: str, agent_type: str) -> list[dict]:
        """Load templates for a specific agent type and version.

        Args:
            version: Template version string (e.g. "v1").
            agent_type: Canonical agent name or explicit compatibility alias.

        Returns:
            List of template dicts, or empty list if not found.
        """
        filename = _AGENT_FILE_MAP.get(agent_type)
        if not filename:
            return []
        path = self.base_path / version / filename
        if not path.exists():
            return []
        try:
            with Path(path).open(encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            logger.warning("Failed to load templates from %s", path)
            return []

    def load_templates(self, version: str | None = None, agent_type: str | None = None) -> list[dict]:
        """Load templates, optionally filtered by agent type.

        Args:
            version: Template version. Uses default if not specified.
            agent_type: If provided, load only this agent's templates.

        Returns:
            List of template dicts from matching agent files.
        """
        ver = version or self.default_version()
        if agent_type:
            return self.load_templates_for_agent(ver, agent_type)
        templates: list[dict] = []
        for atype in _CANONICAL_AGENTS:
            templates.extend(self.load_templates_for_agent(ver, atype))
        return templates

    def default_version(self) -> str:
        """Return the first available version, defaulting to 'v1'.

        Returns:
            First entry from list_versions(), or 'v1' if the list is empty.
        """
        versions = self.list_versions()
        return versions[0] if versions else "v1"


_template_loader: TemplateLoader | None = None
_template_loader_lock = threading.Lock()


def get_template_loader() -> TemplateLoader:
    """Return the process singleton TemplateLoader, creating it on first use.

    Returns:
        Shared TemplateLoader instance.
    """
    global _template_loader
    if _template_loader is None:
        with _template_loader_lock:
            if _template_loader is None:
                _template_loader = TemplateLoader()
    return _template_loader


def __getattr__(name: str) -> object:
    """Preserve lazy compatibility for legacy template_loader imports."""
    if name == "template_loader":
        return get_template_loader()
    raise AttributeError(name)
