"""Provider auto-registration helpers for :mod:`vetinari.adapter_manager`."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from vetinari.backend_config import load_backend_runtime_config, resolve_provider_fallback_order

logger = logging.getLogger(__name__)


def auto_register_configured_providers(
    manager: Any,
    *,
    load_backend_runtime_config_fn: Callable[[], dict[str, Any]] = load_backend_runtime_config,
    resolve_provider_fallback_order_fn: Callable[..., list[str]] = resolve_provider_fallback_order,
) -> None:
    """Register configured inference providers on an adapter manager.

    Args:
        manager: AdapterManager-like object with ``register_provider``,
            ``list_providers``, and ``set_fallback_order`` methods.
        load_backend_runtime_config_fn: Runtime-config loader, injectable for
            compatibility with existing adapter-manager tests.
        resolve_provider_fallback_order_fn: Fallback-order resolver, injectable
            for compatibility with existing adapter-manager tests.
    """
    if os.environ.get("VETINARI_TESTING") or os.environ.get("VETINARI_DISABLE_AUTO_REGISTRATION"):
        logger.info("Adapter auto-registration disabled by environment")
        return

    try:
        from vetinari.adapters.base import ProviderConfig, ProviderType

        runtime_cfg = load_backend_runtime_config_fn()
        local_inference = runtime_cfg.get("local_inference", {})
        inference_backend = runtime_cfg.get("inference_backend", {})
        extra = {
            "models_dir": str(local_inference.get("models_dir", os.environ.get("VETINARI_MODELS_DIR", ""))),
            "gpu_layers": str(os.environ.get("VETINARI_GPU_LAYERS", local_inference.get("gpu_layers", -1))),
            "context_length": str(
                os.environ.get("VETINARI_CONTEXT_LENGTH", local_inference.get("context_length", 8192)),
            ),
            "ram_budget_gb": str(local_inference.get("ram_budget_gb", 30)),
            "cpu_offload_enabled": str(local_inference.get("cpu_offload_enabled", True)),
        }
        hardware = runtime_cfg.get("hardware", {})
        local_config = ProviderConfig(
            provider_type=ProviderType.LOCAL,
            name="local",
            endpoint="",
            memory_budget_gb=hardware.get("gpu_vram_gb", 32) if hardware else 32,
            extra_config=extra,
        )
        manager.register_provider(local_config, "local")
        logger.info("Auto-registered local inference provider")

        _register_endpoint_provider(manager, ProviderConfig, ProviderType.VLLM, "vllm", inference_backend)
        _register_endpoint_provider(manager, ProviderConfig, ProviderType.NIM, "nim", inference_backend)
        _register_endpoint_provider(
            manager,
            ProviderConfig,
            ProviderType.AM_ENGINE,
            "am_engine",
            inference_backend,
        )

        available = set(manager.list_providers())
        fallback_order = resolve_provider_fallback_order_fn(runtime_cfg, available_providers=available)
        if fallback_order:
            manager.set_fallback_order(fallback_order)
            logger.info("Configured backend fallback order: %s", fallback_order)
    except Exception as exc:
        logger.warning("Failed to auto-register local provider: %s", exc)


def _register_endpoint_provider(
    manager: Any,
    provider_config_type: Any,
    provider_type: Any,
    provider_name: str,
    inference_backend: dict[str, Any],
) -> None:
    """Register one enabled endpoint provider from backend config."""
    provider_cfg = inference_backend.get(provider_name, {})
    if not provider_cfg.get("enabled", False) or not provider_cfg.get("endpoint"):
        return

    try:
        extra = {key: value for key, value in provider_cfg.items() if key not in {"enabled", "endpoint"}}
        config = provider_config_type(
            provider_type=provider_type,
            name=provider_name,
            endpoint=provider_cfg["endpoint"],
            extra_config=extra,
        )
        manager.register_provider(config, provider_name)
        logger.info("Auto-registered %s provider at %s", provider_name, provider_cfg["endpoint"])
    except Exception as exc:
        logger.warning("Failed to auto-register %s provider: %s", provider_name, exc)
