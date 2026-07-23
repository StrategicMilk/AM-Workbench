"""Cascade-routing support for :mod:`vetinari.adapter_manager`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from vetinari.constants import INFERENCE_STATUS_ERROR, INFERENCE_STATUS_OK
from vetinari.exceptions import ConfigurationError

if TYPE_CHECKING:
    from vetinari.adapters.base import InferenceRequest, InferenceResponse

logger = logging.getLogger(__name__)


class _AdapterManagerCascadeMixin:
    """Cascade cost-optimization behavior for ``AdapterManager``."""

    if TYPE_CHECKING:
        _cascade_enabled: Any
        _cascade_router: Any
        _metrics: Any
        _metrics_lock: Any
        _provider_fallback_order: Any
        get_provider: Any

    def _infer_via_cascade(
        self,
        request: InferenceRequest,
        provider_name: str | None,
    ) -> InferenceResponse:
        """Run inference through the configured CascadeRouter.

        Builds an adapter function that dispatches each tier's request to the
        named provider (or the first available provider if none is specified),
        then delegates to ``CascadeRouter.route()``.

        Args:
            request: The original InferenceRequest (model_id will be
                overridden per tier by CascadeRouter).
            provider_name: Provider instance name to use for all tier calls.
                Falls back to the first registered provider when ``None``.

        Returns:
            InferenceResponse assembled from the winning cascade tier.
        """
        from vetinari.adapters.base import InferenceResponse

        prov_name = provider_name
        if not prov_name and self._provider_fallback_order:
            prov_name = self._provider_fallback_order[0]

        adapter = self.get_provider(prov_name) if prov_name else None

        if adapter is None:
            logger.warning(
                "CascadeRouter: no provider available (name=%s), falling back to error response",
                prov_name,
            )
            return InferenceResponse(
                model_id=request.model_id,
                output="",
                latency_ms=0,
                tokens_used=0,
                status=INFERENCE_STATUS_ERROR,
                error="Cascade routing: no provider configured",
            )

        def _adapter_fn(tier_request: InferenceRequest) -> InferenceResponse:
            """Dispatch a single tier request through the resolved provider."""
            return adapter.infer(tier_request)

        try:
            cascade_result = self._cascade_router.route(request, adapter_fn=_adapter_fn)
        except Exception as exc:
            logger.error("CascadeRouter.route() failed: %s", exc)
            return InferenceResponse(
                model_id=request.model_id,
                output="",
                latency_ms=0,
                tokens_used=0,
                status=INFERENCE_STATUS_ERROR,
                error=f"Cascade routing failed: {exc}",
            )

        logger.info(
            "Cascade routing complete: model=%s, confidence=%.3f, escalations=%d, tiers=%s",
            cascade_result.model_id,
            cascade_result.confidence,
            cascade_result.escalation_count,
            cascade_result.tiers_tried,
        )

        if prov_name and prov_name in self._metrics:
            metrics = self._metrics[prov_name]
            response = cascade_result.response
            with self._metrics_lock:
                if getattr(response, "status", INFERENCE_STATUS_ERROR) == INFERENCE_STATUS_OK:
                    metrics.successful_inferences += 1
                    metrics.total_tokens_used += getattr(response, "tokens_used", 0)
                else:
                    metrics.failed_inferences += 1

        return cast(InferenceResponse, cascade_result.response)

    def enable_cascade_routing(
        self,
        tiers: list[dict[str, float | str]],
        provider_name: str | None = None,
        confidence_threshold: float = 0.7,
        max_escalations: int = 2,
    ) -> None:
        """Configure cascade cost-optimisation routing for inference.

        When enabled, ``infer()`` will try the cheapest model tier first and
        escalate to more capable (expensive) tiers only when response
        confidence is below the threshold. AdapterManager uses a strict default
        cascade lazily; this method lets callers supply an explicit cascade
        topology.

        Args:
            tiers: Ordered list of tier descriptors. Each dict must have a
                ``model_id`` key (str) and may include ``cost_per_1k_tokens``
                (float, default 0.0). Tiers are tried cheapest-first, so
                sort by ascending cost before passing.
            provider_name: Provider instance name to route cascade requests
                through. When ``None``, the first registered provider is used.
            confidence_threshold: Minimum confidence score to accept a
                response without escalating. Range [0, 1], default 0.7.
            max_escalations: Maximum number of escalation steps after the
                first tier attempt. Default 2.

        Raises:
            ConfigurationError: If ``tiers`` is empty or a tier dict lacks
                ``model_id``.
        """
        if not tiers:
            raise ConfigurationError("enable_cascade_routing: tiers must not be empty")
        if not 0.0 <= float(confidence_threshold) <= 1.0:
            raise ConfigurationError("enable_cascade_routing: confidence_threshold must be in range [0.0, 1.0]")
        if max_escalations < 0:
            raise ConfigurationError("enable_cascade_routing: max_escalations must be >= 0")

        from vetinari.cascade_router import CascadeRouter

        cascade = CascadeRouter(
            confidence_threshold=confidence_threshold,
            max_escalations=max_escalations,
        )
        for i, tier in enumerate(tiers):
            model_id = tier.get("model_id")
            if not model_id:
                raise ConfigurationError(
                    f"enable_cascade_routing: each tier dict must contain 'model_id', got {tier!r}",
                )
            cost = float(tier.get("cost_per_1k_tokens", 0.0))
            cascade.add_tier(str(model_id), cost_per_1k_tokens=cost, priority=i)

        self._cascade_router = cascade
        self._cascade_enabled = True
        self._cascade_provider = provider_name
        logger.info(
            "Cascade routing enabled: %d tiers, threshold=%.2f, max_escalations=%d",
            len(tiers),
            confidence_threshold,
            max_escalations,
        )

    def disable_cascade_routing(self) -> None:
        """Disable cascade routing and restore default provider-fallback routing."""
        self._cascade_enabled = False
        logger.info("Cascade routing disabled")

    def get_cascade_stats(self) -> dict[str, Any]:
        """Return cascade routing statistics, or empty dict if not enabled.

        Returns:
            Stats dict from CascadeRouter, or empty dict when cascade is off.
        """
        if self._cascade_enabled and self._cascade_router is not None:
            return cast(dict[str, Any], self._cascade_router.get_stats())
        return {}
