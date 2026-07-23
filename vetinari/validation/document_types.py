"""Document type definitions for the quality evaluation framework.

Defines the canonical set of document types that Vetinari produces or
evaluates, along with per-type metadata used by the quality rubric and
LLM-as-judge evaluator.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from vetinari.config_paths import resolve_config_path

logger = logging.getLogger(__name__)


def _get_config_dir() -> Path:
    """Return the config directory path, computed at call time."""
    return resolve_config_path()


class DocumentType(Enum):
    """Canonical document types produced by Vetinari agents."""

    ADR = "adr"
    API_REFERENCE = "api_reference"
    CHANGELOG = "changelog"
    CODE_COMMENT = "code_comment"
    COMMIT_MESSAGE = "commit_message"
    DEVELOPER_GUIDE = "developer_guide"
    ERROR_MESSAGE = "error_message"
    PLAN = "plan"
    README = "readme"
    RESEARCH_REPORT = "research_report"


@dataclass
class DocumentProfile:
    """Quality evaluation profile for a specific document type.

    Loaded from config/document_profiles.yaml.  Each profile specifies
    which quality dimensions matter most (via weights) and the minimum
    overall score required to pass.

    Args:
        doc_type: The document type this profile applies to.
        description: Human-readable description of the document type.
        min_score: Minimum weighted score to pass quality evaluation.
        dimension_weights: Mapping of dimension name to importance weight (0.0-1.0).
        rules: Type-specific evaluation rules as free-text strings.
    """

    doc_type: str
    description: str = ""
    min_score: float = 0.60
    dimension_weights: dict[str, float] = field(default_factory=dict)
    rules: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"DocumentProfile(doc_type={self.doc_type!r}, min_score={self.min_score!r}, "
            f"dimensions={len(self.dimension_weights)})"
        )


def load_document_profiles(config_path: Path | None = None) -> dict[str, DocumentProfile]:
    """Load document profiles from the YAML configuration file.

    Args:
        config_path: Path to document_profiles.yaml.  Defaults to
            ``config/document_profiles.yaml`` relative to the project root.

    Returns:
        Mapping of document type name to DocumentProfile objects.

    Raises:
        ValueError: If the profile file is structurally invalid.
    """
    path = config_path or (_get_config_dir() / "document_profiles.yaml")
    if not path.exists():
        return {}

    with Path(path).open(encoding="utf-8") as f:
        loaded = yaml.safe_load(f)
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Document profiles config must be a mapping at {path}")
    raw: dict[str, Any] = loaded

    default_section = raw.get("default", {})
    if not isinstance(default_section, dict):
        default_section = {}
    default_weights: dict[str, float] = default_section.get("dimension_weights", {})
    default_min = float(default_section.get("min_score", 0.60))

    profiles: dict[str, DocumentProfile] = {}

    raw_profiles = raw.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise ValueError(f"Document profiles config has non-mapping profiles at {path}")

    for name, data in raw_profiles.items():
        if not isinstance(data, dict):
            logger.warning("Skipping non-mapping document profile %s", name)
            continue
        weights = dict(default_weights)
        weights.update(data.get("dimension_weights", {}))
        profiles[name] = DocumentProfile(
            doc_type=name,
            description=data.get("description", ""),
            min_score=float(data.get("min_score", default_min)),
            dimension_weights=weights,
            rules=data.get("rules", []),
        )

    return profiles


_profiles: dict[str, DocumentProfile] | None = None
_profiles_lock = threading.Lock()


def get_document_profiles() -> dict[str, DocumentProfile]:
    """Return cached document profiles, loading from YAML on first call.

    Returns:
        Mapping of document type name to its DocumentProfile, loaded from
        config/document_profiles.yaml. Returns an empty dict if the config
        file is missing.
    """
    global _profiles
    if _profiles is None:
        with _profiles_lock:
            if _profiles is None:
                _profiles = load_document_profiles()
    return _profiles


def get_profile_for_type(doc_type: str | DocumentType) -> DocumentProfile:
    """Look up the profile for a document type.

    Falls back to a default profile if the type is not configured.

    Args:
        doc_type: Document type name or enum value.

    Returns:
        The matching DocumentProfile, or a default profile.
    """
    key = doc_type.value if isinstance(doc_type, DocumentType) else doc_type
    profiles = get_document_profiles()
    if key in profiles:
        return profiles[key]
    # Return a generic default profile
    return DocumentProfile(doc_type=key, description="Unknown document type")
