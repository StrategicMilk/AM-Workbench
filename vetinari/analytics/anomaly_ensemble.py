"""Extracted implementation helpers for anomaly.py."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.analytics.anomaly import AnomalyResult

logger = logging.getLogger(__name__)


class EnsembleAnomalyMixin:
    """Shared method implementations for the compatibility wrapper."""

    _agent_type: str
    _cusum_error_rate: Any
    _cusum_latency: Any
    _zscore_detector: Any

    def observe(
        self,
        latency: float | None = None,
        error_rate: float | None = None,
        token_usage: float | None = None,
    ) -> AnomalyResult | None:
        """Observe metrics and return anomaly result if 2+ detectors agree.

        Args:
            latency: Request latency in milliseconds.
            error_rate: Error rate as a fraction (0.0-1.0).
            token_usage: Token usage count.

        Returns:
            AnomalyResult if anomaly confirmed, None otherwise.
        """
        votes = 0
        triggered_detectors: list[str] = []

        if latency is not None and self._cusum_latency.detect(latency):
            votes += 1
            triggered_detectors.append("cusum_latency")

        if error_rate is not None and self._cusum_error_rate.detect(error_rate):
            votes += 1
            triggered_detectors.append("cusum_error_rate")

        if token_usage is not None:
            result = self._zscore_detector.detect(f"{self._agent_type}.token_usage", token_usage)
            if result.is_anomaly:
                votes += 1
                triggered_detectors.append("zscore_token_usage")

        if votes >= 2:
            from vetinari.analytics.anomaly import AnomalyResult

            logger.warning(
                "Ensemble anomaly confirmed for agent %s: %d/%d detectors triggered (%s)",
                self._agent_type,
                votes,
                3,
                ", ".join(triggered_detectors),
            )
            anomaly_result = AnomalyResult(
                metric=f"ensemble.{self._agent_type}",
                value=latency or error_rate or token_usage or 0.0,
                is_anomaly=True,
                method="ensemble",
                score=float(votes),
                reason=f"Ensemble: {votes}/3 detectors triggered: {', '.join(triggered_detectors)}",
            )
            self._on_anomaly_confirmed(anomaly_result, triggered_detectors)
            return anomaly_result

        return None

    def _on_anomaly_confirmed(self, result: AnomalyResult, triggered_detectors: list[str]) -> None:
        """Trip circuit breaker and emit event on confirmed anomaly.

        Args:
            result: The confirmed AnomalyResult.
            triggered_detectors: Names of detectors that fired.
        """
        failed_action_ids: list[str] = []

        # Trip circuit breaker
        action_id = "circuit_breaker_trip"
        try:
            from vetinari.resilience import get_circuit_breaker_registry

            registry = get_circuit_breaker_registry()
            breaker = registry.get(self._agent_type)
            breaker.trip()
            logger.warning(
                "Circuit breaker tripped for %s due to ensemble anomaly",
                self._agent_type,
            )
        except Exception:
            failed_action_ids.append(action_id)
            logger.exception("Failed to trip circuit breaker for %s", self._agent_type)

        # Emit event
        action_id = "anomaly_event_publish"
        try:
            from vetinari.events import AnomalyDetected, get_event_bus

            event = AnomalyDetected(
                event_type="",
                timestamp=time.time(),
                agent_type=self._agent_type,
                anomaly_type=result.method,
                triggered_detectors=triggered_detectors,
                score=result.score,
            )
            get_event_bus().publish(event)
        except Exception:
            failed_action_ids.append(action_id)
            logger.exception("Failed to emit AnomalyDetected event")

        if failed_action_ids:
            logger.warning(
                "Confirmed anomaly side effects degraded for %s: %s",
                self._agent_type,
                ", ".join(failed_action_ids),
            )
