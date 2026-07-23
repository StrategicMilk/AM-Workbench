"""Model health helpers."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def check_model_health(models: list[Any] | None = None) -> dict[str, Any]:
    """Check model health.

    Args:
        models: Models to inspect.

    Returns:
        Model health mapping.
    """
    model_count = len(models or [])
    invalid_model_count = sum(1 for model in models or [] if model is None)
    healthy = model_count > 0 and invalid_model_count == 0
    result: dict[str, Any] = {
        "healthy": healthy,
        "model_count": model_count,
        "invalid_model_count": invalid_model_count,
    }
    if not healthy:
        result["reason"] = "no models" if model_count == 0 else "invalid model entry"
    return result


def check_composite_model_health(adapter_manager: Any | None = None) -> dict[str, Any]:
    """Check composite model health.

    Args:
        adapter_manager: Optional adapter manager.

    Returns:
        Composite health mapping.
    """
    if adapter_manager is None:
        return {"healthy": False, "reason": "no adapter manager"}
    list_providers = getattr(adapter_manager, "list_providers", None)
    if callable(list_providers):
        try:
            providers = list_providers()
        except Exception as exc:
            logger.warning("Provider listing failed during composite model health check", exc_info=True)
            return {"healthy": False, "reason": f"provider listing failed: {exc}"}
        if not providers:
            return {"healthy": False, "reason": "no providers"}
        return {"healthy": True, "reason": "", "provider_count": len(providers)}
    return {"healthy": True, "reason": ""}


__all__ = ["check_composite_model_health", "check_model_health"]
