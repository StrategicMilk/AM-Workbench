"""Glossary loader - reads `config/glossary.yaml` once on first call and caches the parsed entries for the lifetime of the process. Step: Config -> **Glossary** -> API."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


_REQUIRED_FIELDS = frozenset({"term", "short", "long", "see_also", "category"})
_ALLOWED_CATEGORIES = frozenset({
    "agent",
    "model",
    "planning",
    "safety",
    "training",
    "lifecycle",
    "ui",
    "infra",
    "workbench",
})
_GLOSSARY_CACHE: list[dict[str, Any]] | None = None
_GLOSSARY_LOCK = threading.Lock()
# Path is resolved lazily inside load_glossary() — not at module import time.
# Anti-pattern guard: "Module-level I/O" — no disk access at import.
_GLOSSARY_PATH: Path | None = None


def _get_glossary_path() -> Path:
    """Resolve the glossary YAML path relative to this file.

    Returns:
        Absolute path to ``config/glossary.yaml``.
    """
    global _GLOSSARY_PATH
    if _GLOSSARY_PATH is None:
        _GLOSSARY_PATH = Path(__file__).resolve().parents[2] / "config" / "glossary.yaml"
    return _GLOSSARY_PATH


def load_glossary() -> list[dict[str, Any]]:
    """Return cached glossary entries loaded from ``config/glossary.yaml``.

    The first call reads and validates the YAML file. Subsequent calls return
    the same cached list for the lifetime of the process.

    Returns:
        Validated glossary entries.

    Raises:
        ValueError: If the YAML shape, entry fields, categories, or see-also
            references are invalid, or if the glossary file cannot be found.
    """
    global _GLOSSARY_CACHE
    if _GLOSSARY_CACHE is not None:
        return _GLOSSARY_CACHE

    with _GLOSSARY_LOCK:
        if _GLOSSARY_CACHE is not None:
            return _GLOSSARY_CACHE

        path = _get_glossary_path()
        if not path.exists():
            raise ValueError(f"glossary YAML not found at {path}")
        logger.debug("Loading glossary from %s", path)
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        _GLOSSARY_CACHE = _validate_entries(raw)
        return _GLOSSARY_CACHE


def get_term(term: str) -> dict[str, Any] | None:
    """Return one glossary entry by case-insensitive term lookup.

    Args:
        term: Term name to find.

    Returns:
        Matching glossary entry, or ``None`` when the term is unknown.
    """
    wanted = term.casefold()
    for entry in load_glossary():
        if str(entry["term"]).casefold() == wanted:
            return entry
    return None


def _validate_entries(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("glossary.yaml must contain a list of entries")

    entries: list[dict[str, Any]] = []
    terms: set[str] = set()
    for index, entry in enumerate(raw, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"glossary entry {index} must be an object")
        missing = sorted(_REQUIRED_FIELDS - set(entry))
        if missing:
            raise ValueError(f"glossary entry {index} missing required field(s): {', '.join(missing)}")
        if not isinstance(entry["see_also"], list):
            raise ValueError(f"glossary entry {entry['term']} field see_also must be a list")
        if entry["category"] not in _ALLOWED_CATEGORIES:
            raise ValueError(f"glossary entry {entry['term']} has invalid category {entry['category']!r}")
        term_name = str(entry["term"])
        if term_name in terms:
            raise ValueError(f"duplicate glossary term {term_name}")
        terms.add(term_name)
        entries.append({
            "term": term_name,
            "short": str(entry["short"]),
            "long": str(entry["long"]),
            "see_also": [str(item) for item in entry["see_also"]],
            "category": str(entry["category"]),
        })

    for entry in entries:
        for reference in entry["see_also"]:
            if reference not in terms:
                raise ValueError(f"glossary term {entry['term']} references unknown see_also term {reference}")

    return entries


__all__ = ["get_term", "load_glossary"]
