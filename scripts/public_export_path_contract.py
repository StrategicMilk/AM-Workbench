"""Canonical cross-platform path rules for public export trees."""

from __future__ import annotations

import unicodedata

PATH_CONTRACT_VERSION = 1
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_DEVICE_NAMES = frozenset({
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
})


def _component_error(component: str) -> str | None:
    if component in {"", ".", ".."}:
        return "contains an empty or traversal component"
    if component.endswith((" ", ".")):
        return "contains a component ending in a space or dot"
    if any(ord(character) < 32 or ord(character) == 127 for character in component):
        return "contains an ASCII control character"
    if any(character in _WINDOWS_INVALID_CHARACTERS for character in component):
        return "contains a character invalid on Windows"
    device_stem = component.split(".", 1)[0].upper()
    if device_stem in _WINDOWS_DEVICE_NAMES:
        return f"contains reserved Windows device name {device_stem!r}"
    return None


def validate_public_paths(paths: list[str]) -> list[tuple[str, str]]:
    """Return path-specific errors for a canonical cross-platform export."""
    errors: list[tuple[str, str]] = []
    seen_exact: set[str] = set()
    seen_portable: dict[str, str] = {}
    for path in paths:
        if not path:
            errors.append((path, "path is empty"))
            continue
        if path != path.strip():
            errors.append((path, "path has leading or trailing whitespace"))
            continue
        if path.startswith("/") or "\\" in path:
            errors.append((path, "path is absolute or contains a backslash"))
            continue
        if unicodedata.normalize("NFC", path) != path:
            errors.append((path, "path is not Unicode NFC-normalized"))
            continue
        component_error = next(
            (error for component in path.split("/") if (error := _component_error(component)) is not None),
            None,
        )
        if component_error is not None:
            errors.append((path, component_error))
            continue
        if path in seen_exact:
            errors.append((path, "path is duplicated"))
            continue
        portable_key = unicodedata.normalize("NFC", path).casefold()
        previous = seen_portable.get(portable_key)
        if previous is not None and previous != path:
            errors.append((previous, f"path case/normalization-collides with {path!r}"))
            errors.append((path, f"path case/normalization-collides with {previous!r}"))
            continue
        seen_exact.add(path)
        seen_portable[portable_key] = path
    return errors


__all__ = ["PATH_CONTRACT_VERSION", "validate_public_paths"]
