"""VRAM admission, eviction, and KV-cache capacity policy.

This module keeps capacity math out of the VRAM manager shell while preserving
the public methods inherited by ``VRAMManager``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vetinari.models.vram_types import VRAMPreflightResult

logger = logging.getLogger(__name__)

# VRAM cost of KV cache per token for each quantization type.
# Values are bytes per token; divide by 1024**3 to get GB.
KV_BYTES_PER_TOKEN: dict[str, int] = {
    "f16": 2048,  # 2 bytes/element; llama.cpp default.
    "q8_0": 1024,  # 1 byte/element; half the VRAM vs f16.
    "q4_0": 512,  # 0.5 bytes/element; quarter the VRAM vs f16.
}
_KV_BYTES_PER_TOKEN = KV_BYTES_PER_TOKEN


class _VRAMCapacityMixin:
    """Capacity and eviction behavior mixed into ``VRAMManager``."""

    if TYPE_CHECKING:
        _cpu_offload_gb: Any
        _estimates: Any
        _gpu_total_gb: Any
        _leases: Any
        _lock_rw: Any
        _overhead_gb: Any
        _reap_expired_leases: Any
        get_free_vram_gb: Any

    @staticmethod
    def get_model_vram_requirement(model_id: str) -> float:
        """Return estimated VRAM requirement for a model in GB.

        Args:
            model_id: Model identifier to inspect in the registry or estimate
                from its name.

        Returns:
            Estimated model memory requirement in GB.
        """
        try:
            from vetinari.models.model_registry import get_model_registry

            # refresh=False — this is an advisory hot-path lookup and a full
            # registry refresh hashes every local GGUF file (potentially
            # multi-GB) on a synchronous request path. Fall through to the
            # size estimator when the model isn't already cached.
            info = get_model_registry().get_model_info(model_id, refresh=False)
            if info:
                return float(info.memory_requirements_gb)
        except Exception as exc:
            logger.warning(
                "Model registry lookup failed for %s; using size estimator: %s",
                model_id,
                exc,
            )
        from vetinari.utils import estimate_model_memory_gb

        return float(estimate_model_memory_gb(model_id))

    def can_load(self, model_id: str) -> bool:
        """Return True if the model fits within available VRAM and CPU offload.

        Args:
            model_id: Model identifier to evaluate.

        Returns:
            True if the model can be admitted under the current memory budget.
        """
        required = self.get_model_vram_requirement(model_id)
        free_vram = self.get_free_vram_gb()

        if required <= free_vram:
            return True

        gpu_portion = min(required, free_vram)
        cpu_portion = required - gpu_portion
        cpu_used = sum(estimate.cpu_gb for estimate in self._estimates.values())
        cpu_free = self._cpu_offload_gb - cpu_used

        if cpu_portion <= cpu_free:
            logger.debug(
                "[VRAMManager] %s needs CPU offload: %.1f GB GPU + %.1f GB RAM",
                model_id,
                gpu_portion,
                cpu_portion,
            )
            return True

        return False

    def recommend_eviction(self, model_id: str) -> str | None:
        """Return the loaded model that should be evicted to make room.

        Prioritizes lowest priority first, then least-recently-used.

        Args:
            model_id: Candidate model that needs memory.

        Returns:
            Model ID of the best eviction candidate, or None if eviction is not
            needed or no candidate can be evicted.
        """
        required = self.get_model_vram_requirement(model_id)
        free_vram = self.get_free_vram_gb()

        if required <= free_vram:
            return None

        with self._lock_rw:
            self._reap_expired_leases()
            leased_ids = {lease.model_id for lease in self._leases.values()}
            candidates = sorted(
                [
                    estimate
                    for estimate in self._estimates.values()
                    if not estimate.is_pinned and estimate.model_id not in leased_ids
                ],
                key=lambda estimate: (estimate.priority, estimate.last_used),
            )

        for candidate in candidates:
            if candidate.model_id == model_id:
                continue
            freed = candidate.total_gpu_gb
            # total_gpu_gb includes both weight VRAM and KV cache allocation, consistent with get_evictable_vram_gb().
            if free_vram + freed >= required:
                return candidate.model_id

        return candidates[0].model_id if candidates else None

    def get_evictable_vram_gb(self) -> float:
        """Return VRAM that could be freed from non-pinned, non-leased models.

        Returns:
            Total VRAM in GB that eviction could recover.
        """
        with self._lock_rw:
            self._reap_expired_leases()
            leased_ids = {lease.model_id for lease in self._leases.values()}
            return sum(
                estimate.total_gpu_gb
                for estimate in self._estimates.values()
                if not estimate.is_pinned and estimate.model_id not in leased_ids
            )

    def get_max_available_vram_gb(self) -> float:
        """Return maximum VRAM available after possible safe evictions.

        Returns:
            Free VRAM plus evictable VRAM in GB.
        """
        return self.get_free_vram_gb() + self.get_evictable_vram_gb()

    def recommend_kv_quant_for_context(
        self,
        context_length: int,
        model_vram_gb: float = 0.0,
    ) -> str:
        """Recommend a KV cache quantization type for a context length.

        Estimates the VRAM cost of the KV cache at f16 precision and downgrades
        to q8_0 or q4_0 when the budget is tight.

        Args:
            context_length: Desired context window in tokens.
            model_vram_gb: VRAM already committed to model weights in GB.

        Returns:
            One of ``"f16"``, ``"q8_0"``, or ``"q4_0"``.
        """
        with self._lock_rw:
            used_gb = sum(estimate.total_gpu_gb for estimate in self._estimates.values())
            free_gb = max(
                0.0,
                self._gpu_total_gb - self._overhead_gb - used_gb - model_vram_gb,
            )

        kv_f16_gb = context_length * _KV_BYTES_PER_TOKEN["f16"] / (1024**3)

        if kv_f16_gb > free_gb * 0.75:
            return "q4_0"
        if kv_f16_gb > free_gb * 0.50:
            return "q8_0"
        return "f16"


def check_model_vram_capacity(
    model_id: str,
    *,
    required_vram_gb: float | None,
    available_vram_gb: float | None,
) -> VRAMPreflightResult:
    """Fail closed unless the model's required VRAM is known to fit.

    Args:
        model_id: Model identifier being evaluated.
        required_vram_gb: Required GPU memory in GB, or None when unknown.
        available_vram_gb: Available GPU memory in GB, or None when unknown.

    Returns:
        VRAM admission result with explicit failure reason.
    """
    if required_vram_gb is None or required_vram_gb <= 0:
        return VRAMPreflightResult(
            model_id=model_id,
            passed=False,
            required_vram_gb=required_vram_gb,
            available_vram_gb=available_vram_gb,
            reason="VRAM requirement is unknown",
        )
    if available_vram_gb is None or available_vram_gb <= 0:
        return VRAMPreflightResult(
            model_id=model_id,
            passed=False,
            required_vram_gb=float(required_vram_gb),
            available_vram_gb=available_vram_gb,
            reason="VRAM capacity is unknown",
        )
    if required_vram_gb > available_vram_gb:
        return VRAMPreflightResult(
            model_id=model_id,
            passed=False,
            required_vram_gb=float(required_vram_gb),
            available_vram_gb=float(available_vram_gb),
            reason=(
                f"{model_id} requires {float(required_vram_gb):.1f} GB VRAM; "
                f"only {float(available_vram_gb):.1f} GB available"
            ),
        )
    return VRAMPreflightResult(
        model_id=model_id,
        passed=True,
        required_vram_gb=float(required_vram_gb),
        available_vram_gb=float(available_vram_gb),
        reason="sufficient VRAM available",
    )


VramCapacityTracker = _VRAMCapacityMixin


__all__ = ["KV_BYTES_PER_TOKEN", "VramCapacityTracker", "check_model_vram_capacity"]
