"""Enhanced Adapter Manager for Vetinari.

Extends the basic AdapterRegistry with:
- Execution context awareness
- Provider health monitoring
- Automatic provider fallback
- Cost optimization and load balancing
- Integration with the tool execution system

This implements provider agnosticism following OpenCode's approach of
being decoupled from any single provider.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vetinari import _adapter_fallback_helpers as fallback_helpers
from vetinari._provider_metrics import ProviderHealthStatus, ProviderMetrics
from vetinari.adapter_manager_autoregistration import auto_register_configured_providers
from vetinari.adapter_manager_cascade import _AdapterManagerCascadeMixin
from vetinari.adapter_manager_strict_cascade import _AdapterManagerStrictCascadeMixin
from vetinari.backend_config import load_backend_runtime_config, resolve_provider_fallback_order
from vetinari.constants import get_user_dir
from vetinari.exceptions import ConfigurationError
from vetinari.execution_context import (
    ToolPermission,
    get_context_manager,
)

if TYPE_CHECKING:
    from vetinari.adapters.base import (
        InferenceRequest,
        InferenceResponse,
        ModelInfo,
        ProviderAdapter,
        ProviderConfig,
    )

logger = logging.getLogger(__name__)

_DEFAULT_CASCADE_RECEIPTS_ENV = "VETINARI_CASCADE_RECEIPTS_PATH"
_DEFAULT_CASCADE_RECEIPTS_DIR = Path("outputs") / "receipts" / "cascade"
_DEFAULT_CASCADE_RECEIPTS_FILE = "route-receipts.jsonl"


class AdapterManager(_AdapterManagerStrictCascadeMixin, _AdapterManagerCascadeMixin):
    """Enhanced adapter management with context awareness and provider selection.

    Also known as: LLM Dispatcher — picks which LLM provider to use for a
    given request and sends the request through that provider's ProviderAdapter
    (LLM Bridge).  Adds health monitoring, automatic fallback, cost
    optimisation, load balancing, and default cascade routing on top of the
    raw AdapterRegistry.
    """

    def __init__(self):
        from vetinari.adapters.registry import AdapterRegistry

        self.registry = AdapterRegistry()
        self._metrics: dict[str, ProviderMetrics] = {}
        self._metrics_lock = threading.Lock()  # Guards all metric counter updates
        self._health_check_interval = timedelta(minutes=5)
        self._last_health_check: dict[str, datetime] = {}
        self._provider_fallback_order: list[str] = []

        # Cascade routing is the default request path; strict config is loaded lazily.
        self._cascade_enabled: bool = True
        self._cascade_provider: str | None = None  # provider to run cascade through
        self._cascade_router: Any = None  # set by enable_cascade_routing()
        self._cascade_config_lock = threading.Lock()  # Guards lazy cascade initialization
        self._cascade_requires_support_matrix: bool = False
        self._support_matrix_path: Path = Path("config/support_matrix.yaml")
        self._route_receipt_path: Path | None = None

    def register_provider(self, config: ProviderConfig, instance_name: str) -> ProviderAdapter:
        """Register a provider with metrics tracking.

        Args:
            config: ProviderConfig instance
            instance_name: Unique name for this provider instance

        Returns:
            The created ProviderAdapter
        """
        adapter = self.registry.create_adapter(config, instance_name)

        # Initialize metrics
        self._metrics[instance_name] = ProviderMetrics(
            name=instance_name,
            provider_type=config.provider_type,
        )

        # Add to fallback order
        if instance_name not in self._provider_fallback_order:
            self._provider_fallback_order.append(instance_name)

        logger.info("Registered provider: %s (%s)", instance_name, config.provider_type.value)
        return adapter

    def get_provider(self, instance_name: str) -> ProviderAdapter | None:
        """Get a specific provider by name."""
        return self.registry.get_adapter(instance_name)

    def list_providers(self) -> dict[str, ProviderAdapter]:
        """List all registered providers."""
        return self.registry.list_adapters()

    def get_metrics(self, instance_name: str | None = None) -> dict[str, Any]:
        """Get metrics for a provider or all providers.

        Args:
            instance_name: Optional specific provider name

        Returns:
            Metrics dictionary
        """
        if instance_name:
            if instance_name in self._metrics:
                return self._metrics[instance_name].to_dict()
            return {}

        return {name: metrics.to_dict() for name, metrics in self._metrics.items()}

    def health_check(self, instance_name: str | None = None) -> dict[str, Any]:
        """Perform health check on provider(s).

        Args:
            instance_name: Optional specific provider name

        Returns:
            Health check results
        """
        if instance_name:
            adapter = self.get_provider(instance_name)
            if not adapter:
                return {}

            try:
                health = adapter.health_check()

                # Update metrics
                if instance_name in self._metrics:
                    status_str = "healthy" if health.get("healthy") else "unhealthy"
                    self._metrics[instance_name].health_status = ProviderHealthStatus[status_str.upper()]
                    self._metrics[instance_name].last_health_check = datetime.now(timezone.utc)

                self._last_health_check[instance_name] = datetime.now(timezone.utc)
                logger.info("Health check %s: %s", instance_name, status_str)
                return {instance_name: health}
            except Exception:
                logger.exception("Health check failed for %s — marking unhealthy", instance_name)
                if instance_name in self._metrics:
                    self._metrics[instance_name].health_status = ProviderHealthStatus.UNHEALTHY
                    self._metrics[instance_name].last_health_check = datetime.now(timezone.utc)
                return {instance_name: {"healthy": False, "reason": "Health check failed"}}

        # Check all providers
        results = {}
        for name in self.list_providers():
            results.update(self.health_check(name))
        return results

    def discover_models(self, instance_name: str | None = None) -> dict[str, list[ModelInfo]]:
        """Discover models from provider(s).

        Args:
            instance_name: Optional specific provider name

        Returns:
            Dictionary mapping provider names to lists of ModelInfo
        """
        if instance_name:
            adapter = self.get_provider(instance_name)
            if not adapter:
                return {}

            try:
                models = adapter.discover_models()
                logger.info("Discovered %s models from %s", len(models), instance_name)
                return {instance_name: models}
            except Exception as e:
                logger.error("Model discovery failed for %s: %s", instance_name, e)
                return {instance_name: []}

        # Discover from all providers
        result = self.registry.discover_all_models()
        total_models = sum(len(models) for models in result.values())
        if total_models == 0:
            from vetinari.adapters.discovery import discover_models as discover_local_model_ids

            local_model_ids = discover_local_model_ids()
            logger.warning(
                "No models discovered by any provider. LLM inference will not be available. "
                "Configure a model provider or place GGUF files in the models directory. "
                "Local model files detected outside a registered provider: %s",
                len(local_model_ids),
            )
        return result

    def select_provider_for_task(
        self,
        task_requirements: dict[str, Any],
        preferred_provider: str | None = None,
    ) -> tuple[str | None, ModelInfo | None]:
        """Select best provider and model for a task.

        Uses metrics (success rate, latency, cost) in selection decision.

        Args:
            task_requirements: Dict with capability and latency requirements
            preferred_provider: Optional preferred provider name

        Returns:
            Tuple of (provider_name, ModelInfo)
        """
        if preferred_provider:
            adapter = self.get_provider(preferred_provider)
            if adapter:
                try:
                    # Try to find a model in preferred provider
                    models = adapter.discover_models()
                    if models:
                        # Find best model in this provider
                        best_model = None
                        best_score = -1.0
                        for model in models:
                            score = adapter.score_model_for_task(model, task_requirements)
                            if score > best_score:
                                best_score = score
                                best_model = model

                        if best_model:
                            logger.info("Selected %s from preferred provider %s", best_model.id, preferred_provider)
                            return preferred_provider, best_model
                except Exception as e:
                    logger.warning("Preferred provider %s failed: %s", preferred_provider, e)

        # Fall back to best model across all providers
        best_adapter, best_model = self.registry.find_best_model(task_requirements)
        if best_adapter:
            provider_name = None
            for name, prov_adapter in self.list_providers().items():
                if prov_adapter == best_adapter:
                    provider_name = name
                    break

            if provider_name and best_model:
                logger.info("Selected %s from provider %s", best_model.id, provider_name)
                return provider_name, best_model

        logger.warning("Could not select a provider for the task")
        return None, None

    def infer(
        self,
        request: InferenceRequest,
        provider_name: str | None = None,
        fallback_on_error: bool = True,
        use_cascade: bool | None = None,
    ) -> InferenceResponse:
        """Execute inference with automatic provider fallback.

        Cascade routing is the default request path. When cascade mode is
        active, the request is routed through ``CascadeRouter``: the cheapest
        tier is tried first and escalation happens automatically when
        confidence is low. If no cascade router has been configured yet, the
        strict default route is loaded from the model-family and support-matrix
        contracts before inference proceeds.

        Args:
            request: InferenceRequest describing the prompt and model.
            provider_name: Optional specific provider instance name.  When
                cascade mode is active this selects which provider executes
                each tier call.
            fallback_on_error: Whether to try other providers on adapter error.
            use_cascade: ``None`` follows the manager default, ``True`` forces
                cascade routing for this call, and ``False`` uses direct
                provider fallback even when the manager default is cascade.

        Returns:
            InferenceResponse with the chosen model's output.
        """
        context_manager = get_context_manager()

        if not context_manager.check_permission(ToolPermission.MODEL_INFERENCE):
            return self._permission_denied_response(request)

        cascade_requested = self._cascade_enabled if use_cascade is None else use_cascade
        if cascade_requested and self._cascade_router is None:
            default_error = self._ensure_default_cascade_routing(request, provider_name)
            if default_error is not None:
                return default_error
        if cascade_requested:
            return self._infer_via_cascade(request, provider_name or self._cascade_provider)

        providers_to_try = self._providers_to_try(provider_name, fallback_on_error)
        try:
            self._warn_if_vram_may_exceed_capacity(request)
        except RuntimeError as exc:
            logger.warning("Inference request exceeds configured VRAM capacity; returning provider failure")
            return self._all_providers_failed_response(request, str(exc))
        return self._infer_with_provider_fallback(request, providers_to_try, fallback_on_error)

    def set_route_receipt_store(self, path: str | Path) -> None:
        """Configure the durable JSONL write/read path for cascade route receipts."""
        self._route_receipt_path = Path(path)

    @staticmethod
    def _default_cascade_receipt_path() -> Path:
        """Return the receipt path used by lazy default cascade routing."""
        override = os.environ.get(_DEFAULT_CASCADE_RECEIPTS_ENV)
        if override:
            return Path(override)
        return get_user_dir() / _DEFAULT_CASCADE_RECEIPTS_DIR / _DEFAULT_CASCADE_RECEIPTS_FILE

    def _resolve_cascade_provider_name(self, provider_name: str | None) -> str | None:
        """Select the provider used to execute cascade tiers for this request."""
        if provider_name:
            return provider_name
        if self._cascade_provider:
            return self._cascade_provider
        if self._provider_fallback_order:
            return self._provider_fallback_order[0]
        return None

    def _ensure_default_cascade_routing(
        self,
        request: InferenceRequest,
        provider_name: str | None,
    ) -> InferenceResponse | None:
        """Configure the strict default cascade before a default-path request runs."""
        if self._cascade_router is not None:
            return None
        with self._cascade_config_lock:
            if self._cascade_router is not None:
                return None
            from vetinari.models.inference_endpoint_capabilities import CapabilityContractError

            resolved_provider = self._resolve_cascade_provider_name(provider_name)
            try:
                self.configure_default_cascade_routing(
                    provider_name=resolved_provider,
                    receipt_path=self._default_cascade_receipt_path(),
                )
            except (CapabilityContractError, ConfigurationError, OSError, TypeError, ValueError) as exc:
                logger.warning(
                    "Default cascade routing could not be configured for model %s; "
                    "inference fails closed instead of bypassing cascade: %s",
                    request.model_id,
                    exc,
                    exc_info=True,
                )
                return self._cascade_error_response(
                    request,
                    f"Cascade routing default configuration unavailable: {exc}",
                )
        return None

    @staticmethod
    def _permission_denied_response(request: InferenceRequest) -> InferenceResponse:
        """Build the inference response returned when model access is denied."""
        return fallback_helpers.permission_denied_response(request)

    def _providers_to_try(self, provider_name: str | None, fallback_on_error: bool) -> list[str]:
        """Return the ordered provider names for the standard fallback path."""
        return fallback_helpers.providers_to_try(
            provider_name,
            fallback_on_error,
            self._provider_fallback_order,
        )

    @staticmethod
    def _warn_if_vram_may_exceed_capacity(request: InferenceRequest) -> None:
        """Log an advisory warning when the requested model may exceed VRAM."""
        fallback_helpers.warn_if_vram_may_exceed_capacity(request)

    def _record_provider_response(self, provider_name: str, response: InferenceResponse) -> None:
        """Update provider metrics after an adapter response."""
        fallback_helpers.record_provider_response(
            provider_name,
            response,
            self._metrics,
            self._metrics_lock,
        )

    def _record_provider_failure(self, provider_name: str) -> None:
        """Increment provider failure metrics after an adapter exception."""
        fallback_helpers.record_provider_failure(provider_name, self._metrics, self._metrics_lock)

    @staticmethod
    def _all_providers_failed_response(request: InferenceRequest, last_error: object) -> InferenceResponse:
        """Build the final error response after provider fallback is exhausted."""
        return fallback_helpers.all_providers_failed_response(request, last_error)

    def _infer_with_provider_fallback(
        self,
        request: InferenceRequest,
        providers_to_try: list[str],
        fallback_on_error: bool,
    ) -> InferenceResponse:
        """Run inference through providers in fallback order until one succeeds."""
        return fallback_helpers.infer_with_provider_fallback(
            request,
            providers_to_try,
            fallback_on_error,
            self.get_provider,
            self._metrics,
            self._metrics_lock,
        )

    def set_fallback_order(self, provider_names: list[str]) -> None:
        """Override the provider fallback sequence used when the preferred provider is unavailable.

        Args:
            provider_names: Ordered list of provider names to try in sequence.
        """
        self._provider_fallback_order = provider_names
        logger.info("Set provider fallback order: %s", provider_names)

    def get_status(self) -> dict[str, Any]:
        """Get comprehensive status of all providers."""
        return {
            "providers": {
                name: {
                    "adapter": str(adapter),
                    "metrics": self._metrics[name].to_dict() if name in self._metrics else {},
                }
                for name, adapter in self.list_providers().items()
            },
            "fallback_order": self._provider_fallback_order,
        }


# Global adapter manager instance
_adapter_manager: AdapterManager | None = None
_adapter_manager_lock = threading.Lock()
_ADAPTER_BASE_EXPORTS = {
    "InferenceRequest",
    "InferenceResponse",
    "ModelInfo",
    "ProviderAdapter",
    "ProviderConfig",
    "ProviderType",
}


def __getattr__(name: str) -> Any:
    """Resolve legacy adapter-manager exports without eager adapter imports."""
    if name == "AdapterRegistry":
        from vetinari.adapters.registry import AdapterRegistry

        return AdapterRegistry
    if name in _ADAPTER_BASE_EXPORTS:
        from vetinari.adapters import base as adapter_base

        return getattr(adapter_base, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_adapter_manager() -> AdapterManager:
    """Get or create the global adapter manager.

    Auto-registers the local llama-cpp-python provider on first access
    so that inference works out of the box when GGUF models are available.

    Returns:
        The AdapterManager singleton.
    """
    global _adapter_manager
    if _adapter_manager is None:
        with _adapter_manager_lock:
            if _adapter_manager is None:
                _adapter_manager = AdapterManager()
                auto_register_configured_providers(
                    _adapter_manager,
                    load_backend_runtime_config_fn=load_backend_runtime_config,
                    resolve_provider_fallback_order_fn=resolve_provider_fallback_order,
                )

    return _adapter_manager
