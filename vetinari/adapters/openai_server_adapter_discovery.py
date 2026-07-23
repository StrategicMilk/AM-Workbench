"""Discovery and health-check mixin for OpenAIServerAdapter."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .base import ModelInfo, ProviderType
from .llama_cpp_model_info import _infer_capabilities, _infer_context_window
from .openai_server_adapter_helpers import (
    _CACHE_TTL_S,
    _DISCOVERY_TIMEOUT_S,
    _detect_provider_label,
    _httpx,
    logger,
)


def _httpx_runtime() -> Any:
    facade = sys.modules.get("vetinari.adapters.openai_server_adapter")
    facade_httpx = getattr(facade, "_httpx", None) if facade is not None else None
    if facade_httpx is not None and facade_httpx is not _httpx:
        return facade_httpx()
    return _httpx()


class OpenAIServerDiscoveryMixin:
    """Provide model discovery, health checks, and capability reporting."""

    _api_base: str
    _auth_headers: Callable[[], dict[str, str]]
    _cache_is_stale: bool
    _discovered_models: list[ModelInfo]
    _engine_version_block_reason: str | None
    _engine_version_checked: bool
    _last_discovery_ts: float
    _raw_model_entries: dict[str, dict[str, Any]]
    models: list[ModelInfo]
    name: str
    provider_type: ProviderType

    def _model_info_from_entry(self, model_id: str, entry: dict[str, Any]) -> ModelInfo:
        """Map one OpenAI-compatible model entry to ModelInfo."""
        capabilities = _infer_capabilities(model_id)
        provider_label = _detect_provider_label(self._api_base, self.provider_type.value)
        return ModelInfo(
            id=model_id,
            name=model_id,
            provider=provider_label,
            endpoint=self._api_base,
            capabilities=capabilities,
            context_len=_infer_context_window(model_id),
            memory_gb=0,  # Server manages its own VRAM
            version=entry.get("owned_by", "unknown"),
            latency_estimate_ms=500,  # Server-based inference is typically fast
            throughput_tokens_per_sec=100.0,
            cost_per_1k_tokens=0.0,  # Local server - no API cost
            free_tier=True,
            tags=capabilities,
        )

    def discover_models(self) -> list[ModelInfo]:
        """Query the ``/v1/models`` endpoint and map results to ModelInfo.

        On success, updates the internal cache and resets the stale flag.  On
        failure, marks the cache stale when it is older than ``_CACHE_TTL_S``
        so callers know they are reading potentially outdated data.

        Returns:
            List of ModelInfo objects for models served by this backend.
        """
        if not self._api_base:
            logger.warning("No endpoint configured for %s — cannot discover models", self.name)
            return []

        httpx = _httpx_runtime()
        url = f"{self._api_base}/v1/models"
        headers = self._auth_headers()

        try:
            resp = httpx.get(url, headers=headers, timeout=_DISCOVERY_TIMEOUT_S)
            resp.raise_for_status()
        except Exception as exc:
            logger.warning(
                "Model discovery failed for %s at %s — server may not be running: %s",
                self.name,
                url,
                exc,
            )
            # Mark cache stale when past TTL so every subsequent caller sees a warning
            now = time.time()
            if self._last_discovery_ts > 0 and (now - self._last_discovery_ts) > _CACHE_TTL_S:
                self._cache_is_stale = True
                age_s = int(now - self._last_discovery_ts)
                logger.warning(
                    "Serving stale model list for %s — last successful discovery was %ds ago",
                    self.name,
                    age_s,
                )
            return self._discovered_models

        raw = resp.json()
        # Always read from "data"; servers that omit it return an empty list
        data = raw.get("data", [])
        discovered: list[ModelInfo] = []
        raw_model_entries: dict[str, dict[str, Any]] = {}

        for entry in data:
            model_id = entry.get("id", "")
            if not model_id:
                continue
            raw_model_entries[model_id] = dict(entry)
            discovered.append(self._model_info_from_entry(model_id, entry))

        self._discovered_models = discovered
        self._raw_model_entries = raw_model_entries
        self.models = discovered
        self._last_discovery_ts = time.time()
        self._cache_is_stale = False
        logger.info("Discovered %d models from %s at %s", len(discovered), self.name, self._api_base)
        return discovered

    def health_check(self) -> dict[str, Any]:
        """Verify the server is reachable by hitting ``/v1/models``.

        For vLLM endpoints, also probe ``/version`` once and cross-check the
        engine version against ``config/runtime/supported_matrix.yaml``. If the
        running version is in a known-bad range on this hardware (e.g. vLLM
        0.18.1 on SM120), report unhealthy so the router skips this adapter
        rather than dispatching inference to a regressed engine.

        Returns:
            Dict with ``healthy``, ``reason``, and ``timestamp`` keys.
        """
        if not self._api_base:
            return {
                "healthy": False,
                "reason": f"No endpoint configured for {self.name}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        httpx = _httpx_runtime()
        url = f"{self._api_base}/v1/models"
        try:
            resp = httpx.get(url, headers=self._auth_headers(), timeout=_DISCOVERY_TIMEOUT_S)
            resp.raise_for_status()
            model_count = len(resp.json().get("data", []))
        except Exception as exc:
            logger.warning("Health check for %s at %s failed: %s — reporting unhealthy", self.name, url, exc)
            return {
                "healthy": False,
                "reason": f"Cannot reach {self.name} at {url}: {exc}",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        if self.provider_type.value == "vllm":
            self._check_vllm_engine_version_once(httpx)
            if self._engine_version_block_reason:
                return {
                    "healthy": False,
                    "reason": self._engine_version_block_reason,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

        return {
            "healthy": True,
            "reason": f"{model_count} model(s) available",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _check_vllm_engine_version_once(self, httpx_module: Any) -> None:
        """Probe vLLM ``/version`` once and validate against the supported matrix.

        Best-effort: if the endpoint is missing, returns silently and treats the
        engine version as unknown (no block, no false positive). When the matrix
        flags the running version as a blocker on the local hardware, sets
        ``self._engine_version_block_reason`` so subsequent health checks return
        unhealthy without re-probing.

        Args:
            httpx_module: The lazily-imported httpx module already in use by
                the caller; passed to avoid a second import.
        """
        if self._engine_version_checked:
            return
        self._engine_version_checked = True

        version_url = f"{self._api_base}/version"
        try:
            resp = httpx_module.get(version_url, headers=self._auth_headers(), timeout=_DISCOVERY_TIMEOUT_S)
            if resp.status_code != 200:
                logger.debug(
                    "vLLM /version probe at %s returned HTTP %s — skipping known-bad-range gate",
                    version_url,
                    resp.status_code,
                )
                return
            payload = resp.json()
        except Exception as exc:
            # Network or parse failure on /version — record at INFO so operators
            # can see the gate did not run, but do not flip the adapter unhealthy
            # since this is best-effort verification, not a hard precondition.
            logger.info(
                "vLLM /version probe at %s failed (%s) — engine version treated as unknown; "
                "supported-matrix gate skipped for this session",
                version_url,
                exc,
            )
            return

        engine_version = payload.get("version") if isinstance(payload, dict) else None
        if not isinstance(engine_version, str) or not engine_version.strip():
            logger.debug(
                "vLLM /version at %s returned no version string — skipping known-bad-range gate",
                version_url,
            )
            return

        try:
            from vetinari.runtime.runtime_doctor import validate_runtime_version

            result = validate_runtime_version("vllm", engine_version.strip())
        except Exception as exc:
            logger.warning(
                "Could not validate vLLM engine version %r against supported matrix (%s) — "
                "leaving adapter healthy; re-run vetinari --doctor to see matrix diagnostics",
                engine_version,
                exc,
            )
            return

        if not result.passed and result.is_blocker:
            sources = ", ".join(result.matrix_sources) if result.matrix_sources else "supported_matrix.yaml"
            self._engine_version_block_reason = (
                f"vLLM engine version {engine_version} is blocked by the supported matrix: "
                f"{result.reason} Remediation: pin to a version outside the known-bad range "
                f"(see {sources}). Until then, this adapter is held unhealthy so the router skips it."
            )
            logger.error(self._engine_version_block_reason)

    def get_capabilities(self) -> dict[str, list[str]]:
        """Return inferred capabilities for all discovered models.

        Returns:
            Dict mapping model_id to list of capability tags.
        """
        if not self._discovered_models:
            self.discover_models()
        return {m.id: m.capabilities for m in self._discovered_models}
