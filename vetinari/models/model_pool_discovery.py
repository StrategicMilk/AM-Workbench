"""Discovery workflow helpers for :mod:`vetinari.models.model_pool`."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)


class ModelPoolDiscoveryMixin:
    """Local and router-backed model discovery behavior for ``ModelPool``."""

    if TYPE_CHECKING:
        _discovery_lock: Any
        _discovery_retry_count: Any
        _discovery_retry_delay_base: Any
        _fallback_active: Any
        _last_known_good: Any
        _llama_swap_enabled: Any
        _max_discovery_retries: Any
        _max_discovery_wall_time: Any
        _router_url: Any
        config: Any
        memory_budget_gb: Any
        models: Any

    def _discover_via_llama_swap(self) -> list[dict]:
        """Query llama-swap HTTP API for available models."""
        from vetinari.models.model_pool import _estimate_latency_hint

        try:
            import requests

            resp = requests.get(f"{self._router_url}/api/v1/models", timeout=5)
            resp.raise_for_status()
            data = resp.json()
            models = []
            for entry in data.get("data", data.get("models", [])):
                model_id = entry.get("id") or entry.get("name", "unknown")
                models.append({
                    "id": model_id,
                    "name": model_id,
                    "endpoint": self._router_url,
                    "capabilities": ["coding", "reasoning", "general"],
                    "context_len": entry.get("context_length", 8192),
                    "memory_gb": entry.get("memory_gb", 0),
                    "latency_hint": _estimate_latency_hint(model_id, entry.get("memory_gb", 0)),
                    "version": "",
                })
            logger.info("llama-swap discovered %d models from %s", len(models), self._router_url)
            return models
        except Exception as exc:
            logger.warning("llama-swap discovery failed at %s: %s", self._router_url, exc)
            return []

    def _try_llama_swap_discovery(self) -> bool:
        """Use llama-swap discovery when enabled and populated."""
        if not self._llama_swap_enabled:
            return False
        swap_models = self._discover_via_llama_swap()
        if not swap_models:
            logger.warning("llama-swap returned no models - falling back to local discovery")
            return False
        self.models = swap_models
        self._last_known_good = list(swap_models)
        self._discovery_failed = False
        self._fallback_active = False
        return True

    def _reset_discovery_state(self) -> None:
        """Reset per-run discovery status before local scanning."""
        self._discovery_failed = False
        self._fallback_active = False
        self._discovery_retry_count = 0

    def _discover_local_once(self) -> list[dict[str, Any]]:
        """Run one local adapter discovery attempt."""
        from vetinari.adapters.base import ProviderConfig, ProviderType
        from vetinari.adapters.llama_cpp_adapter import LlamaCppProviderAdapter
        from vetinari.models.model_pool import _estimate_latency_hint

        adapter = LlamaCppProviderAdapter(ProviderConfig(provider_type=ProviderType.LOCAL, name="local", endpoint=""))
        discovered = adapter.discover_models()
        logger.debug(
            "[Model Discovery] Attempt %s/%s: found %s local GGUF files",
            self._discovery_retry_count,
            self._max_discovery_retries,
            len(discovered),
        )

        models: list[dict[str, Any]] = []
        for model in discovered:
            mem = float(model.memory_gb) if model.memory_gb else 0.0
            if mem > self.memory_budget_gb:
                logger.info(
                    "[Model Discovery] Skipping %s - exceeds memory budget (%sGB > %sGB)",
                    model.id,
                    mem,
                    self.memory_budget_gb,
                )
                continue
            models.append({
                "id": model.id,
                "name": model.name,
                "endpoint": model.endpoint,
                "capabilities": model.capabilities,
                "context_len": model.context_len,
                "memory_gb": mem if mem > 0 else max(2.0, self.memory_budget_gb * 0.25),
                "latency_hint": _estimate_latency_hint(model.id, mem),
                "version": "",
            })
        logger.info("[Model Discovery] SUCCESS: Discovered %s local models", len(models))
        return models

    def _handle_local_discovery_error(self, exc: Exception, attempt: int, discovery_start: float) -> bool:
        """Record a failed local discovery attempt and return whether to stop."""
        error_msg = (
            f"Model discovery failed (attempt {self._discovery_retry_count}/{self._max_discovery_retries}): {exc!s}"
        )
        logger.warning("[Model Discovery] %s", error_msg)
        self._last_discovery_error = error_msg
        self._discovery_failed = True

        elapsed = time.monotonic() - discovery_start
        if elapsed >= self._max_discovery_wall_time:
            logger.warning(
                "Model discovery wall-clock limit (%.0fs) reached - stopping retries",
                self._max_discovery_wall_time,
            )
            return True
        if attempt >= self._max_discovery_retries - 1:
            return True
        delay = min(self._discovery_retry_delay_base * (2**attempt), 5.0)
        remaining = self._max_discovery_wall_time - elapsed
        delay = min(delay, max(0, remaining - 0.5))
        if delay > 0:
            logger.info("[Model Discovery] Retrying in %.1fs...", delay)
            time.sleep(delay)
        return False

    def _discover_local_models(self) -> list[dict[str, Any]]:
        """Discover local GGUF models with bounded retries."""
        from vetinari.models.model_pool import _seed_discovered_models

        discovery_start = time.monotonic()
        for attempt in range(self._max_discovery_retries):
            self._discovery_retry_count = attempt + 1
            try:
                models = self._discover_local_once()
            except Exception as exc:
                logger.warning("Exception handled by  discover local models fallback", exc_info=True)
                if self._handle_local_discovery_error(exc, attempt, discovery_start):
                    break
                continue
            self._discovery_failed = False
            self._fallback_active = False
            self._last_known_good = list(models)
            _seed_discovered_models(models)
            return models
        return []

    def _fallback_models(self, previous_models: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return fallback models after local discovery finds none."""
        if previous_models:
            logger.warning(
                "[Model Discovery] FAILED after %s attempts. Using last-known-good (%s models).",
                self._discovery_retry_count,
                len(previous_models),
            )
            self._fallback_active = True
            return list(previous_models)
        logger.warning(
            "[Model Discovery] FAILED after %s attempts. Falling back to static config models.",
            self._discovery_retry_count,
        )
        self._fallback_active = True
        return []

    @staticmethod
    def _merge_static_models(
        discovered_models: list[dict[str, Any]],
        static_models: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Append static-config models not already discovered."""
        merged = list(discovered_models)
        for model in static_models:
            if not any(existing.get("id") == model.get("id") for existing in merged):
                merged.append(model)
        return merged

    def _log_model_inventory(self) -> None:
        """Log final discovery inventory size and fallback state."""
        if self._fallback_active:
            logger.info("[Model Discovery] Using %s models (fallback active)", len(self.models))
        else:
            logger.info("[Model Discovery] Available models: %s", len(self.models))

    def discover_models(self) -> None:
        """Discover local GGUF models via filesystem scan with retry logic."""
        with self._discovery_lock:
            if self._try_llama_swap_discovery():
                return
            previous_models = list(self.models) if self.models else list(self._last_known_good)
            self._reset_discovery_state()
            static_models = self.config.get("models", [])
            new_models = self._discover_local_models()
            if not new_models:
                new_models = self._fallback_models(previous_models)
            self.models = self._merge_static_models(new_models, static_models)
            self._log_model_inventory()
