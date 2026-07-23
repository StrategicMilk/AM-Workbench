"""Configuration loader for Workbench model quick choices."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from vetinari.workbench.model_choices.contracts import Surface

CONFIG_SCHEMA_VERSION = 1


class QuickChoicesConfigError(ValueError):
    """Raised when quick-choice configuration is invalid."""


@dataclass(frozen=True, slots=True)
class SurfaceChoiceConfig:
    """Capability requirements and provider ordering for one surface."""

    required_capabilities: tuple[str, ...]
    disallowed_capabilities: tuple[str, ...]
    provider_priority: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QuickChoicesConfig:
    """Validated quick-choice configuration."""

    schema_version: int
    surfaces: dict[Surface, SurfaceChoiceConfig]

    def surface_config(self, surface: Surface | str) -> SurfaceChoiceConfig:
        """Return the validated config for one surface.

        Returns:
            SurfaceChoiceConfig value produced by surface_config().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        resolved = surface if isinstance(surface, Surface) else Surface(str(surface))
        try:
            return self.surfaces[resolved]
        except KeyError as exc:
            raise QuickChoicesConfigError(f"surface {resolved.value!r} is not configured") from exc

    def for_surface(self, surface: Surface | str) -> SurfaceChoiceConfig:
        """Compatibility alias for callers that request config by surface."""
        return self.surface_config(surface)


def load_quick_choices_config(path: Path | str) -> QuickChoicesConfig:
    """Load and validate quick-choice YAML from ``path``.

    Returns:
        Resolved quick choices config value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    resolved = Path(path)
    try:
        payload = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except OSError as exc:
        raise QuickChoicesConfigError(f"quick choices config unreadable: {resolved}") from exc
    if not isinstance(payload, dict):
        raise QuickChoicesConfigError("quick choices config must be a mapping")
    version = payload.get("schema_version")
    if version != CONFIG_SCHEMA_VERSION:
        raise QuickChoicesConfigError(f"schema_version must be {CONFIG_SCHEMA_VERSION}")
    raw_surfaces = payload.get("surfaces")
    if not isinstance(raw_surfaces, dict):
        raise QuickChoicesConfigError("surfaces must be a mapping")
    capability_catalog = set(_string_tuple(payload.get("capability_catalog", ()), "capability_catalog"))

    allowed = {surface.value for surface in Surface}
    unknown = set(raw_surfaces) - allowed
    missing = allowed - set(raw_surfaces)
    if unknown:
        raise QuickChoicesConfigError(f"unknown surfaces: {sorted(unknown)}")
    if missing:
        raise QuickChoicesConfigError(f"missing surfaces: {sorted(missing)}")

    surfaces: dict[Surface, SurfaceChoiceConfig] = {}
    for surface_value, row in raw_surfaces.items():
        if not isinstance(row, dict):
            raise QuickChoicesConfigError(f"{surface_value} surface config must be a mapping")
        required = _string_tuple(row.get("required_capabilities", ()), f"{surface_value}.required_capabilities")
        disallowed = _string_tuple(
            row.get("disallowed_capabilities", ()),
            f"{surface_value}.disallowed_capabilities",
            allow_empty=True,
        )
        providers = _string_tuple(
            row.get("provider_priority", ()), f"{surface_value}.provider_priority", allow_empty=True
        )
        overlap = set(required) & set(disallowed)
        if overlap:
            raise QuickChoicesConfigError(
                f"{surface_value} required_capabilities overlap disallowed_capabilities: {sorted(overlap)}",
            )
        unknown_capabilities = (set(required) | set(disallowed)) - capability_catalog
        if unknown_capabilities:
            raise QuickChoicesConfigError(
                f"{surface_value} references capabilities outside capability_catalog: {sorted(unknown_capabilities)}"
            )
        surfaces[Surface(surface_value)] = SurfaceChoiceConfig(
            required_capabilities=required,
            disallowed_capabilities=disallowed,
            provider_priority=providers,
        )
    return QuickChoicesConfig(schema_version=version, surfaces=surfaces)


def _string_tuple(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        raise QuickChoicesConfigError(f"{field} must be a list of strings")
    result = tuple(str(item) for item in value if str(item).strip())
    if not allow_empty and not result:
        raise QuickChoicesConfigError(f"{field} must not be empty")
    if len(result) != len(value):
        raise QuickChoicesConfigError(f"{field} contains blank values")
    return result


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "QuickChoicesConfig",
    "QuickChoicesConfigError",
    "SurfaceChoiceConfig",
    "load_quick_choices_config",
]
