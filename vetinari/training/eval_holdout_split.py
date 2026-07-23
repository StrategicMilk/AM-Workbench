"""Deterministic training/eval holdout split helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass


class HoldoutSplitError(ValueError):
    """Raised when train/eval separation cannot be proven."""


@dataclass(frozen=True, slots=True)
class HoldoutSplit:
    """Deterministic split of record ids."""

    train_ids: tuple[str, ...]
    holdout_ids: tuple[str, ...]
    seed: str


def build_holdout_split(
    record_ids: Iterable[str],
    *,
    holdout_fraction: float = 0.2,
    seed: str = "default",
) -> HoldoutSplit:
    """Split records deterministically and fail closed on unusable inputs.

    Returns:
        Deterministic train and holdout ids.

    Raises:
        HoldoutSplitError: if ids are duplicated or too few.
        ValueError: if the holdout fraction is outside ``(0, 1)``.
    """
    ids = tuple(str(record_id) for record_id in record_ids)
    if len(set(ids)) != len(ids):
        raise HoldoutSplitError("record ids must be unique before splitting")
    if len(ids) < 2:
        raise HoldoutSplitError("at least two records are required for a holdout split")
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError("holdout_fraction must be between 0 and 1")
    ordered = sorted(ids, key=lambda value: _stable_hash(f"{seed}:{value}"))
    holdout_count = max(1, round(len(ordered) * holdout_fraction))
    holdout = tuple(sorted(ordered[:holdout_count]))
    train = tuple(sorted(ordered[holdout_count:]))
    assert_no_train_eval_reuse(train, holdout)
    return HoldoutSplit(train_ids=train, holdout_ids=holdout, seed=seed)


def assert_no_train_eval_reuse(training_ids: Iterable[str], eval_ids: Iterable[str]) -> None:
    """Raise if any record appears in both training and eval sets.

    Args:
        training_ids: Training record ids.
        eval_ids: Holdout/eval record ids.

    Raises:
        HoldoutSplitError: if any id appears in both sets.
    """
    overlap = set(training_ids) & set(eval_ids)
    if overlap:
        raise HoldoutSplitError(f"training/eval reuse detected: {sorted(overlap)}")


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = ["HoldoutSplit", "HoldoutSplitError", "assert_no_train_eval_reuse", "build_holdout_split"]
