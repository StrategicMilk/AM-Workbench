"""Cascade Model Router — vetinari.cascade_router.

Implements a cost-optimised routing strategy that starts inference with the
cheapest/smallest model and escalates to larger models when the response
confidence is below a configurable threshold.

Strategy
--------
1. Try cheapest model in the cascade chain.
2. Evaluate confidence of the response (heuristic or model-provided score).
3. If confidence >= threshold  →  return response (done, cheap).
4. If confidence <  threshold  →  escalate to next model in chain.
5. Repeat until the chain is exhausted or confidence is satisfied.

The caller always gets *a* response — the best one seen across the chain.

Integration with DynamicModelRouter
------------------------------------
``CascadeRouter`` can be used as a standalone router or wired into
``DynamicModelRouter`` as a routing strategy::

    from vetinari.cascade_router import CascadeRouter, get_cascade_router
    from vetinari.models.dynamic_model_router import DynamicModelRouter, TaskType

    cr = get_cascade_router()
    cr.add_tier("small-model", cost_per_1k=0.001, priority=0)
    cr.add_tier("medium-model", cost_per_1k=0.005, priority=1)
    cr.add_tier("large-model", cost_per_1k=0.015, priority=2)

    response, used_model = cr.route(request, adapter_fn=my_adapter)

Configuration via environment
------------------------------
- ``CASCADE_CONFIDENCE_THRESHOLD``  — minimum confidence to accept (default 0.7)
- ``CASCADE_MAX_ESCALATIONS``       — max number of escalation steps (default 2)
- ``CASCADE_ENABLED``               — set to "0" to disable cascading
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from vetinari.cascade_confidence import heuristic_confidence as _heuristic_confidence
from vetinari.cascade_router_factory import build_cascade_from_router, get_cascade_router, reset_cascade_router
from vetinari.constants import INFERENCE_STATUS_OK
from vetinari.exceptions import ConfigurationError
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)

__all__ = (
    "CascadeResult",
    "CascadeRouter",
    "CascadeTier",
    "build_cascade_from_router",
    "get_cascade_router",
    "reset_cascade_router",
)


_CONFIDENCE_THRESHOLD = float(os.environ.get("CASCADE_CONFIDENCE_THRESHOLD", "0.7"))
_MAX_ESCALATIONS = int(os.environ.get("CASCADE_MAX_ESCALATIONS", "2"))
_CASCADE_ENABLED = os.environ.get("CASCADE_ENABLED", "1").lower() not in ("0", "false", "no")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class CascadeTier:
    """A single model tier in the cascade chain."""

    model_id: str
    cost_per_1k_tokens: float = 0.0
    priority: int = 0  # lower = tried first
    max_tokens_override: int | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"CascadeTier(model_id={self.model_id!r}, priority={self.priority!r}, cost_per_1k_tokens={self.cost_per_1k_tokens!r})"


@dataclass
class CascadeResult:
    """Result of a cascade routing attempt."""

    response: Any  # InferenceResponse
    model_id: str
    confidence: float
    escalation_count: int = 0
    total_latency_ms: float = 0.0
    tiers_tried: list[str] = field(default_factory=list)
    cost_saved_vs_largest: float = 0.0

    def __repr__(self) -> str:
        return (
            f"CascadeResult(model_id={self.model_id!r}, confidence={self.confidence!r}, "
            f"escalation_count={self.escalation_count!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this CascadeResult to a plain dictionary.

        Returns:
            Dictionary containing model ID, confidence, escalation count,
            latency, tiers tried, and cost savings fields.
        """
        return dataclass_to_dict(self)


# ---------------------------------------------------------------------------
# Confidence estimators
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CascadeRouter
# ---------------------------------------------------------------------------


class CascadeRouter:
    """Cost-optimising cascade router.

    Also known as: Model Escalator — tries the cheapest/smallest model first
    and escalates to progressively larger, more capable models only when the
    response confidence is below the configured threshold.  The caller always
    receives the best response seen across the escalation chain.

    Tries the cheapest model first; escalates to more capable models
    only when confidence is below the threshold.

    Thread-safe. Can be used standalone or integrated into DynamicModelRouter.
    """

    def __init__(
        self,
        confidence_threshold: float = _CONFIDENCE_THRESHOLD,
        max_escalations: int = _MAX_ESCALATIONS,
        enabled: bool = _CASCADE_ENABLED,
        confidence_estimator: Callable[[str], float] | None = None,
        queue_depth_failover_threshold: int = 0,
    ):
        """Configure the cascade router with optional queue-depth cloud failover.

        Args:
            confidence_threshold: Accept response if confidence >= this value.
            max_escalations: Maximum number of escalation steps after first attempt.
            enabled: If False, always use the cheapest tier (no escalation).
            confidence_estimator: Custom function(response_text) -> float [0,1].
                Defaults to built-in heuristic estimator.
            queue_depth_failover_threshold: When the local inference queue exceeds
                this depth, include cloud tiers in routing. 0 = disabled.
        """
        self.confidence_threshold = confidence_threshold
        self.max_escalations = max_escalations
        self.enabled = enabled
        self._estimate_confidence = confidence_estimator or _heuristic_confidence

        # Queue-depth-based cloud failover (Item 1.7)
        self._queue_depth_threshold = queue_depth_failover_threshold
        self._get_queue_depth: Callable[[], int] | None = None  # Injected via set_queue_depth_fn

        self._tiers: list[CascadeTier] = []
        self._cloud_tiers: list[CascadeTier] = []  # Cloud overflow tiers (added separately)
        self._lock = threading.RLock()

        # Stats
        self._stats: dict[str, Any] = {
            "total_requests": 0,
            "escalations": 0,
            "accepted_at_tier": {},  # tier_index -> count
            "total_cost_usd": 0.0,
            "cloud_failovers": 0,  # Times cloud tiers were included due to queue depth
        }

        logger.info(
            "CascadeRouter initialized (threshold=%.2f, max_escalations=%d, enabled=%s)",
            confidence_threshold,
            max_escalations,
            enabled,
        )

    def add_tier(
        self,
        model_id: str,
        cost_per_1k_tokens: float = 0.0,
        priority: int | None = None,
        **kwargs,
    ) -> CascadeRouter:
        """Add a model tier to the cascade chain.

        Args:
            model_id: Model identifier used to select the adapter.
            cost_per_1k_tokens: Cost per 1 000 tokens, used for cost-saving
                calculations and statistics.
            priority: Explicit priority (lower = tried first). Defaults to
                the current number of tiers so the new tier is appended at
                the end of the chain.
            **kwargs: Optional tier-level overrides forwarded to
                ``CascadeTier``. Recognised keys: ``max_tokens_override``
                (int), ``tags`` (list[str]), ``metadata`` (dict).

        Returns:
            The router instance, enabling fluent chaining (``router
            .add_tier(...).add_tier(...)``).
        """
        with self._lock:
            if priority is None:
                priority = len(self._tiers)
            tier = CascadeTier(
                model_id=model_id,
                cost_per_1k_tokens=cost_per_1k_tokens,
                priority=priority,
                **{k: v for k, v in kwargs.items() if k in ("max_tokens_override", "tags", "metadata")},
            )
            self._tiers.append(tier)
            self._tiers.sort(key=lambda t: t.priority)
            logger.debug(
                "CascadeRouter: added tier %s (priority=%d, cost=%.4f)",
                model_id,
                priority,
                cost_per_1k_tokens,
            )
        return self

    def add_cloud_tier(
        self,
        model_id: str,
        cost_per_1k_tokens: float = 0.01,
        priority: int | None = None,
        **kwargs,
    ) -> CascadeRouter:
        """Add a cloud tier used only when queue depth exceeds the failover threshold.

        Cloud tiers are kept separate from local tiers and only appended to the
        cascade chain when the local inference queue is saturated.

        Args:
            model_id: Cloud model identifier (e.g. "claude-haiku-4").
            cost_per_1k_tokens: Cost per 1000 tokens for this cloud model.
            priority: Explicit priority (defaults to 100 + len to sort after local tiers).
            **kwargs: Additional CascadeTier fields (tags, metadata).

        Returns:
            Self for fluent chaining.
        """
        with self._lock:
            if priority is None:
                priority = 100 + len(self._cloud_tiers)
            tier = CascadeTier(
                model_id=model_id,
                cost_per_1k_tokens=cost_per_1k_tokens,
                priority=priority,
                **{k: v for k, v in kwargs.items() if k in ("max_tokens_override", "tags", "metadata")},
            )
            self._cloud_tiers.append(tier)
            self._cloud_tiers.sort(key=lambda t: t.priority)
        return self

    def set_queue_depth_fn(self, fn: Callable[[], int]) -> None:
        """Inject a function that returns the current inference queue depth.

        When the returned depth exceeds ``queue_depth_failover_threshold``,
        cloud tiers are automatically included in the cascade chain.

        Args:
            fn: Zero-arg callable returning current queue depth (int).
        """
        self._get_queue_depth = fn

    def _should_include_cloud(self) -> bool:
        """Check whether cloud tiers should be included based on queue depth."""
        if self._queue_depth_threshold <= 0 or not self._cloud_tiers:
            return False
        if self._get_queue_depth is None:
            return False
        try:
            depth = self._get_queue_depth()
            if depth >= self._queue_depth_threshold:
                logger.info(
                    "Queue depth %d >= threshold %d — including cloud tiers in cascade",
                    depth,
                    self._queue_depth_threshold,
                )
                return True
        except Exception as exc:
            logger.warning("Queue depth check failed: %s", exc)
        return False

    def get_tiers(self) -> list[CascadeTier]:
        """Return ordered list of tiers (cheapest first).

        Returns:
            Snapshot of all configured cascade tiers sorted by ascending priority.
        """
        with self._lock:
            return list(self._tiers)

    def _route_tiers_snapshot(self) -> list[CascadeTier]:
        """Return the cascade tiers for a route attempt, including cloud failover."""
        with self._lock:
            tiers = list(self._tiers)
            if self._should_include_cloud():
                tiers = tiers + list(self._cloud_tiers)
                self._stats["cloud_failovers"] += 1
        return tiers

    def _attempt_cascade_route(
        self,
        request: Any,
        adapter_fn: Callable[[Any], Any],
        tiers: list[CascadeTier],
    ) -> tuple[Any, str, float, int, list[str]]:
        """Run tier attempts and return the best response plus route metadata."""
        tiers_tried: list[str] = []
        best_response = None
        best_confidence = 0.0
        best_model = tiers[0].model_id
        escalation_count = 0
        max_tiers = 1 + self.max_escalations if self.enabled else 1

        for index, tier in enumerate(tiers[:max_tiers]):
            tier_request = self._apply_tier(request, tier)
            tiers_tried.append(tier.model_id)
            try:
                response = adapter_fn(tier_request)
            except Exception as exc:
                logger.warning("CascadeRouter: tier %s failed: %s", tier.model_id, exc)
                escalation_count += 1
                continue

            status = getattr(response, "status", "ok")
            if status != INFERENCE_STATUS_OK:
                logger.debug("CascadeRouter: tier %s returned status=%s, escalating", tier.model_id, status)
                escalation_count += 1
                if best_response is None:
                    best_response = response
                    best_model = tier.model_id
                continue

            confidence = self._estimate_confidence(getattr(response, "output", "") or "")
            logger.debug(
                "CascadeRouter: tier %s confidence=%.3f (threshold=%.2f)",
                tier.model_id,
                confidence,
                self.confidence_threshold,
            )
            if confidence > best_confidence:
                best_confidence = confidence
                best_response = response
                best_model = tier.model_id
            if not self.enabled or confidence >= self.confidence_threshold:
                self._record_accepted(index)
                break
            if index < max_tiers - 1:
                self._log_escalation(tiers, index, confidence)
                escalation_count += 1
                self._record_accepted(None)
            else:
                self._record_accepted(index)
        return best_response, best_model, best_confidence, escalation_count, tiers_tried

    def _log_escalation(self, tiers: list[CascadeTier], index: int, confidence: float) -> None:
        """Log an escalation from one cascade tier to the next."""
        logger.info(
            "CascadeRouter: confidence %.3f < %.2f, escalating %s -> %s",
            confidence,
            self.confidence_threshold,
            tiers[index].model_id,
            tiers[index + 1].model_id if index + 1 < len(tiers) else "end",
        )

    @staticmethod
    def _fallback_response(tiers: list[CascadeTier], total_latency: float) -> Any:
        """Build the absolute fallback response when every tier fails."""
        from vetinari.adapters.base import InferenceResponse

        return InferenceResponse(
            model_id=tiers[0].model_id,
            output="",
            latency_ms=int(total_latency),
            tokens_used=0,
            status="error",
            error="All cascade tiers failed",
        )

    def _build_route_result(
        self,
        *,
        response: Any,
        model_id: str,
        confidence: float,
        escalation_count: int,
        tiers_tried: list[str],
        tiers: list[CascadeTier],
        total_latency: float,
    ) -> CascadeResult:
        """Record route stats and build the final CascadeResult."""
        used_tier_cost = next((tier.cost_per_1k_tokens for tier in tiers if tier.model_id == model_id), 0.0)
        largest_tier_cost = tiers[-1].cost_per_1k_tokens if tiers else 0.0
        tokens = getattr(response, "tokens_used", 0) or 0
        with self._lock:
            self._stats["total_requests"] += 1
            self._stats["escalations"] += escalation_count
            self._stats["total_cost_usd"] += used_tier_cost * tokens / 1000.0
        return CascadeResult(
            response=response,
            model_id=model_id,
            confidence=confidence,
            escalation_count=escalation_count,
            total_latency_ms=total_latency,
            tiers_tried=tiers_tried,
            cost_saved_vs_largest=max(0.0, (largest_tier_cost - used_tier_cost) * tokens / 1000.0),
        )

    def route(
        self,
        request: Any,
        adapter_fn: Callable[[Any], Any],
        context: dict[str, Any] | None = None,
    ) -> CascadeResult:
        """Route a request through the cascade chain.

        Args:
            request: InferenceRequest (or any object with .model_id and .max_tokens).
            adapter_fn: Callable(request) -> InferenceResponse. Called for each tier.
            context: Optional context dict for logging/metadata.

        Returns:
            CascadeResult with the accepted response and routing metadata.

        Raises:
            ValueError: If the operation fails.
        """
        tiers = self._route_tiers_snapshot()
        if not tiers:
            raise ConfigurationError("CascadeRouter has no tiers configured. Call add_tier() first.")

        start_total = time.monotonic()
        best_response, best_model, best_confidence, escalation_count, tiers_tried = self._attempt_cascade_route(
            request,
            adapter_fn,
            tiers,
        )
        total_latency = (time.monotonic() - start_total) * 1000

        if best_response is None:
            best_response = self._fallback_response(tiers, total_latency)

        return self._build_route_result(
            response=best_response,
            model_id=best_model,
            confidence=best_confidence,
            escalation_count=escalation_count,
            tiers_tried=tiers_tried,
            tiers=tiers,
            total_latency=total_latency,
        )

    @staticmethod
    def _apply_tier(request: Any, tier: CascadeTier) -> Any:
        """Return a copy of the request with the tier's model_id applied."""
        import copy

        new_req = copy.copy(request)
        new_req.model_id = tier.model_id
        if tier.max_tokens_override is not None:
            new_req.max_tokens = tier.max_tokens_override
        return new_req

    def _record_accepted(self, tier_index: int | None) -> None:
        if tier_index is not None:
            key = str(tier_index)
            with self._lock:
                self._stats["accepted_at_tier"][key] = self._stats["accepted_at_tier"].get(key, 0) + 1

    def get_stats(self) -> dict[str, Any]:
        """Return routing statistics.

        Returns:
            Dictionary with keys: total_requests, escalations, accepted_at_tier,
            total_cost_usd, tiers, confidence_threshold, max_escalations, enabled,
            and escalation_rate (escalations / total_requests).
        """
        with self._lock:
            stats = dict(self._stats)
            stats["tiers"] = [
                {"model_id": t.model_id, "priority": t.priority, "cost_per_1k": t.cost_per_1k_tokens}
                for t in self._tiers
            ]
            stats["confidence_threshold"] = self.confidence_threshold
            stats["max_escalations"] = self.max_escalations
            stats["enabled"] = self.enabled
            if stats["total_requests"] > 0:
                stats["escalation_rate"] = stats["escalations"] / stats["total_requests"]
            else:
                stats["escalation_rate"] = 0.0
            return stats

    def reset_stats(self) -> None:
        """Reset routing statistics."""
        with self._lock:
            self._stats = {
                "total_requests": 0,
                "escalations": 0,
                "accepted_at_tier": {},
                "total_cost_usd": 0.0,
                "cloud_failovers": 0,
            }
