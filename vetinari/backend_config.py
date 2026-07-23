"""Shared backend configuration helpers for local, vLLM, and NIM runtimes.

Normalizes configuration from the project config, user config, and environment
variables so setup, health checks, and runtime registration agree on one shape.
"""

from __future__ import annotations

import copy
import importlib
import logging
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from vetinari.config_paths import resolve_config_path
from vetinari.constants import (
    DEFAULT_NATIVE_MODELS_DIR,
    OPERATOR_MODELS_CACHE_DIR,
    get_user_dir,
)
from vetinari.guards import GateError

logger = logging.getLogger(__name__)


_PROJECT_MODELS_CONFIG = resolve_config_path("models.yaml")
ENGINE_ENDPOINT_ENV = "VETINARI_ENGINE_ENDPOINT"
ENGINE_BINARY_PATH_ENV = "VETINARI_ENGINE_BINARY_PATH"
ENGINE_MODEL_PATH_ENV = "VETINARI_ENGINE_MODEL_PATH"

_DEFAULT_BACKEND_CONFIG: dict[str, Any] = {
    "selection_policy": "configured",
    "primary": "vllm",
    "fallback": "nim",
    "llama_cpp_use_cases": [
        "explicit_user_preference",
        "weak_or_no_server_setup",
        "gguf_only_models",
        "cpu_ram_vram_offload",
        "oversized_local_models",
        "recovery_fallback",
    ],
    "native_models_dir": DEFAULT_NATIVE_MODELS_DIR,
    "vllm": {
        "enabled": False,
        "endpoint": "http://localhost:8000",
        "gpu_only": True,
        "semantic_cache_enabled": True,
        "cache_namespace": "vetinari",
        "cache_salt": "",
        "prefix_caching_enabled": True,
        "prefix_caching_hash_algo": "sha256",
        "container_setup": {},
    },
    "nim": {
        "enabled": False,
        "endpoint": "http://localhost:8001",
        "gpu_only": True,
        "semantic_cache_enabled": True,
        "cache_namespace": "vetinari",
        "kv_cache_host_offload_enabled": None,
        "kv_cache_reuse_enabled": False,
        "supports_cache_salt": False,
    },
    "am_engine": {
        "enabled": False,
        "endpoint": None,
        "binary_path": None,
        "model_path": None,
        "vision_enabled": False,
    },
}

_DEFAULT_LOCAL_INFERENCE_CONFIG: dict[str, Any] = {
    "models_dir": OPERATOR_MODELS_CACHE_DIR,
    "gpu_layers": -1,
    "context_length": 8192,
}


def _load_yaml_dict(path: Path) -> dict[str, Any]:
    """Load a YAML mapping from disk, failing closed on malformed files."""
    if not path.exists():
        return {}
    try:
        yaml: Any = importlib.import_module("yaml")

        with path.open(encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh) or {}
        return loaded if isinstance(loaded, dict) else {}
    except Exception as exc:
        logger.error("Could not load YAML config from %s - refusing defaults: %s", path, exc)
        raise GateError("backend_config", f"malformed YAML at {path}: {exc}", exc) from exc


def _merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base`` and return a new dict."""
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _coerce_env_bool(value: str) -> bool:
    """Coerce an environment flag string into a boolean."""
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def _normalize_backend_name(value: Any) -> str:
    """Normalize backend aliases used by config and environment variables."""
    if value is None:
        return ""
    normalized_name = str(value).strip().lower().replace("-", "_")
    if normalized_name in {"local", "llama", "llamacpp", "llama_cpp"}:
        return "llama_cpp"
    if normalized_name in {"nims", "nvidia_nim", "nvidia_nims"}:
        return "nim"
    return normalized_name


def _backend_order_from_primary_fallback(backend: dict[str, Any]) -> list[str]:
    """Build an explicit order from legacy primary/fallback keys."""
    order: list[str] = []
    for backend_name in (backend.get("primary"), backend.get("fallback"), "llama_cpp"):
        if backend_name is None:
            continue
        normalized_name = _normalize_backend_name(backend_name)
        if normalized_name and normalized_name not in order:
            order.append(normalized_name)
    return order


def _normalize_user_config(config: dict[str, Any]) -> dict[str, Any]:
    """Translate legacy user-config keys into the runtime backend shape."""
    normalized: dict[str, Any] = {}

    if isinstance(config.get("models"), dict):
        normalized["models"] = dict(config["models"])

    inference = config.get("inference")
    if isinstance(inference, dict):
        local = {}
        if "models_dir" in inference:
            local["models_dir"] = inference["models_dir"]
            normalized.setdefault("models", {})["gguf_dir"] = inference["models_dir"]
        if "gpu_layers" in inference:
            local["gpu_layers"] = inference["gpu_layers"]
        if "context_length" in inference:
            local["context_length"] = inference["context_length"]
        if local:
            normalized["local_inference"] = local

    if isinstance(config.get("local_inference"), dict):
        normalized["local_inference"] = _merge_dict(
            normalized.get("local_inference", {}),
            config["local_inference"],
        )

    backend: dict[str, Any] = {}
    if isinstance(config.get("inference_backend"), dict):
        backend = _merge_dict(backend, config["inference_backend"])

    for backend_name in ("vllm", "nim", "am_engine"):
        legacy_cfg = config.get(backend_name)
        if isinstance(legacy_cfg, dict):
            backend[backend_name] = _merge_dict(backend.get(backend_name, {}), legacy_cfg)

    if ("primary" in backend or "fallback" in backend) and "fallback_order" not in backend:
        backend["fallback_order"] = _backend_order_from_primary_fallback(backend)

    if backend:
        normalized["inference_backend"] = backend

    return normalized


def _base_runtime_config() -> dict[str, Any]:
    return {
        "inference_backend": dict(_DEFAULT_BACKEND_CONFIG),
        "local_inference": dict(_DEFAULT_LOCAL_INFERENCE_CONFIG),
        "models": {
            "gguf_dir": OPERATOR_MODELS_CACHE_DIR,
            "native_dir": DEFAULT_NATIVE_MODELS_DIR,
        },
    }


def _merge_loaded_config(config: dict[str, Any], loaded: dict[str, Any]) -> None:
    for section in ("inference_backend", "local_inference", "models"):
        if isinstance(loaded.get(section), dict):
            config[section] = _merge_dict(config[section], loaded[section])


def _merge_project_config(config: dict[str, Any], project_loaded: dict[str, Any]) -> None:
    if isinstance(project_loaded.get("hardware"), dict):
        config["hardware"] = dict(project_loaded["hardware"])
    _merge_loaded_config(config, project_loaded)


def _apply_model_directory_env(config: dict[str, Any]) -> None:
    gguf_dir = os.environ.get("VETINARI_MODELS_DIR")
    if gguf_dir:
        config["local_inference"]["models_dir"] = gguf_dir
        config["models"]["gguf_dir"] = gguf_dir

    native_dir = os.environ.get("VETINARI_NATIVE_MODELS_DIR")
    if native_dir:
        config["inference_backend"]["native_models_dir"] = native_dir
        config["models"]["native_dir"] = native_dir
        return
    config["models"]["native_dir"] = config["inference_backend"].get(
        "native_models_dir",
        config["models"]["native_dir"],
    )


def _merge_backend_env(config: dict[str, Any], backend_name: str, values: dict[str, Any]) -> None:
    config["inference_backend"][backend_name] = _merge_dict(
        config["inference_backend"].get(backend_name, {}),
        values,
    )


def _apply_backend_env(config: dict[str, Any]) -> None:
    if vllm_endpoint := os.environ.get("VETINARI_VLLM_ENDPOINT"):
        _merge_backend_env(config, "vllm", {"enabled": True, "endpoint": vllm_endpoint})
    if (vllm_cache_salt := os.environ.get("VETINARI_VLLM_CACHE_SALT")) is not None:
        _merge_backend_env(config, "vllm", {"cache_salt": vllm_cache_salt})
    if (vllm_prefix_caching := os.environ.get("VETINARI_VLLM_PREFIX_CACHING_ENABLED")) is not None:
        _merge_backend_env(config, "vllm", {"prefix_caching_enabled": _coerce_env_bool(vllm_prefix_caching)})
    if vllm_prefix_hash_algo := os.environ.get("VETINARI_VLLM_PREFIX_CACHING_HASH_ALGO"):
        _merge_backend_env(config, "vllm", {"prefix_caching_hash_algo": vllm_prefix_hash_algo})
    if nim_endpoint := os.environ.get("VETINARI_NIM_ENDPOINT"):
        _merge_backend_env(config, "nim", {"enabled": True, "endpoint": nim_endpoint})
    if (nim_kv_reuse := os.environ.get("NIM_ENABLE_KV_CACHE_REUSE")) is not None:
        _merge_backend_env(config, "nim", {"kv_cache_reuse_enabled": _coerce_env_bool(nim_kv_reuse)})
    if (nim_kv_host_offload := os.environ.get("NIM_ENABLE_KV_CACHE_HOST_OFFLOAD")) is not None:
        _merge_backend_env(config, "nim", {"kv_cache_host_offload_enabled": _coerce_env_bool(nim_kv_host_offload)})
    engine_values: dict[str, Any] = {}
    if engine_endpoint := get_engine_endpoint():
        engine_values["endpoint"] = engine_endpoint
    if engine_binary_path := get_engine_binary_path():
        engine_values["binary_path"] = str(engine_binary_path)
    if engine_model_path := get_engine_model_path():
        engine_values["model_path"] = str(engine_model_path)
    if engine_values:
        _merge_backend_env(config, "am_engine", engine_values)


def get_engine_endpoint() -> str | None:
    """Return a validated loopback AM Engine endpoint override, if configured.

    Returns:
        Normalized loopback HTTP origin, or ``None`` when unset.

    Raises:
        ValueError: If the override is not an explicit loopback HTTP endpoint.
    """
    value = os.environ.get(ENGINE_ENDPOINT_ENV)
    if not value:
        return None
    endpoint = value.rstrip("/")
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port is None:
        raise ValueError(f"{ENGINE_ENDPOINT_ENV} must be an http loopback endpoint with an explicit port")
    return endpoint


def get_engine_binary_path() -> Path | None:
    """Return the explicit AM Engine binary override without probing or provisioning.

    Returns:
        Resolved override path, or ``None`` when unset.
    """
    value = os.environ.get(ENGINE_BINARY_PATH_ENV)
    if not value:
        return None
    return Path(value).expanduser().resolve()


def get_engine_model_path() -> Path | None:
    """Return the operator-selected GGUF model after strict path validation.

    Returns:
        Resolved regular-file path, or ``None`` when unset.

    Raises:
        ValueError: If the configured path is not an existing GGUF file.
    """
    value = os.environ.get(ENGINE_MODEL_PATH_ENV, "").strip()
    if not value:
        return None
    path = Path(value).expanduser().resolve()
    if path.suffix.lower() != ".gguf" or not path.is_file():
        raise ValueError(f"{ENGINE_MODEL_PATH_ENV} must name an existing regular .gguf file")
    return path


def _apply_preferred_backend_env(config: dict[str, Any]) -> None:
    preferred_backend = _normalize_backend_name(
        os.environ.get("VETINARI_PREFERRED_BACKEND") or os.environ.get("VETINARI_INFERENCE_BACKEND")
    )
    if not preferred_backend:
        return
    existing_order = config["inference_backend"].get("fallback_order")
    if not isinstance(existing_order, list):
        existing_order = _backend_order_from_primary_fallback(config["inference_backend"])
    fallback_order = [preferred_backend]
    for backend_name in existing_order:
        normalized_name = _normalize_backend_name(backend_name)
        if normalized_name and normalized_name not in fallback_order:
            fallback_order.append(normalized_name)
    if "llama_cpp" not in fallback_order:
        fallback_order.append("llama_cpp")
    config["inference_backend"]["primary"] = preferred_backend
    config["inference_backend"]["fallback"] = fallback_order[1] if len(fallback_order) > 1 else "llama_cpp"
    config["inference_backend"]["fallback_order"] = fallback_order


_BACKEND_RUNTIME_CONFIG_CACHE: dict[tuple[str, str], dict[str, Any]] = {}
_BACKEND_RUNTIME_CONFIG_CACHE_LOCK = threading.Lock()


def reset_backend_runtime_config_cache() -> None:
    """Clear the cached backend runtime config (lock-safe; tests + ops).

    The cache hashes by the resolved ``project_config_path`` and
    ``user_config_path`` strings, so tests that point the loader at
    different fixtures get isolated cache entries automatically.  This
    reset is exposed for ops paths that need to reload after editing
    the YAML on disk and for tests that need a guaranteed-cold cache.
    """
    with _BACKEND_RUNTIME_CONFIG_CACHE_LOCK:
        _BACKEND_RUNTIME_CONFIG_CACHE.clear()


def load_backend_runtime_config(
    *,
    project_config_path: Path | None = None,
    user_config_path: Path | None = None,
) -> dict[str, Any]:
    """Return normalized backend config from project, user, and env sources.

    Disk reads are cached at the module level keyed by the resolved
    ``(project_config_path, user_config_path)`` pair so route handlers
    and per-request callers do not pay a YAML-parse cost on every
    invocation.  Each call returns a fresh deep copy of the cached
    payload so caller mutations cannot bleed back into the cache.

    Args:
        project_config_path: Optional override for the project ``config/models.yaml`` path.
        user_config_path: Optional override for the user ``~/.vetinari/config.yaml`` path.

    Returns:
        Normalized runtime config covering backend enablement, endpoint URLs,
        and local/native model directories.
    """
    project_key = str(project_config_path or _PROJECT_MODELS_CONFIG)
    user_key = str(user_config_path or (get_user_dir() / "config.yaml"))
    cache_key = (project_key, user_key)
    with _BACKEND_RUNTIME_CONFIG_CACHE_LOCK:
        cached = _BACKEND_RUNTIME_CONFIG_CACHE.get(cache_key)
        if cached is not None:
            return copy.deepcopy(cached)

    config = _base_runtime_config()

    project_loaded = _load_yaml_dict(project_config_path or _PROJECT_MODELS_CONFIG)
    if project_loaded:
        _merge_project_config(config, project_loaded)

    user_loaded = _normalize_user_config(_load_yaml_dict(user_config_path or (get_user_dir() / "config.yaml")))
    if user_loaded:
        _merge_loaded_config(config, user_loaded)

    _apply_model_directory_env(config)
    _apply_backend_env(config)
    _apply_preferred_backend_env(config)

    with _BACKEND_RUNTIME_CONFIG_CACHE_LOCK:
        _BACKEND_RUNTIME_CONFIG_CACHE[cache_key] = copy.deepcopy(config)
    return config


def resolve_provider_fallback_order(
    config: dict[str, Any],
    available_providers: set[str] | None = None,
) -> list[str]:
    """Return the preferred provider order for runtime fallback.

    Args:
        config: Runtime config dict produced by ``load_backend_runtime_config``.
        available_providers: Optional provider names to filter the final order by.

    Returns:
        Provider names in preferred fallback order, limited to configured and
        currently enabled backends.
    """
    backend_cfg = config.get("inference_backend", {})
    name_map = {
        "am_engine": "am_engine",
        "llama_cpp": "local",
        "local": "local",
        "vllm": "vllm",
        "nim": "nim",
    }

    order: list[str] = []

    def add_backend(backend_name: str | None) -> None:
        """Append a configured backend to the fallback order when it is usable."""
        if backend_name is None:
            return
        normalized_name = _normalize_backend_name(backend_name)
        if normalized_name in {"vllm", "nim", "am_engine"}:
            section = backend_cfg.get(normalized_name, {})
            if not (isinstance(section, dict) and section.get("enabled") and section.get("endpoint")):
                return
        provider_name = name_map.get(normalized_name)
        if provider_name and provider_name not in order:
            order.append(provider_name)

    configured_order = backend_cfg.get("fallback_order")
    if isinstance(configured_order, list):
        for backend_name in configured_order:
            add_backend(str(backend_name))
    else:
        add_backend(backend_cfg.get("primary"))
        add_backend(backend_cfg.get("fallback"))

    for backend_name in ("vllm", "nim", "am_engine"):
        section = backend_cfg.get(backend_name, {})
        if isinstance(section, dict) and section.get("enabled") and section.get("endpoint"):
            add_backend(backend_name)

    add_backend("llama_cpp")

    if available_providers is not None:
        order = [provider_name for provider_name in order if provider_name in available_providers]

    return order
