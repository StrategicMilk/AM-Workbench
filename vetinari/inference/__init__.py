"""Inference subsystem — continuous batching."""

from __future__ import annotations

from vetinari.inference.batcher import (
    BatchConfig,
    BatchRequest,
    InferenceBatcher,
    get_inference_batcher,
)
from vetinari.inference.request import RoutedInferenceRequest
from vetinari.inference.result import InferenceResult, NoCapacityError
from vetinari.inference.router import ComputeTarget, select_target

__all__ = [
    "BatchConfig",
    "BatchRequest",
    "ComputeTarget",
    "InferenceBatcher",
    "InferenceResult",
    "NoCapacityError",
    "RoutedInferenceRequest",
    "get_inference_batcher",
    "select_target",
]
