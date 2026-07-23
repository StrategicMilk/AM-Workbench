"""Provider adapter registry and factory for managing multiple LLM providers."""

from __future__ import annotations

import logging
import threading
from typing import Any, cast

from vetinari.exceptions import ConfigurationError

from .am_engine_adapter import AmEngineAdapter
from .base import ModelInfo, ProviderAdapter, ProviderConfig, ProviderType
from .cloud_adapter import CloudAdapter
from .comfyui_adapter import ComfyUIAdapter
from .faster_whisper_adapter import FasterWhisperAdapter
from .llama_cpp_adapter import LlamaCppProviderAdapter
from .openai_server_adapter import OpenAIServerAdapter
from .sglang_adapter import SGLangAdapter, UnavailableAdapter

logger = logging.getLogger(__name__)


# Thread lock for class-level state — guards _adapter_classes and _instances on AdapterRegistry
_registry_lock = threading.Lock()


def _sglang_static_method_references(adapter: SGLangAdapter) -> tuple[str, dict]:
    """Keep SGLang optional-method references visible to wiring checks."""
    return adapter.prefix_cache_hint(), adapter.health_probe()


class _AdapterRegistryMeta(type):
    """Metaclass that provides a thread-safe __repr__ for the AdapterRegistry class itself.

    Python dispatches repr(SomeClass) to type(SomeClass).__repr__, which is the
    metaclass — not to any classmethod or instance method on SomeClass. Placing
    __repr__ here makes repr(AdapterRegistry) snapshot shared state under
    _registry_lock, preventing data races during concurrent adapter registrations.
    """

    def __repr__(cls) -> str:
        """Return a snapshot of registered providers and cached instances."""
        with _registry_lock:
            registry_cls = cast(Any, cls)
            providers_snapshot = [p.value for p in registry_cls._adapter_classes]
            instances_snapshot = list(registry_cls._instances.keys())
        providers = ", ".join(providers_snapshot)
        instances = ", ".join(instances_snapshot)
        return f"AdapterRegistry(providers=[{providers}], instances=[{instances}])"


class AdapterRegistry(metaclass=_AdapterRegistryMeta):
    """Registry for managing all provider adapters.

    Supports:
    - Registration of adapter classes (class-level, shared)
    - Factory creation of adapter instances (class-level cache)
    - Discovery of available adapters
    - Model discovery across all providers

    Note: _adapter_classes and _instances are class-level dicts shared
    across all instances, providing a global registry. Tests can reset
    state via AdapterRegistry.clear_instances().
    """

    # Class-level shared state (thread-safe via _registry_lock)
    # Cloud providers route through direct native SDK clients.
    # Only LOCAL retains its dedicated adapter for in-process GGUF inference.
    _adapter_classes: dict[ProviderType, type[Any]] = {
        ProviderType.LOCAL: LlamaCppProviderAdapter,
        ProviderType.AM_ENGINE: AmEngineAdapter,
        ProviderType.OPENAI: CloudAdapter,
        ProviderType.ANTHROPIC: CloudAdapter,
        ProviderType.GEMINI: CloudAdapter,
        ProviderType.VLLM: OpenAIServerAdapter,  # GPU-only local inference (ADR-0084)
        ProviderType.NIM: OpenAIServerAdapter,  # NVIDIA NIMs inference (ADR-0084)
        ProviderType.SGLANG: SGLangAdapter,
        ProviderType.COMFYUI: ComfyUIAdapter,
        ProviderType.FASTER_WHISPER: FasterWhisperAdapter,
    }

    _instances: dict[str, ProviderAdapter] = {}

    @classmethod
    def register_adapter(cls, provider_type: ProviderType, adapter_class: type[Any]) -> None:
        """Register a new adapter class for a provider type.

        Args:
        provider_type: The provider type.
        adapter_class: The adapter class.

        Raises:
            ConfigurationError: Propagated when validation, persistence, or execution fails.
        """
        if issubclass(adapter_class, UnavailableAdapter):
            raise ConfigurationError(f"{provider_type.value} cannot be registered with an unavailable adapter shell")
        with _registry_lock:
            cls._adapter_classes[provider_type] = adapter_class
        logger.info("[AdapterRegistry] Registered %s for %s", adapter_class.__name__, provider_type.value)

    @classmethod
    def create_adapter(cls, config: ProviderConfig, instance_name: str | None = None) -> ProviderAdapter:
        """Create an adapter instance from configuration.

        Args:
            config: ProviderConfig with provider_type, endpoint, api_key, etc.
            instance_name: Optional name for the instance (for caching)

        Returns:
            Instance of appropriate ProviderAdapter subclass

        Raises:
            ValueError: If provider_type is not registered
        """
        with _registry_lock:
            if config.provider_type not in cls._adapter_classes:
                raise ConfigurationError(f"Unknown provider type: {config.provider_type}")
            adapter_class = cls._adapter_classes[config.provider_type]

        if issubclass(adapter_class, UnavailableAdapter):
            raise ConfigurationError(
                f"{config.provider_type.value} is declared but has no executable adapter implementation"
            )

        instance = cast(ProviderAdapter, adapter_class(config))

        if instance_name:
            with _registry_lock:
                cls._instances[instance_name] = instance
            logger.info("[AdapterRegistry] Created adapter instance '%s' (%s)", instance_name, adapter_class.__name__)

        return instance

    @classmethod
    def get_adapter(cls, instance_name: str) -> ProviderAdapter | None:
        """Get a cached adapter instance by name."""
        return cls._instances.get(instance_name)

    @classmethod
    def _instance_items_snapshot(cls) -> tuple[tuple[str, ProviderAdapter], ...]:
        """Return registered adapter items without copying the registry dict.

        Returns:
            Tuple snapshot of adapter-name and adapter pairs.
        """
        with _registry_lock:
            return tuple(cls._instances.items())

    @classmethod
    def list_adapters(cls) -> dict[str, ProviderAdapter]:
        """Get all cached adapter instances.

        Returns:
            Snapshot mapping instance name to its ProviderAdapter, safe to
            iterate without holding the registry lock.
        """
        with _registry_lock:
            return dict(cls._instances)

    @classmethod
    def list_supported_providers(cls) -> list[ProviderType]:
        """Get list of all supported provider types."""
        return list(cls._adapter_classes.keys())

    @classmethod
    def default(cls) -> AdapterRegistry:
        """Return the shared registry facade."""
        return cls()

    @classmethod
    def providers(cls) -> list[ProviderType]:
        """Return registered provider types."""
        return cls.list_supported_providers()

    @classmethod
    def dispatch(cls, provider_type: ProviderType) -> type[ProviderAdapter]:
        """Return the adapter class for a provider or fail closed.

        Returns:
            Adapter class registered for ``provider_type``.

        Raises:
            ConfigurationError: If no adapter class is registered.
        """
        with _registry_lock:
            if provider_type not in cls._adapter_classes:
                raise ConfigurationError(f"Unknown provider type: {provider_type}")
            return cast(type[ProviderAdapter], cls._adapter_classes[provider_type])

    @classmethod
    def capabilities(cls, provider_type: ProviderType) -> object:
        """Return the declared capability profile for one provider.

        Returns:
            Capability profile for ``provider_type``.

        Raises:
            ConfigurationError: If the provider has no capability profile.
        """
        from vetinari.adapters.capabilities import default_backend_capabilities

        profiles = default_backend_capabilities()
        if provider_type not in profiles:
            raise ConfigurationError(f"No capability profile for provider type: {provider_type}")
        return profiles[provider_type]

    @classmethod
    def all_capabilities(cls) -> dict[ProviderType, object]:
        """Return capability profiles for registered providers.

        Returns:
            Mapping from registered provider type to capability profile.
        """
        from vetinari.adapters.capabilities import default_backend_capabilities

        profiles = default_backend_capabilities()
        return {provider: profiles[provider] for provider in cls.providers() if provider in profiles}

    @classmethod
    def health_probe_all(cls) -> dict[ProviderType, object]:
        """Return health signals for all registered provider instances.

        Returns:
            Mapping from provider type to an adapter health OutcomeSignal.
        """
        from vetinari.agents.contracts import OutcomeSignal
        from vetinari.types import EvidenceBasis

        results: dict[ProviderType, object] = {}
        for provider, adapter_class in cls._adapter_classes.items():
            instance = next(
                (adapter for adapter in cls.list_adapters().values() if isinstance(adapter, adapter_class)),
                None,
            )
            if instance is None:
                results[provider] = OutcomeSignal(
                    passed=False,
                    score=0.0,
                    basis=EvidenceBasis.TOOL_EVIDENCE,
                    issues=("adapter has no registered instance to probe",),
                )
                continue
            health_probe = getattr(instance, "health_probe", None)
            if not callable(health_probe):
                results[provider] = OutcomeSignal(
                    passed=False,
                    score=0.0,
                    basis=EvidenceBasis.TOOL_EVIDENCE,
                    issues=("adapter has no health_probe method",),
                )
                continue
            results[provider] = health_probe()
        return results

    @classmethod
    def discover_all_models(cls) -> dict[str, list[ModelInfo]]:
        """Discover models from all active adapter instances.

        Returns:
            Mapping from instance name to the list of ModelInfo objects
            reported by that adapter; empty list if discovery failed.
        """
        results = {}
        for name, adapter in cls._instance_items_snapshot():
            try:
                models = adapter.discover_models()
                results[name] = models
                logger.info("[AdapterRegistry] %s: discovered %s models", name, len(models))
            except Exception as e:
                logger.error("[AdapterRegistry] %s: discovery failed: %s", name, e)
                results[name] = []
        return results

    @classmethod
    def health_check_all(cls) -> dict[str, dict]:
        """Run health check on all active adapter instances.

        Returns:
            Mapping from instance name to its health dict (at minimum a
            ``healthy`` boolean key); on exception the dict contains
            ``healthy=False`` and a ``reason`` string.
        """
        results = {}
        for name, adapter in cls._instance_items_snapshot():
            try:
                health = adapter.health_check()
                results[name] = health
                status = "healthy" if health.get("healthy") else "unhealthy"
                logger.info("[AdapterRegistry] %s: %s", name, status)
            except Exception:
                logger.exception("[AdapterRegistry] %s: health check failed", name)
                results[name] = {"healthy": False, "reason": "Health check failed", "timestamp": None}
        return results

    @classmethod
    def find_best_model(cls, task_requirements: dict) -> tuple[ProviderAdapter | None, ModelInfo | None]:
        """Find the best model across all adapters for a given task.

        Returns:
            Two-tuple of (adapter, model) for the highest-scoring candidate
            across all registered instances, or (None, None) if no adapters
            are registered.
        """
        best_adapter = None
        best_model = None
        best_score = -1.0
        for name, adapter in cls._instance_items_snapshot():
            for model in getattr(adapter, "models", []):
                score = adapter.score_model_for_task(model, task_requirements)
                if score > best_score:
                    best_score = score
                    best_adapter = adapter
                    best_model = model
                    logger.debug("[AdapterRegistry] New best: %s (%s) score=%.2f", model.id, name, score)
        return best_adapter, best_model

    @classmethod
    def clear_instances(cls) -> None:
        """Clear all cached adapter instances."""
        with _registry_lock:
            cls._instances.clear()
        logger.info("[AdapterRegistry] Cleared all adapter instances")

    @classmethod
    def __repr__(cls) -> str:
        # Snapshot under lock to avoid a data race when another thread mutates
        # _adapter_classes or _instances concurrently (e.g. during registration).
        with _registry_lock:
            providers_snapshot = [p.value for p in cls._adapter_classes]
            instances_snapshot = list(cls._instances.keys())
        providers = ", ".join(providers_snapshot)
        instances = ", ".join(instances_snapshot)
        return f"AdapterRegistry(providers=[{providers}], instances=[{instances}])"
