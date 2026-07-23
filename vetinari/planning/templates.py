"""Prompt template lookup helpers."""

from __future__ import annotations

from pathlib import Path

_PROMPT_ROOT = Path(__file__).resolve().parents[2] / "prompts"
_PROMPT_PATH = _PROMPT_ROOT / "planning-active-mode.md"
_TEMPLATES: dict[str, Path] = {
    "foreman": _PROMPT_ROOT / "planning-active-mode-foreman.md",
    "worker": _PROMPT_ROOT / "planning-active-mode-worker.md",
    "inspector": _PROMPT_ROOT / "planning-active-mode-inspector.md",
    "tier1_init": _PROMPT_ROOT / "t1_init.txt",
    "tier1_run": _PROMPT_ROOT / "t1_run.txt",
    "tier1_post": _PROMPT_ROOT / "t1_post.txt",
    "tier2_init": _PROMPT_ROOT / "t2_init.txt",
    "tier2_run": _PROMPT_ROOT / "t2_run.txt",
    "tier2_post": _PROMPT_ROOT / "t2_post.txt",
}
_TEMPLATE_MARKERS: dict[str, str] = {
    "foreman": "foreman",
    "worker": "worker",
    "inspector": "inspector",
    "tier1_init": "plan engine",
    "tier1_run": "planned task",
    "tier1_post": "validate the generated code",
    "tier2_init": "continuing an in-progress",
    "tier2_run": "integration phase",
    "tier2_post": "final validation",
}


def get_template_manifest() -> dict[str, str]:
    """Return the prompt template manifest.

    Returns:
        Mapping of role to repository-relative template path.
    """
    project_root = Path(__file__).resolve().parents[2]
    return {name: path.relative_to(project_root).as_posix() for name, path in _TEMPLATES.items()}


def load_prompt_template(name: str) -> str:
    """Load a named prompt template.

    Args:
        name: Template name.

    Returns:
        Template file content.

    Raises:
        KeyError: If the template name is not declared in the manifest.
        FileNotFoundError: If the declared template file is missing.
    """
    try:
        path = _TEMPLATES[name]
    except KeyError as exc:
        raise KeyError(f"unknown prompt template: {name}") from exc
    text = path.read_text(encoding="utf-8")
    if len(text.split()) < 40 or "Active planning prompt" in text:
        raise ValueError(f"prompt template is a placeholder: {path.name}")
    marker = _TEMPLATE_MARKERS[name]
    if marker not in text.lower():
        raise ValueError(f"prompt template {path.name} lacks marker {marker!r}")
    return text


def get_active_mode_prompt_path() -> Path:
    """Return the active planning prompt path.

    Returns:
        Existing prompt file path.
    """
    return _PROMPT_PATH


__all__ = ["get_active_mode_prompt_path", "get_template_manifest", "load_prompt_template"]
