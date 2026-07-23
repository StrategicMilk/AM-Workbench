"""Load baked YAML defaults and expose a cached layered config resolver."""

from __future__ import annotations

import copy
import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from vetinari.config.layered_resolver import LayeredResolver

logger = logging.getLogger(__name__)


YAML_FILES: list[str] = [
    "safety_defaults.yaml",
    "agent_model_defaults.yaml",
    "inference_profiles.yaml",
    "quantization_recommendations.yaml",
    "llamacpp_engine_defaults.yaml",
    "agent_routing_policy.yaml",
    "memory_cache_defaults.yaml",
    "ml_config.yaml",
    "training_dpo_defaults.yaml",
]
PROJECT_OVERRIDE_FILE = "project_overrides.yaml"
USER_OVERRIDE_FILE = "user_overrides.yaml"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "config"
_resolver_cache: dict[Path, LayeredResolver] = {}
_resolver_lock = threading.Lock()


def _is_path_under(candidate: Path, root: Path) -> bool:
    """Return True iff ``candidate`` resolves to a path under ``root``."""
    try:
        candidate_resolved = os.path.normcase(os.path.abspath(str(candidate.resolve(strict=False))))
        root_resolved = os.path.normcase(os.path.abspath(str(root.resolve(strict=False))))
        return os.path.commonpath([candidate_resolved, root_resolved]) == root_resolved
    except (OSError, ValueError) as exc:
        logger.warning("Could not compare config path %s against allowed root %s: %s", candidate, root, exc)
        return False


def _is_caller_supplied_path_allowed(config_dir: Path) -> bool:
    """Q-M2 path-traversal allowlist.

    A caller-supplied ``config_dir`` is permitted when it resolves to a path
    under the project root OR under the system temp dir (so pytest fixtures
    using ``tmp_path`` keep working). Anything else is rejected.
    """
    import tempfile

    resolved = config_dir.resolve()
    if _is_path_under(resolved, _PROJECT_ROOT):
        return True
    temp_roots = [Path(tempfile.gettempdir())]
    for env_name in ("PYTEST_DEBUG_TEMPROOT", "TMP", "TEMP", "TMPDIR"):
        env_value = os.environ.get(env_name)
        if env_value:
            temp_roots.append(Path(env_value))
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            temp_roots.append(Path(local_app_data) / "Temp")
        temp_roots.append(Path.home() / "AppData" / "Local" / "Temp")
    return any(_is_path_under(resolved, root) for root in temp_roots)


def load_config(config_dir: Path | None = None) -> LayeredResolver:
    """Return the LayeredResolver for ``config_dir`` or the project default.

    Each distinct resolved config directory gets its own cached resolver
    instance. This prevents custom config directories from receiving the
    default-directory resolver after the default cache has been primed.

    Args:
        config_dir: Directory to load YAMLs from. Defaults to the project
            ``config/`` directory. Must resolve to a path under the project
            root or under the system temp dir; anything else raises
            ``ValueError`` (Q-M2 path-traversal guard).

    Returns:
        Cached resolver for the selected configuration directory.

    Raises:
        ValueError: If ``config_dir`` resolves outside the project root and
            outside the system temp directory.
    """
    if config_dir is not None and not _is_caller_supplied_path_allowed(config_dir):
        raise ValueError(
            f"load_config(config_dir=...) must be under the project root or system tempdir; "
            f"refusing path: {config_dir.resolve()}"
        )
    key = (config_dir or _CONFIG_DIR).resolve()
    cached = _resolver_cache.get(key)
    if cached is None:
        with _resolver_lock:
            cached = _resolver_cache.get(key)
            if cached is None:
                cached = _build_resolver(key)
                _resolver_cache[key] = cached
    return cached


def load_backend_runtime_config(config_dir: Path | None = None) -> dict[str, Any]:
    """Load backend runtime config as a plain mapping.

    Args:
        config_dir: Optional config directory to resolve.

    Returns:
        Deep-copied backend runtime configuration.
    """
    resolver = load_config(config_dir)
    normalized = _resolved_runtime_config(resolver)
    normalized["baked"] = copy.deepcopy(resolver.baked)
    normalized["project"] = copy.deepcopy(resolver.project)
    normalized["user"] = copy.deepcopy(resolver.user)
    local_inference = normalized.get("local_inference")
    if isinstance(local_inference, Mapping):
        models_dir = local_inference.get("models_dir")
        if isinstance(models_dir, str) and models_dir.strip():
            normalized.setdefault("models", {}).setdefault("models_dir", models_dir)
            normalized.setdefault("gguf", {}).setdefault("gguf_dir", models_dir)
    return normalized


def _resolved_runtime_config(resolver: LayeredResolver) -> dict[str, Any]:
    """Return the effective runtime config while retaining layer provenance elsewhere.

    ``LayeredResolver`` owns the source layers, but runtime consumers need the
    effective mapping at the top level.  The resolver is still returned by
    :func:`load_config` for callers that need per-key source inspection.
    """
    merged = copy.deepcopy(resolver.baked)
    _deep_merge(merged, resolver.project)
    _deep_merge(merged, resolver.user)
    for dotted_key, (value, _source) in resolver.resolve_all().items():
        _set_dotted_key(merged, dotted_key, copy.deepcopy(value))
    return merged


def _set_dotted_key(target: dict[str, Any], dotted_key: str, value: Any) -> None:
    current = target
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        next_value = current.setdefault(part, {})
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value


def load_config_file(path: str | Path) -> dict[str, Any]:
    """Load a YAML config file and require a mapping root.

    Args:
        path: YAML file path.

    Returns:
        Loaded YAML mapping.

    Raises:
        ValueError: If the YAML root is not a mapping.
    """
    return _load_yaml_mapping(Path(path))


def reset_config_cache() -> None:
    """Clear the per-config_dir resolver cache for test isolation."""
    with _resolver_lock:
        _resolver_cache.clear()


def _build_resolver(config_dir: Path) -> LayeredResolver:
    merged: dict[str, Any] = {}
    for filename in YAML_FILES:
        loaded = _load_yaml_mapping(config_dir / filename)
        _deep_merge(merged, loaded)
    project = _load_optional_yaml_mapping(config_dir / PROJECT_OVERRIDE_FILE)
    user = _load_optional_yaml_mapping(config_dir / USER_OVERRIDE_FILE)
    return LayeredResolver(baked=merged, project=project, user=user)


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"config YAML root must be a mapping: {path}")
    LayeredResolver._validate_keys(loaded)
    return loaded


def _load_optional_yaml_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _load_yaml_mapping(path)


def _deep_merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        elif key in target and target[key] == value:
            continue
        else:
            target[key] = copy.deepcopy(value)
