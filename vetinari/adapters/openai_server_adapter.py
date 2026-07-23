"""OpenAI-compatible server adapter for vLLM, NVIDIA NIMs, and similar backends.

Provides a unified adapter for any inference server exposing the OpenAI-compatible
``/v1/chat/completions`` and ``/v1/models`` API endpoints.  This covers:

- **vLLM**: GPU-only, high-throughput local inference server
- **NVIDIA NIMs**: NVIDIA's inference microservice containers
- **Any OpenAI-compatible server**: LM Studio, text-generation-inference, etc.

Decision: single adapter for all OpenAI-compatible servers (ADR-0084).
"""

from __future__ import annotations

from typing import Any

from .base import ModelInfo, ProviderAdapter, ProviderConfig
from .openai_server_adapter_discovery import OpenAIServerDiscoveryMixin
from .openai_server_adapter_handlers import create_openai_compat_handlers
from .openai_server_adapter_helpers import (
    _CACHE_SENSITIVE_PAYLOAD_KEYS as _CACHE_SENSITIVE_PAYLOAD_KEYS,
)
from .openai_server_adapter_helpers import (
    _CACHE_TTL_S as _CACHE_TTL_S,
)
from .openai_server_adapter_helpers import (
    _DEFAULT_INFERENCE_TIMEOUT_S as _DEFAULT_INFERENCE_TIMEOUT_S,
)
from .openai_server_adapter_helpers import (
    _DISCOVERY_TIMEOUT_S as _DISCOVERY_TIMEOUT_S,
)
from .openai_server_adapter_helpers import (
    _PROVENANCE_EXTRA_KEYS as _PROVENANCE_EXTRA_KEYS,
)
from .openai_server_adapter_helpers import (
    SERVER_ADAPTER_CACHE_VERSION as SERVER_ADAPTER_CACHE_VERSION,
)
from .openai_server_adapter_helpers import (
    _cache_identity_payload as _cache_identity_payload,
)
from .openai_server_adapter_helpers import (
    _cache_safe_payload as _cache_safe_payload,
)
from .openai_server_adapter_helpers import (
    _coerce_bool as _coerce_bool,
)
from .openai_server_adapter_helpers import (
    _detect_provider_label as _detect_provider_label,
)
from .openai_server_adapter_helpers import (
    _get_semantic_cache as _get_semantic_cache,
)
from .openai_server_adapter_helpers import (
    _hash_secret as _hash_secret,
)
from .openai_server_adapter_helpers import (
    _httpx as _httpx,
)
from .openai_server_adapter_helpers import (
    _semantic_cache_identity as _semantic_cache_identity,
)
from .openai_server_adapter_helpers import (
    _stable_hash as _stable_hash,
)
from .openai_server_adapter_helpers import (
    _stable_json as _stable_json,
)
from .openai_server_adapter_helpers import (
    logger as logger,
)
from .openai_server_adapter_inference import OpenAIServerInferenceMixin

__all__ = [
    "SERVER_ADAPTER_CACHE_VERSION",
    "_CACHE_SENSITIVE_PAYLOAD_KEYS",
    "_CACHE_TTL_S",
    "_DEFAULT_INFERENCE_TIMEOUT_S",
    "_DISCOVERY_TIMEOUT_S",
    "_PROVENANCE_EXTRA_KEYS",
    "OpenAIServerAdapter",
    "_cache_identity_payload",
    "_cache_safe_payload",
    "_coerce_bool",
    "_detect_provider_label",
    "_get_semantic_cache",
    "_hash_secret",
    "_httpx",
    "_semantic_cache_identity",
    "_stable_hash",
    "_stable_json",
    "create_openai_compat_handlers",
]


class OpenAIServerAdapter(
    OpenAIServerDiscoveryMixin,
    OpenAIServerInferenceMixin,
    ProviderAdapter,
):
    """Adapter for OpenAI-compatible inference servers (vLLM, NVIDIA NIMs, etc.).

    Communicates via the standard ``/v1/models`` and ``/v1/chat/completions``
    endpoints.  Does not depend on litellm — uses httpx directly for minimal
    footprint.

    Configuration via ``extra_config``:

    - ``gpu_only`` (bool, default True): Whether the backend requires models
      to fit entirely in VRAM.  True for vLLM, usually True for NIMs.  Accepts
      YAML string values (``"false"`` is correctly coerced to ``False``).
    - ``api_key`` (str, optional): Bearer token for authenticated endpoints.
    - ``semantic_cache_enabled`` (bool, default True): Enables Vetinari's
      local semantic response cache with server/artifact/sampler isolation.
    - ``cache_salt`` (str, vLLM only): Optional request-level vLLM prefix-cache
      salt. The raw value is sent to vLLM but only its hash is stored locally.
    - ``kv_cache_reuse_enabled`` (bool, NIM): Records server-side NIM KV cache
      reuse state in cache provenance. NIM exposes this as deployment config,
      not as an OpenAI request parameter.

    Cache staleness: ``_last_discovery_ts`` records the epoch-second of the last
    successful discovery.  When rediscovery fails and the cache is older than
    ``_CACHE_TTL_S`` seconds, ``_cache_is_stale`` is set to ``True`` and every
    subsequent request logs a WARNING so operators know they are reading stale
    data.
    """

    def __init__(self, config: ProviderConfig) -> None:
        """Store endpoint URL and parse backend-specific flags from extra_config.

        Args:
            config: Provider configuration with endpoint URL and optional api_key.
        """
        super().__init__(config)
        self._api_base = config.endpoint.rstrip("/") if config.endpoint else ""
        # Coerce gpu_only — YAML may deliver "false" as a truthy string
        self._gpu_only = _coerce_bool(config.extra_config.get("gpu_only", True), default=True)
        self._semantic_cache_enabled = _coerce_bool(
            config.extra_config.get("semantic_cache_enabled", True),
            default=True,
        )
        self._discovered_models: list[ModelInfo] = []
        self._raw_model_entries: dict[str, dict[str, Any]] = {}
        # Cache staleness tracking — set after failed rediscovery past TTL
        self._last_discovery_ts: float = 0.0  # epoch seconds of last successful discovery
        self._cache_is_stale: bool = False  # True when cache is past TTL and rediscovery failed
        # Cached supported-matrix verdict for the vLLM engine version.
        # _engine_version_checked flips to True after the first successful
        # /version probe so we do not re-fetch on every health check.
        # _engine_version_block_reason is set when the supported matrix flags
        # the running version as a blocker (e.g. vLLM 0.18.1 on SM120). When
        # populated, health_check returns unhealthy with this reason so the
        # router skips this adapter.
        self._engine_version_checked: bool = False
        self._engine_version_block_reason: str | None = None
