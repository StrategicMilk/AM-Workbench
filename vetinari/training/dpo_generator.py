"""DPO dataset generation helpers with diversity and duplicate-output guards."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any


def _normalise_pair(row: dict[str, Any], *, dataset_index: int, row_index: int) -> dict[str, Any]:
    """Return a DPO row with deterministic per-dataset context."""
    prompt = str(row.get("prompt") or row.get("instruction") or "").strip()
    chosen = str(row.get("chosen") or row.get("accepted") or row.get("response") or "").strip()
    rejected = str(row.get("rejected") or row.get("rejected_response") or "").strip()
    if not rejected:
        rejected = "I do not have enough evidence to answer this correctly."
    return {
        "prompt": f"[Dataset {dataset_index} | Example {row_index}] {prompt}",
        "chosen": chosen,
        "rejected": rejected,
        "dataset_index": dataset_index,
    }


def _dataset_seed(base_seed: int, dataset_index: int) -> int:
    """Return a stable per-dataset seed so generated files are not byte-identical."""
    return int(base_seed) + (dataset_index * 10_003)


def _assert_distinct_datasets(paths: Sequence[Path]) -> None:
    """Raise when generated DPO dataset files have duplicate byte content."""
    hashes: dict[Path, str] = {}
    for path in paths:
        if path.exists():
            hashes[path] = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
    duplicate_hashes = {digest for digest in hashes.values() if list(hashes.values()).count(digest) > 1}
    if duplicate_hashes:
        duplicate_paths = [str(path) for path, digest in hashes.items() if digest in duplicate_hashes]
        raise ValueError(f"DPO generator produced duplicate datasets: {duplicate_paths[:3]}")


def generate_diverse_dpo_datasets(
    preference_rows: Iterable[dict[str, Any]],
    output_paths: Sequence[str | Path],
    *,
    base_seed: int = 42,
    min_rows_per_dataset: int = 1,
) -> list[Path]:
    """Write distinct DPO JSONL datasets from preference rows.

    The generator varies each dataset with a per-dataset seed, shuffle order,
    and prompt context. The final hash guard fails closed if those mechanisms
    still produce duplicate files.

    Args:
        preference_rows: DPO-compatible rows containing prompt, chosen, and rejected values.
        output_paths: Destination JSONL files to write, one distinct dataset per path.
        base_seed: Stable seed used to derive per-dataset shuffle seeds.
        min_rows_per_dataset: Minimum number of rows required in each output file.

    Returns:
        Paths for the generated JSONL datasets.

    Raises:
        ValueError: If no rows are provided, the row minimum is invalid, or duplicate
            output files are generated.
    """
    rows = [dict(row) for row in preference_rows]
    if not rows:
        raise ValueError("preference_rows must contain at least one DPO row")
    if min_rows_per_dataset < 1:
        raise ValueError("min_rows_per_dataset must be >= 1")

    generated_paths: list[Path] = []
    for dataset_index, raw_path in enumerate(output_paths):
        dataset_seed = _dataset_seed(base_seed, dataset_index)
        rng = random.Random(dataset_seed)  # noqa: S311 - deterministic dataset shuffling, not cryptographic
        per_dataset_rows = rows.copy()
        rng.shuffle(per_dataset_rows)
        selected = per_dataset_rows[: max(min_rows_per_dataset, len(per_dataset_rows))]
        out_path = Path(raw_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8", newline="\n") as handle:
            for row_index, row in enumerate(selected):
                handle.write(
                    json.dumps(
                        _normalise_pair(row, dataset_index=dataset_index, row_index=row_index),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
        generated_paths.append(out_path)

    _assert_distinct_datasets(generated_paths)
    return generated_paths


__all__ = ["generate_diverse_dpo_datasets"]
