"""Vetinari Agent Prompt Loader.

Loads agent system prompts from ``vetinari/config/agents/`` at runtime.

**Per-mode loading** (preferred, 10-20x smaller prompts for 7B models):
    Each agent has a directory with ``identity.md`` and ``mode-{name}.md`` files.
    Only the identity + current mode file are loaded per request.

**Monolithic fallback**:
    Falls back to the single ``{agent}.md`` file with YAML frontmatter and
    ## sections if the per-mode directory does not exist.

Both paths use mtime-based cache invalidation for hot-reload.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from vetinari.context.window_manager import count_tokens
from vetinari.utils.frontmatter import FrontmatterError, parse_frontmatter

logger = logging.getLogger(__name__)


# Root of the project — resolved relative to this file's location
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Agent spec directory
_AGENTS_DIR = _PROJECT_ROOT / "vetinari" / "config" / "agents"

# Sections extracted at runtime from monolithic files (order matters)
_RUNTIME_SECTIONS = ("Identity", "Modes", "Project Standards")

# Cache: cache_key -> (mtime, content_string)
_prompt_cache: dict[str, tuple[float, str]] = {}

# Section cache: agent_name -> (mtime, parsed_sections)
_section_cache: dict[str, tuple[float, dict[str, str]]] = {}

# Section header regex: ## Title
_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_MODE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

# -- Per-mode file loading (preferred) ----------------------------------------


def _load_per_mode(agent_name: str, mode: str | None = None) -> str | None:
    """Try to load prompt from per-mode directory structure.

    Looks for ``config/agents/{agent}/identity.md`` and optionally
    ``config/agents/{agent}/mode-{mode}.md``.

    Args:
        agent_name: Base agent name (e.g. "worker", "foreman").
        mode: Optional mode name (e.g. "build", "code_review").

    Returns:
        Assembled prompt string, or None if per-mode directory does not exist.

    Raises:
        ValueError: If the agent name contains path traversal sequences that
            would place the directory outside the configured agents directory.
    """
    agent_dir = (_AGENTS_DIR / agent_name).resolve()
    if not agent_dir.is_relative_to(_AGENTS_DIR.resolve()):
        raise ValueError(f"Agent name contains path traversal: {agent_name}")
    identity_file = agent_dir / "identity.md"

    if not identity_file.exists():
        return None  # Fall back to monolithic loading

    if mode and not _MODE_NAME_RE.fullmatch(mode):
        raise ValueError(f"Invalid prompt mode name: {mode}")

    # Check cache
    cache_key = f"{agent_name}:{mode or 'none'}"
    mtime = identity_file.stat().st_mtime

    # Also check mode file mtime if applicable
    mode_file = (agent_dir / f"mode-{mode}.md").resolve() if mode else None
    if mode_file and not mode_file.is_relative_to(agent_dir):
        raise ValueError(f"Prompt mode path escapes agent directory: {mode}")
    if mode_file and mode_file.exists():
        mtime = max(mtime, mode_file.stat().st_mtime)

    cached = _prompt_cache.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]

    # Load identity
    parts: list[str] = []
    identity_text = identity_file.read_text(encoding="utf-8").strip()
    if identity_text:
        parts.append(identity_text)

    # Load mode-specific file
    if mode_file and mode_file.exists():
        mode_text = mode_file.read_text(encoding="utf-8").strip()
        if mode_text:
            parts.append(mode_text)
    elif mode_file:
        # Mode was requested but the mode file doesn't exist — return None so
        # the caller falls through to the monolithic file which has all sections.
        return None

    result = "\n\n".join(parts)

    _prompt_cache[cache_key] = (mtime, result)
    logger.debug(
        "Loaded per-mode prompt for %s/%s (%d chars, %d tokens)",
        agent_name,
        mode or "identity-only",
        len(result),
        count_tokens(result),
    )

    return result


# -- Monolithic file loading (fallback) ----------------------------------------


def _agent_type_to_filename(agent_type_value: str) -> str:
    """Map an AgentType value to its markdown filename.

    Args:
        agent_type_value: The AgentType enum value string (e.g. "WORKER",
            "FOREMAN").

    Returns:
        The base filename without extension (e.g. "worker", "foreman").
    """
    return agent_type_value.lower()


def _resolve_agent_filepath(agent_name: str) -> Path | None:
    """Resolve the filepath for a monolithic agent spec.

    Args:
        agent_name: Base name of the agent (e.g. "foreman", "worker").

    Returns:
        Path to the agent spec file, or None if not found.
    """
    filepath = _AGENTS_DIR / f"{agent_name}.md"
    if filepath.exists():
        return filepath
    return None


def _extract_sections(body: str) -> dict[str, str]:
    """Extract all ## sections from a markdown body.

    Args:
        body: Markdown text without frontmatter.

    Returns:
        Dict mapping section title to section content (without the header line).
    """
    sections: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(body))

    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections[title] = body[start:end].strip()

    return sections


def _load_monolithic(agent_name: str) -> dict[str, str]:
    """Load an agent markdown file and cache its sections.

    Args:
        agent_name: Base name of the agent (e.g. "foreman", "worker").

    Returns:
        Dict mapping section title to section content for runtime sections.
    """
    filepath = _resolve_agent_filepath(agent_name)
    if filepath is None:
        logger.warning("Agent prompt file not found: %s (checked %s)", agent_name, _AGENTS_DIR)
        return {}

    mtime = filepath.stat().st_mtime

    cached = _section_cache.get(agent_name)
    if cached and cached[0] == mtime:
        return cached[1]

    content = filepath.read_text(encoding="utf-8")
    try:
        metadata, body = parse_frontmatter(content, strict=True)
    except FrontmatterError as exc:
        logger.warning("Skipping agent prompt with invalid frontmatter %s: %s", filepath, exc)
        _section_cache[agent_name] = (mtime, {})
        return {}

    if metadata.get("runtime") is False:
        logger.debug("Skipping non-runtime agent file: %s", filepath)
        _section_cache[agent_name] = (mtime, {})
        return {}

    all_sections = _extract_sections(body)

    runtime_sections = {title: text for title, text in all_sections.items() if title in _RUNTIME_SECTIONS}

    # Synthesise a virtual "Modes" section from group-based headers
    if "Modes" not in runtime_sections:
        mode_parts: list[str] = []
        for title, text in all_sections.items():
            if "###" in text and title not in _RUNTIME_SECTIONS:
                mode_parts.append(text)
        if mode_parts:
            runtime_sections["Modes"] = "\n\n".join(mode_parts)

    _section_cache[agent_name] = (mtime, runtime_sections)
    logger.debug(
        "Loaded monolithic agent prompt for %s (%d sections) from %s",
        agent_name,
        len(runtime_sections),
        filepath,
    )
    return runtime_sections


def _extract_mode_section(modes_text: str, mode: str) -> str:
    """Extract a specific mode's subsection from the Modes section.

    Mode subsections use ### headers (e.g. ``### build``).

    Args:
        modes_text: Full text of the ## Modes section.
        mode: The mode name to extract (e.g. "build", "code_review").

    Returns:
        The mode-specific text, or empty string if not found.
    """
    pattern = re.compile(
        rf"^###\s+`?{re.escape(mode)}`?\s*$",
        re.MULTILINE,
    )
    match = pattern.search(modes_text)
    if not match:
        return ""

    start = match.end()
    next_header = re.search(r"^###\s+", modes_text[start:], re.MULTILINE)
    end = start + next_header.start() if next_header else len(modes_text)
    return modes_text[start:end].strip()


# -- Public API ----------------------------------------------------------------


def load_agent_prompt(
    agent_type: Any,
    mode: str | None = None,
) -> str:
    """Load the runtime prompt for an agent, preferring per-mode files.

    **Per-mode path** (preferred): Loads ``identity.md`` + ``mode-{mode}.md``
    from the agent's directory. Produces ~25 lines per task instead of ~211.

    **Monolithic path** (fallback): Loads the single ``{agent}.md`` file and
    extracts Identity + Project Standards + mode-specific section.

    Args:
        agent_type: An AgentType enum value or string.
        mode: Optional mode name to include mode-specific instructions.

    Returns:
        Assembled prompt text, or empty string if the file is not found.
    """
    type_value = agent_type.value if hasattr(agent_type, "value") else str(agent_type)
    agent_name = _agent_type_to_filename(type_value)

    # Try per-mode directory first (10-20x smaller prompts)
    per_mode_result = _load_per_mode(agent_name, mode)
    if per_mode_result is not None:
        return per_mode_result

    # Fall back to monolithic file
    sections = _load_monolithic(agent_name)
    if not sections:
        return ""

    parts: list[str] = []

    identity = sections.get("Identity", "")
    if identity:
        parts.append(f"## Identity\n\n{identity}")

    project_standards = sections.get("Project Standards", "")
    if project_standards:
        parts.append(f"## Project Standards\n\n{project_standards}")

    # Mode-specific section
    if mode:
        modes_text = sections.get("Modes", "")
        if modes_text:
            mode_section = _extract_mode_section(modes_text, mode)
            if mode_section:
                parts.append(f"## Current Mode: {mode}\n\n{mode_section}")

    return "\n\n".join(parts)


def check_prompt_budget(
    system_prompt: str,
    task_prompt: str,
    max_tokens: int,
    n_ctx: int,
) -> dict[str, Any]:
    """Check whether a prompt + response fits within the model's context window.

    Uses AM Engine token counts and verifies that
    ``system_tokens + task_tokens + max_tokens <= n_ctx``.

    Args:
        system_prompt: The system prompt text.
        task_prompt: The user/task prompt text.
        max_tokens: Maximum tokens reserved for the response.
        n_ctx: Model context window size in tokens.

    Returns:
        Dict with keys: fits (bool), system_tokens, task_tokens, max_tokens,
        total_tokens, n_ctx, headroom.
    """
    system_tokens = count_tokens(system_prompt)
    task_tokens = count_tokens(task_prompt)
    total = system_tokens + task_tokens + max_tokens
    headroom = n_ctx - total

    result = {
        "fits": total <= n_ctx,
        "system_tokens": system_tokens,
        "task_tokens": task_tokens,
        "max_tokens": max_tokens,
        "total_tokens": total,
        "n_ctx": n_ctx,
        "headroom": headroom,
    }

    if not result["fits"]:
        logger.warning(
            "Prompt budget exceeded: %d tokens > %d n_ctx (system=%d, task=%d, response=%d)",
            total,
            n_ctx,
            system_tokens,
            task_tokens,
            max_tokens,
        )

    return result


def clear_prompt_cache() -> None:
    """Clear all prompt caches, forcing reload on next access."""
    _prompt_cache.clear()
    _section_cache.clear()


def get_cached_agent_names() -> list[str]:
    """Return list of agent names currently in the cache.

    Returns:
        List of cached agent name strings.
    """
    # Combine per-mode and section cache keys
    names: set[str] = set()
    names.update(key.split(":")[0] for key in _prompt_cache)
    names.update(_section_cache.keys())
    return sorted(names)
