"""Explicit training-algorithm to loss-function dispatch."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from vetinari.exceptions import ConfigurationError
from vetinari.types import TrainingAlgorithm

# SimPO paper arXiv:2405.14734 and the Princeton SimPO repo recommend beta=2.0
# as a baseline and gamma/beta ratio 0.5 as the starting margin. TRL exposes
# that margin as DPOConfig.simpo_gamma, whose current default is also 0.5.
SIMPO_BETA_DEFAULT = 2.0
SIMPO_GAMMA_DEFAULT = 0.5

LossCallable = Callable[..., Any]

_LOSS_FUNCTION_NAMES: dict[TrainingAlgorithm, str] = {
    TrainingAlgorithm.DPO: "dpo_loss",
    TrainingAlgorithm.SIMPO: "simpo_loss",
}


def _coerce_algorithm(algorithm: TrainingAlgorithm | str) -> TrainingAlgorithm:
    """Normalize a training algorithm identifier to the canonical enum."""
    if isinstance(algorithm, TrainingAlgorithm):
        return algorithm
    try:
        return TrainingAlgorithm(str(algorithm).lower())
    except ValueError as exc:
        raise ConfigurationError(f"Unsupported training algorithm: {algorithm!r}") from exc


def dpo_loss(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """DPO loss sentinel for dispatch-level tests and lightweight callers."""
    return {"algorithm": TrainingAlgorithm.DPO.value, "args": args, "kwargs": kwargs}


def simpo_loss(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """SimPO loss sentinel for dispatch-level tests and lightweight callers."""
    return {"algorithm": TrainingAlgorithm.SIMPO.value, "args": args, "kwargs": kwargs}


def get_loss_function(algorithm: TrainingAlgorithm | str) -> LossCallable:
    """Return the configured loss function for ``algorithm`` or fail closed.

    Args:
        algorithm: Training algorithm enum or string identifier.

    Returns:
        Callable loss function for the requested algorithm.

    Raises:
        ConfigurationError: If the algorithm is unsupported or its loss function
            is not available.
    """
    training_algorithm = _coerce_algorithm(algorithm)
    function_name = _LOSS_FUNCTION_NAMES.get(training_algorithm)
    loss_function = globals().get(function_name or "")
    if not callable(loss_function):
        raise ConfigurationError(
            f"Training algorithm '{training_algorithm.value}' requires loss function "
            f"'{function_name}', but it is unavailable."
        )
    return loss_function


def run_training_loss(algorithm: TrainingAlgorithm | str, *args: Any, **kwargs: Any) -> Any:
    """Execute the loss function selected by the explicit algorithm mapping."""
    return get_loss_function(algorithm)(*args, **kwargs)
