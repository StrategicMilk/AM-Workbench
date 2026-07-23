"""Dynamic Model Router for Vetinari.

Provides intelligent model selection based on task requirements, capability
matching, performance history, latency, cost, and availability.  Supports
both local and cloud models, configurable ``RoutingPolicy``, and an optional
``PonderEngine`` scoring backend via dependency injection.

This is step 2 of the model-selection pipeline:
Discovery -> **Routing** -> Inference.

All type definitions (``ModelCapabilities``, ``ModelInfo``, ``ModelSelection``,
``RoutingPolicy``, ``TaskType`` alias) live in
``vetinari.models.model_router_types`` and are re-exported from here for
backward compatibility.

Pure scoring helpers (``assess_difficulty``, ``parse_model_size_b``,
``calculate_confidence``, ``generate_reasoning``, ``infer_task_type``) live in
``vetinari.models.model_router_scoring`` and are re-exported from here.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from vetinari.contracts import ConfigContractViolation, fail_closed_config_load
from vetinari.models.dynamic_model_router_scoring import (
    _IMPLICIT_BOOST_CAP,
    _IMPLICIT_BOOST_MATURITY,
)
from vetinari.models.dynamic_model_router_selection import DynamicModelRouterSelectionMixin
from vetinari.models.model_router_catalog import ModelRouterCatalogAccess
from vetinari.models.model_router_scoring import (  # re-exported for callers
    assess_difficulty,
    assess_warm_model_bonus,
    calculate_confidence,
    generate_reasoning,
    infer_task_type,
    parse_model_size_b,
)
from vetinari.models.model_router_types import (  # re-exported for callers
    _TASK_TYPE_COMPAT,
    ModelCapabilities,
    ModelInfo,
    ModelSelection,
    ModelStatus,
    TaskType,
    parse_task_type,
)
from vetinari.models.model_router_types import RouterTypePolicy as RoutingPolicy
from vetinari.types import ModelProvider

logger = logging.getLogger(__name__)


# ── Public re-exports (backward compat) ──────────────────────────────────────
__all__ = [
    "_IMPLICIT_BOOST_CAP",
    "_IMPLICIT_BOOST_MATURITY",
    "_TASK_TYPE_COMPAT",
    "DynamicModelRouter",
    "ModelCapabilities",
    "ModelInfo",
    "ModelProvider",
    "ModelSelection",
    "ModelStatus",
    "RoutingPolicy",
    "TaskType",
    "assess_difficulty",
    "assess_warm_model_bonus",
    "calculate_confidence",
    "generate_reasoning",
    "get_dynamic_router",
    "get_model_router",
    "infer_task_type",
    "init_model_router",
    "parse_model_size_b",
    "parse_task_type",
]

# =====================================================================
# DynamicModelRouter
# =====================================================================


class DynamicModelRouter(DynamicModelRouterSelectionMixin, ModelRouterCatalogAccess):
    """Dynamic model routing based on task requirements and model capabilities.

    Features:
    - Task-type aware model selection
    - Performance-based routing
    - Latency optimization
    - Cost optimization (for cloud models)
    - Fallback handling
    - Model health checking
    - Configurable RoutingPolicy (merged from ModelRelay)
    - Optional PonderEngine scoring backend (dependency injection)

    Catalog methods (registration, queries, health, stats, performance) are
    inherited from ``ModelRouterCatalogAccess`` in ``model_router_catalog``.
    """

    def __init__(
        self,
        prefer_local: bool = True,
        max_latency_ms: float = 60000,
        max_memory_gb: float = 64,
        ponder_engine: Any | None = None,
    ):
        """Initialize the model router.

        Args:
            prefer_local: Prefer local models over cloud when possible.
            max_latency_ms: Maximum acceptable latency in milliseconds.
            max_memory_gb: Maximum memory budget in gigabytes.
            ponder_engine: Optional PonderEngine instance for scoring.
        """
        self.prefer_local = prefer_local
        self.max_latency_ms = max_latency_ms
        self.max_memory_gb = max_memory_gb
        self._ponder_engine = ponder_engine

        from vetinari.config import model_config

        try:
            fail_closed_config_load(model_config._CONFIG_PATH)
        except ConfigContractViolation:
            raise
        try:
            self._model_config: dict[str, Any] = model_config.load_model_config()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ConfigContractViolation(
                path=model_config._CONFIG_PATH,
                reason=f"Model config loader failed: {exc}",
            ) from exc

        self._task_defaults: dict[str, str] = self._model_config.get("task_defaults", {})
        self._model_tiers: list[dict[str, Any]] = self._model_config.get("model_tiers", [])

        # Model registry: keyed by model_id
        self.models: dict[str, ModelInfo] = {}

        # Performance tracking: keyed by "model_id:task_type"
        self._performance_cache: dict[str, dict[str, Any]] = {}

        # Selection history (capped to avoid unbounded growth)
        self._selection_history: deque[dict[str, Any]] = deque(maxlen=500)
        self._last_implicit_boost: dict[str, dict[str, Any]] = {}

        self._health_check_callback: Callable | None = None

        logger.info("DynamicModelRouter initialized (prefer_local=%s)", prefer_local)


# =====================================================================
# Global singleton accessors
# =====================================================================

_model_router: DynamicModelRouter | None = None
_model_router_lock = threading.Lock()


def get_model_router() -> DynamicModelRouter:
    """Get or create the global DynamicModelRouter singleton.

    Returns:
        The singleton DynamicModelRouter instance.
    """
    global _model_router
    if _model_router is None:
        with _model_router_lock:
            if _model_router is None:
                _model_router = DynamicModelRouter()
    return _model_router


# Legacy alias used by assignment_pass.py and other callers
get_dynamic_router = get_model_router


def init_model_router(prefer_local: bool = True, **kwargs) -> DynamicModelRouter:
    """Initialize a new model router and inject PonderEngine if available.

    Args:
        prefer_local: Prefer local models over cloud when possible.
        **kwargs: Additional keyword arguments passed to DynamicModelRouter.

    Returns:
        The initialized DynamicModelRouter with PonderEngine wired in when
        the ponder module is importable.
    """
    global _model_router
    _model_router = DynamicModelRouter(prefer_local=prefer_local, **kwargs)

    try:
        from vetinari.models.ponder import PonderEngine

        _model_router.set_ponder_engine(PonderEngine())
        logger.debug("PonderEngine injected into model router")
    except (ImportError, Exception):
        logger.warning("PonderEngine not available — model router using local scoring only")

    return _model_router
