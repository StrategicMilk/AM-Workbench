"""In-process resident CPU-tier lifecycle.

# Side effects:
#   - Publishes CpuTierStatusChanged events on: load start, ready, degraded,
#     releasing, released, unloaded.
#   - Acquires _complete_lock (threading.Lock) on every complete() call to
#     enforce single-request constraint.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from vetinari.events import CpuTierStatusChanged, get_event_bus
from vetinari.inference.cpu_tier.states import (
    CPU_TIER_RELEASEABLE_TERMINAL_STATES,
    CPU_TIER_STATE_DEGRADED,
    CPU_TIER_STATE_LOADING,
    CPU_TIER_STATE_READY,
    CPU_TIER_STATE_RELEASED,
    CPU_TIER_STATE_RELEASING,
    CPU_TIER_STATE_SMOKE_TEST,
    CPU_TIER_STATE_UNLOADED,
    validate_cpu_tier_state,
)
from vetinari.inference.request import CpuTierConfig, RoutedInferenceRequest
from vetinari.inference.result import InferenceResult, NoCapacityError

logger = logging.getLogger(__name__)
_CPU_TIER_STATUS_SUBSCRIBED = False
_CPU_TIER_STATUS_SUBSCRIBED_BUS_ID: int | None = None


def _log_cpu_tier_status(event: CpuTierStatusChanged) -> None:
    logger.info("cpu tier status observed: %r", event)


def _ensure_cpu_tier_status_subscriber() -> None:
    global _CPU_TIER_STATUS_SUBSCRIBED, _CPU_TIER_STATUS_SUBSCRIBED_BUS_ID
    bus = get_event_bus()
    bus_id = id(bus)
    if _CPU_TIER_STATUS_SUBSCRIBED and bus_id == _CPU_TIER_STATUS_SUBSCRIBED_BUS_ID:
        return
    bus.subscribe(CpuTierStatusChanged, _log_cpu_tier_status)
    _CPU_TIER_STATUS_SUBSCRIBED = True
    _CPU_TIER_STATUS_SUBSCRIBED_BUS_ID = bus_id


class InProcessCpuTier:
    """Single-process resident model holder with explicit memory handoff."""

    def __init__(
        self,
        config: CpuTierConfig | dict,
        model: Any | None = None,
        *,
        monotonic_clock: Callable[[], float] | None = None,
        epoch_clock: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._config = config
        self._model_path = config.model_path if isinstance(config, CpuTierConfig) else config.get("model_path", "")
        self._model_id = (
            config.model_path if isinstance(config, CpuTierConfig) else config.get("synthesis_model", "cpu-tier")
        )
        self._model = model
        self._state = CPU_TIER_STATE_UNLOADED
        self._complete_lock = threading.Lock()
        self._release_requested = False
        self._last_release_succeeded = False
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._epoch_clock = epoch_clock or time.time
        self._sleep = sleep_fn or time.sleep

    @property
    def state(self) -> str:
        """Current lifecycle state."""
        return validate_cpu_tier_state(self._state)

    def load(self) -> None:
        """Load or reload the resident model and run a smoke test."""
        self._state = CPU_TIER_STATE_LOADING
        self._publish_status()
        if self._model is None:
            self._model = _EchoModel(self._model_id)
        self._state = CPU_TIER_STATE_SMOKE_TEST
        self._publish_status()
        try:
            passed = self.smoke_test()
        except Exception:
            logger.warning("CPU tier smoke test raised; entering degraded state", exc_info=True)
            passed = False
        self._state = CPU_TIER_STATE_READY if passed else CPU_TIER_STATE_DEGRADED
        self._release_requested = False
        self._last_release_succeeded = False
        self._publish_status()

    def smoke_test(self) -> bool:
        """Run the post-load smoke test."""
        return self._post_load_smoke_test()

    def _post_load_smoke_test(self) -> bool:
        """Run a short local inference probe.

        Wall-clock time may be elevated immediately after training runs
        (Decision 12 item 1). Do not raise on first-call latency outlier.
        """
        if self._model is None:
            return False
        if hasattr(self._model, "smoke_test"):
            return bool(self._model.smoke_test())
        return True

    def complete(self, request: RoutedInferenceRequest) -> InferenceResult:
        """Run a single inference request with a non-reentrant execution guard.

                State checks are performed INSIDE the lock to close the TOCTOU window
                a release request could open between the entry check and lock acquire
                (Wave 9 audit finding #6, fixed 2026-04-27).

        Returns:
            InferenceResult value produced by complete().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        acquired = self._complete_lock.acquire(blocking=False)
        if not acquired:
            raise RuntimeError("InProcessCpuTier does not support concurrent complete() calls")
        try:
            current_state = validate_cpu_tier_state(self._state)
            if self._release_requested or current_state == CPU_TIER_STATE_RELEASING:
                raise NoCapacityError("CPU tier is releasing", capability=request.capability)
            if current_state != CPU_TIER_STATE_READY:
                raise NoCapacityError(f"CPU tier is not ready: {current_state}", capability=request.capability)
            text = self._run_model(request)
            return InferenceResult(
                text=text,
                tokens_out=max(1, len(text.split())),
                model_id=self._model_id,
                compute_tier="cpu_background",
                quality_floor=request.quality_floor,
                is_fallback=False,
            )
        finally:
            self._complete_lock.release()
            if self._state == CPU_TIER_STATE_RELEASING and self._release_requested and not self._last_release_succeeded:
                self._state = CPU_TIER_STATE_READY
                self._release_requested = False
                self._publish_status()

    def request_release(self, reason: str, timeout_s: float) -> bool:
        """Drain in-flight work and mark the model released when possible.

        Args:
            reason: Reason value consumed by request_release().
            timeout_s: Timeout value controlling how long the operation may wait.

        Returns:
            bool value produced by request_release().
        """
        logger.info("CPU tier release requested: %s", reason)
        current_state = validate_cpu_tier_state(self._state)
        if current_state in CPU_TIER_RELEASEABLE_TERMINAL_STATES:
            self._state = CPU_TIER_STATE_RELEASED
            self._model = None
            self._last_release_succeeded = True
            self._publish_status()
            return True
        self._release_requested = True
        self._state = CPU_TIER_STATE_RELEASING
        self._publish_status()
        deadline = self._monotonic_clock() + max(0.0, timeout_s)
        while self._monotonic_clock() <= deadline:
            acquired = self._complete_lock.acquire(blocking=False)
            if acquired:
                try:
                    self._model = None
                    self._state = CPU_TIER_STATE_RELEASED
                    self._last_release_succeeded = True
                    self._publish_status()
                    return True
                finally:
                    self._complete_lock.release()
            self._sleep(min(0.01, max(0.0, deadline - self._monotonic_clock())))
        self._last_release_succeeded = False
        return False

    def release_finished(self) -> None:
        """Finalize memory handoff and return to an unloaded state when applicable."""
        if not self._last_release_succeeded:
            logger.debug("release_finished ignored because last release did not succeed")
            return
        if validate_cpu_tier_state(self._state) == CPU_TIER_STATE_RELEASED:
            self._state = CPU_TIER_STATE_UNLOADED
            self._release_requested = False
            self._last_release_succeeded = False
            self._publish_status()

    def _run_model(self, request: RoutedInferenceRequest) -> str:
        if self._model is None:
            raise NoCapacityError("CPU tier has no loaded model", capability=request.capability)
        if hasattr(self._model, "complete"):
            return str(self._model.complete(request))
        return request.prompt

    def _publish_status(self) -> None:
        _ensure_cpu_tier_status_subscriber()
        get_event_bus().publish(
            CpuTierStatusChanged(
                event_type="CpuTierStatusChanged",
                timestamp=self._epoch_clock(),
                compute_id="cpu_background",
                state=validate_cpu_tier_state(self._state),
                queue_depth=0,
            )
        )


class _EchoModel:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def complete(self, request: RoutedInferenceRequest) -> str:
        return request.prompt


__all__ = ["InProcessCpuTier"]
