"""Shared helpers for the OpenAI-compatible server adapter."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from .base import InferenceRequest

logger = logging.getLogger(__name__)


# Timeout for health checks and model discovery (seconds)
_DISCOVERY_TIMEOUT_S = 10
# Timeout for inference requests (seconds) — overridden by config.timeout_seconds
_DEFAULT_INFERENCE_TIMEOUT_S = 120
# How long (seconds) before the model cache is considered stale
_CACHE_TTL_S = 300


def _httpx() -> Any:
    """Lazy-import httpx to avoid import-time cost when not used."""
    try:
        import httpx

        return httpx
    except ImportError as exc:
        raise ImportError(
            "httpx is required for OpenAIServerAdapter.  Install with: pip install httpx",
        ) from exc


# URL patterns that identify NVIDIA NIM endpoints.
# NIM can be self-hosted (using /nim/ path) or cloud-hosted on NVIDIA's API gateway.
_NIM_URL_PATTERNS = ("api.nvcf.nvidia.com", "ngc.nvidia.com", "/nim/")
SERVER_ADAPTER_CACHE_VERSION = "openai_server_adapter:v2"
_CACHE_SENSITIVE_PAYLOAD_KEYS = {"cache_salt"}
_PROVENANCE_EXTRA_KEYS = (
    "artifact_sha256",
    "cache_namespace",
    "container_digest",
    "container_image",
    "deployment_id",
    "engine_version",
    "gpu_only",
    "kv_cache_host_offload_enabled",
    "kv_cache_reuse_enabled",
    "model_digest",
    "model_revision",
    "nim_profile",
    "prefix_caching_enabled",
    "prefix_caching_hash_algo",
    "served_model_name",
    "served_model_revision",
    "server_build",
    "tensor_parallel_size",
    "tokenizer_revision",
    "vllm_engine_args_hash",
)


def _stable_json(value: Any) -> str:
    """Return stable JSON for cache/provenance identity material."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _stable_hash(value: Any) -> str:
    """Return SHA-256 over stable JSON identity material."""
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _hash_secret(value: object) -> str:
    """Hash sensitive identity material without storing the raw value."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _cache_safe_payload(value: Any) -> Any:
    """Return payload material safe to store in semantic-cache context."""
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            if key in _CACHE_SENSITIVE_PAYLOAD_KEYS and item is not None:
                safe[key] = {"sha256": _hash_secret(item)}
            else:
                safe[key] = _cache_safe_payload(item)
        return safe
    if isinstance(value, list):
        return [_cache_safe_payload(item) for item in value]
    return value


def _cache_identity_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return output-affecting payload identity without embedding the query text."""
    safe = _cache_safe_payload(payload)
    if not isinstance(safe, dict):
        return {}

    messages = safe.get("messages")
    if isinstance(messages, list):
        identity_messages: list[Any] = []
        for message in messages:
            if isinstance(message, dict):
                identity_message = dict(message)
                if identity_message.get("role") == "user":
                    identity_message["content"] = "<query>"
                identity_messages.append(identity_message)
            else:
                identity_messages.append(message)
        safe["messages"] = identity_messages
    return safe


def _detect_provider_label(api_base: str, configured_type_value: str) -> str:
    """Return the correct provider label for a server endpoint.

    Detects NVIDIA NIM endpoints by URL pattern so that ModelInfo objects
    are correctly labelled even when the adapter was registered under a
    generic ``vllm`` provider type.

    Args:
        api_base: The base URL of the inference server (already rstrip'd of ``/``).
        configured_type_value: The ``ProviderType.value`` string from the config.

    Returns:
        ``"nim"`` when the URL matches a known NIM pattern, otherwise
        ``configured_type_value`` unchanged.
    """
    for pattern in _NIM_URL_PATTERNS:
        if pattern in api_base:
            return "nim"
    return configured_type_value


def _coerce_bool(value: object, default: bool) -> bool:
    """Coerce a config value to bool, handling string representations from YAML.

    YAML parses unquoted booleans correctly, but quoted values like ``"false"``
    arrive as strings.  This guard prevents ``"false"`` from being truthy.

    Args:
        value: Raw config value (bool, str, int, or None).
        default: Returned when value is None.

    Returns:
        Coerced boolean.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("false", "0", "no", "off")


def _semantic_cache_identity(
    *,
    request: InferenceRequest,
    provider_type: str,
    provider_name: str,
    provider_label: str,
    api_base: str,
    gpu_only: bool,
    payload: dict[str, Any],
    raw_model_entry: dict[str, Any] | None,
    extra_config: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    """Build strict cache identity for OpenAI-compatible server backends."""
    model_entry = raw_model_entry or {}
    model_entry_hash = _stable_hash(model_entry)
    provenance = {
        key: extra_config[key]
        for key in _PROVENANCE_EXTRA_KEYS
        if key in extra_config and extra_config[key] not in (None, "")
    }
    provenance_hash = _stable_hash(provenance)
    endpoint_hash = hashlib.sha256(api_base.encode("utf-8")).hexdigest()
    cache_model_id = (
        f"{provider_type}:{provider_name}:{endpoint_hash}:{request.model_id}:{model_entry_hash}:{provenance_hash}"
    )
    identity: dict[str, Any] = {
        "adapter_version": SERVER_ADAPTER_CACHE_VERSION,
        "endpoint_sha256": endpoint_hash,
        "gpu_only": gpu_only,
        "model_entry": model_entry,
        "model_entry_sha256": model_entry_hash,
        "payload": _cache_identity_payload(payload),
        "provider_label": provider_label,
        "provider_name": provider_name,
        "provider_type": provider_type,
        "requested_model_id": request.model_id,
        "server_provenance": provenance,
        "server_provenance_sha256": provenance_hash,
    }
    cache_context = _stable_json(identity)
    return cache_model_id, cache_context, identity


def _get_semantic_cache() -> Any:
    """Return the global semantic cache lazily for patchable server-adapter use."""
    from vetinari.optimization.semantic_cache import get_semantic_cache

    return get_semantic_cache()
