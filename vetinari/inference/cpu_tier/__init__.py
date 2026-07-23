"""CPU-tier interface and factory."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from vetinari.inference.request import CpuTierConfig, RoutedInferenceRequest
from vetinari.inference.result import InferenceResult


@runtime_checkable
class CpuTierInterface(Protocol):
    """Protocol implemented by resident CPU inference tiers."""

    def load(self) -> None:
        """Load model weights and enter a callable state."""

    def smoke_test(self) -> bool:
        """Run a short post-load health check."""

    def complete(self, request: RoutedInferenceRequest) -> InferenceResult:
        """Run one synchronous inference request."""

    def request_release(self, reason: str, timeout_s: float) -> bool:
        """Drain in-flight work and release resident model memory.

        Args:
            reason: Reason value consumed by request_release().
            timeout_s: Timeout value controlling how long the operation may wait.
        """

    def release_finished(self) -> None:
        """Signal that the caller has finished RAM-intensive work."""


def make_cpu_tier(config: CpuTierConfig | dict) -> CpuTierInterface:
    """Create a CPU tier for the configured process mode.

    Returns:
        Newly constructed cpu tier value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    from vetinari.inference.cpu_tier.lifecycle import InProcessCpuTier

    process_mode = (
        config.process_mode if isinstance(config, CpuTierConfig) else config.get("process_mode", "in_process")
    )
    if process_mode == "in_process":
        return InProcessCpuTier(config)
    raise ValueError(f"unknown CPU tier process_mode: {process_mode}")


__all__ = ["CpuTierInterface", "make_cpu_tier"]
