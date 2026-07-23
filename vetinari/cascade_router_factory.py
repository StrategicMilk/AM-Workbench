"""Factory and singleton helpers for :mod:`vetinari.cascade_router`."""

from __future__ import annotations

import threading
from typing import Any

_DEFAULT_CONFIDENCE_THRESHOLD = 0.7
_DEFAULT_MAX_ESCALATIONS = 2
_cascade_router: Any = None
_cr_lock = threading.Lock()


def _default_confidence_threshold() -> float:
    from vetinari.utils.lazy_config import env_float

    return env_float("CASCADE_CONFIDENCE_THRESHOLD", _DEFAULT_CONFIDENCE_THRESHOLD)


def _default_max_escalations() -> int:
    from vetinari.utils.lazy_config import env_int

    return env_int("CASCADE_MAX_ESCALATIONS", _DEFAULT_MAX_ESCALATIONS)


def build_cascade_from_router(
    dynamic_router: Any,
    task_type: Any,
    confidence_threshold: float | None = None,
    max_escalations: int | None = None,
) -> Any:
    """Build a CascadeRouter from DynamicModelRouter models, cheapest first.

    Args:
        dynamic_router: Router exposing ``get_available_models()``.
        task_type: Task type associated with the cascade build.
        confidence_threshold: Confidence threshold for escalation.
        max_escalations: Maximum escalation steps after the first tier.

    Returns:
        Configured CascadeRouter instance.
    """
    from vetinari.cascade_router import CascadeRouter

    resolved_confidence_threshold = (
        _default_confidence_threshold() if confidence_threshold is None else confidence_threshold
    )
    resolved_max_escalations = _default_max_escalations() if max_escalations is None else max_escalations
    cascade_router = CascadeRouter(
        confidence_threshold=resolved_confidence_threshold,
        max_escalations=resolved_max_escalations,
    )
    models_sorted = sorted(
        dynamic_router.get_available_models(),
        key=lambda model: (
            getattr(model, "metadata", {}).get("cost_per_1k_tokens", 0.0) if hasattr(model, "metadata") else 0.0,
            getattr(model, "avg_latency_ms", 0.0),
        ),
    )

    for priority, model in enumerate(models_sorted):
        cost = model.metadata.get("cost_per_1k_tokens", 0.0) if hasattr(model, "metadata") and model.metadata else 0.0
        provider = getattr(model, "provider", None)
        provider_value = (
            provider.value if hasattr(provider, "value") else (str(provider) if provider is not None else "")
        )
        if provider_value in ("openai", "anthropic", "cloud"):
            cascade_router.add_cloud_tier(model.id, cost_per_1k_tokens=cost, priority=priority)
        else:
            cascade_router.add_tier(model.id, cost_per_1k_tokens=cost, priority=priority)
    return cascade_router


def get_cascade_router(
    confidence_threshold: float | None = None,
    max_escalations: int | None = None,
) -> Any:
    """Get or create the global CascadeRouter instance.

    Args:
        confidence_threshold: Confidence threshold used on first creation.
        max_escalations: Maximum escalation steps used on first creation.

    Returns:
        Shared CascadeRouter instance.
    """
    global _cascade_router
    resolved_confidence_threshold = (
        _default_confidence_threshold() if confidence_threshold is None else confidence_threshold
    )
    resolved_max_escalations = _default_max_escalations() if max_escalations is None else max_escalations
    if _cascade_router is None:
        with _cr_lock:
            if _cascade_router is None:
                from vetinari.cascade_router import CascadeRouter

                _cascade_router = CascadeRouter(
                    confidence_threshold=resolved_confidence_threshold,
                    max_escalations=resolved_max_escalations,
                )
    return _cascade_router


def reset_cascade_router() -> None:
    """Reset the global CascadeRouter, clearing stats before releasing."""
    global _cascade_router
    with _cr_lock:
        if _cascade_router is not None:
            _cascade_router.reset_stats()
        _cascade_router = None
