"""Fail-closed model default selection backed by the setup catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from vetinari.setup.model_recommender import ModelRecommender
from vetinari.setup.model_recommender_types import Modality, SetupModelRecommendation
from vetinari.system.hardware_detect import HardwareProfile

ModelDefaultPurpose = Literal["grunt", "worker", "thinker", "modality"]


class ModelDefaultError(ValueError):
    """Raised when a trustworthy model default cannot be selected."""


@dataclass(frozen=True, slots=True)
class ModelDefaultSelection:
    """Resolved default model and the branch that produced it."""

    model_id: str
    backend: str
    quantization: str
    purpose: str
    modality: str
    reason: str
    cloud_only: bool

    def __repr__(self) -> str:
        """Return a compact representation keyed by selected model branch."""
        return (
            "ModelDefaultSelection("
            f"model_id={self.model_id!r}, backend={self.backend!r}, "
            f"purpose={self.purpose!r}, modality={self.modality!r}, "
            f"cloud_only={self.cloud_only!r})"
        )

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "model_id": self.model_id,
            "backend": self.backend,
            "quantization": self.quantization,
            "purpose": self.purpose,
            "modality": self.modality,
            "reason": self.reason,
            "cloud_only": self.cloud_only,
        }


def resolve_model_default(
    *,
    hardware: HardwareProfile,
    purpose: ModelDefaultPurpose = "worker",
    modality: Modality | str = Modality.TEXT,
    recommender: ModelRecommender | None = None,
    allow_cloud: bool = False,
) -> ModelDefaultSelection:
    """Resolve the default model from live catalog branches.

    The resolver intentionally fails closed: invalid modalities, empty catalog
    results, and cloud-only defaults without explicit allowance raise
    ``ModelDefaultError`` instead of returning a placeholder model id.

    Args:
        hardware: Hardware profile used to select the setup catalog branch.
        purpose: Default model purpose to resolve.
        modality: Modality branch to use when resolving modality defaults.
        recommender: Optional recommender instance for tests or alternate
            catalog sources.
        allow_cloud: Whether cloud-only catalog entries may be selected.

    Returns:
        Resolved model selection with model id, backend, quantization, branch,
        and provenance reason.

    Raises:
        ModelDefaultError: If the hardware profile is invalid, the modality or
            purpose is unsupported, or no trusted model can be selected.
    """
    if not isinstance(hardware, HardwareProfile):
        raise ModelDefaultError("hardware profile is required")
    try:
        modality_key = modality if isinstance(modality, Modality) else Modality(str(modality))
    except ValueError as exc:
        raise ModelDefaultError(f"unsupported modality: {modality}") from exc

    recommender = recommender or ModelRecommender()
    if purpose == "modality" or modality_key is not Modality.TEXT:
        candidates = recommender.recommend_for_modality(modality_key, hardware)
    else:
        portfolio = recommender.recommend_portfolio(hardware)
        if purpose not in portfolio:
            raise ModelDefaultError(f"unsupported default purpose: {purpose}")
        candidates = portfolio[purpose]

    selected = _first_allowed(candidates, allow_cloud=allow_cloud)
    return ModelDefaultSelection(
        model_id=selected.model_id,
        backend=selected.recommended_backend or selected.backend,
        quantization=selected.recommended_quant or selected.quantization,
        purpose=purpose,
        modality=selected.modality.value,
        reason=selected.reason,
        cloud_only=selected.cloud_only,
    )


def _first_allowed(
    candidates: list[SetupModelRecommendation],
    *,
    allow_cloud: bool,
) -> SetupModelRecommendation:
    for candidate in candidates:
        if candidate.cloud_only and not allow_cloud:
            continue
        if not candidate.model_id.strip():
            continue
        return candidate
    raise ModelDefaultError("no trusted model default available for requested branch")


__all__ = [
    "ModelDefaultError",
    "ModelDefaultPurpose",
    "ModelDefaultSelection",
    "resolve_model_default",
]
