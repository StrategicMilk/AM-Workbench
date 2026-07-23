"""Continual learning protection facade for Vetinari training.

This module preserves the public import path for the continual-learning
protections used by the training pipeline while delegating each responsibility
to a focused helper module:

- ``STABLERegularizer`` handles forgetting metrics and LoRA layer gates.
- ``ReplayBuffer`` owns replay-example storage and mixed dataset creation.
- ``LoRAAdapterManager`` owns task-specific LoRA adapter registry state.
"""

from __future__ import annotations

import logging

from vetinari.training.continual_learning_adapters import LoRAAdapterManager
from vetinari.training.continual_learning_replay import ReplayBuffer
from vetinari.training.continual_learning_stable import STABLERegularizer

logger = logging.getLogger(__name__)


BOUNDARY_ADR = "ADR-0132"
CANONICAL_BOUNDARY = "training.continual_adaptation"


__all__ = [
    "BOUNDARY_ADR",
    "CANONICAL_BOUNDARY",
    "LoRAAdapterManager",
    "ReplayBuffer",
    "STABLERegularizer",
]
