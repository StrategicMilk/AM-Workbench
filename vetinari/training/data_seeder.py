"""Training data seeding - bootstrap from day one."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir
from vetinari.inference.result import InferenceResult
from vetinari.security.redaction import redact_text
from vetinari.training.external_data import ExternalDataManager
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


class TrainingSeedAuthorizationError(PermissionError):
    """Raised when a caller reaches seed download side effects without authorization."""


def safe_for_training_corpus(result: InferenceResult) -> bool:
    """Pre-check whether a tier output is eligible for the training corpus."""
    return result.safe_for_training_corpus()


def _training_data_dir() -> Path:
    """Return the configured training-data cache root lazily."""
    return get_user_dir() / "training_data"


def _seed_marker() -> Path:
    """Return the seed-completion marker under the configured cache root."""
    return _training_data_dir() / ".seeded"


def _redact_seed_text(value: object) -> str:
    """Redact local paths and secrets before seed state reaches logs or UI events."""
    return redact_text(str(value))


@dataclass(frozen=True, slots=True)
class SeedDataset:
    """Specification for a seed dataset to bootstrap training."""

    name: str
    domain: str
    size: int
    description: str
    subsample: bool = False
    revision: str | None = None
    license_ref: str = "review-required:unknown"
    default_training_allowed: bool = False

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"SeedDataset(name={self.name!r}, domain={self.domain!r}, size={self.size!r})"


class TrainingDataSeeder:
    """Seeds the local training data store with curated external datasets."""

    CURATED_TRAINING_SOURCE = bool("reviewed")
    SEED_DATASETS: list[SeedDataset] = [
        SeedDataset(
            "codeparrot/apps",
            "coding_eval",
            5000,
            "Coding problems from competitive programming for evaluation",
            revision="21e74ddf8de1a21436da12e3e653065c5213e9d1",
            license_ref="mit",
            default_training_allowed=CURATED_TRAINING_SOURCE,
        ),
        SeedDataset(
            "mbpp",
            "coding",
            1000,
            "Basic Python problems - foundational Python coding coverage",
            revision="4bb6404fdc6cacfda99d4ac4205087b89d32030c",
            license_ref="cc-by-4.0",
            default_training_allowed=CURATED_TRAINING_SOURCE,
        ),
        SeedDataset(
            "hendrycks/competition_math",
            "reasoning",
            5000,
            "Competition math - chain-of-thought reasoning from structured problem sets",
            subsample=True,
            revision="71b758ecc688b2822d07ffa7f8393299f1dc7cac",
            license_ref="mit",
            default_training_allowed=CURATED_TRAINING_SOURCE,
        ),
        SeedDataset(
            "Open-Orca/OpenOrca",
            "instruction",
            10000,
            "Instruction following - broad generalist instruction-tuning data from an approved source",
            subsample=True,
            revision="e9c87b4abb2609913751f9b26553fdb9c061796c",
            license_ref="mit",
            default_training_allowed=CURATED_TRAINING_SOURCE,
        ),
    ]

    def __init__(self) -> None:
        """Initialise the seeder with lazy-loaded dependencies."""
        self._manager: Any = None

    @staticmethod
    def _require_seed_authorization(*, authorized: bool) -> None:
        """Block seed side effects unless the caller passed an explicit authorization decision."""
        if not authorized:
            raise TrainingSeedAuthorizationError(
                "Training seed downloads require explicit local authorization before installing packages or "
                "fetching external datasets."
            )

    def _ensure_dataset_manager(self, *, include_training_libs: bool = False) -> Any | None:
        """Install optional packages if needed and return ExternalDataManager."""
        manager = self._get_manager()
        if manager is not None and (not hasattr(manager, "is_available") or manager.is_available()):
            return manager
        from vetinari.training.pipeline import _ensure_packages

        install_results = _ensure_packages(["datasets"])
        logger.info("seed_if_empty: auto-install results: %s", install_results)
        if include_training_libs:
            _ensure_packages(["trl", "peft", "bitsandbytes", "transformers"])
        if manager is None or (hasattr(manager, "is_available") and not manager.is_available()):
            self._manager = None
            manager = self._get_manager()
        if manager is None:
            return None
        if hasattr(manager, "is_available") and not manager.is_available():
            return None
        return manager

    @staticmethod
    def _download_seed_dataset(manager: Any, seed: SeedDataset, *, allow_unrevised: bool = False) -> str:
        """Download one seed dataset and return its local path."""
        from vetinari.training.external_data import DatasetSpec

        if seed.revision is None and not allow_unrevised:
            raise ValueError(
                f"SeedDataset {seed.name!r} has no revision pin; set revision= or pass allow_unrevised=True"
            )
        spec = DatasetSpec(
            name=seed.name,
            domain=seed.domain,
            format="sft",
            description=seed.description,
            max_examples=seed.size,
            revision=seed.revision,
            license_ref=seed.license_ref,
            default_training_allowed=seed.default_training_allowed,
        )
        return str(manager.download_dataset(spec))

    @staticmethod
    def _write_seed_marker(seeded_count: int) -> None:
        """Write the seed marker when at least one dataset was downloaded."""
        if seeded_count <= 0:
            return
        try:
            _seed_marker().write_text(f"seeded={seeded_count} datasets\n", encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not write seed marker: %s", _redact_seed_text(exc))

    def seed_if_empty(self, *, authorized: bool = False, allow_unrevised: bool = False) -> int:
        """Download seed datasets if no training data exists yet.

        Args:
            authorized: Explicit caller authorization for package installation
                and external dataset downloads.
            allow_unrevised: Permit seed datasets without explicit revision pins.

        Returns:
            Value produced for the caller.

        Raises:
            ValueError: If a seed dataset lacks a revision pin and unrevised
                seeds were not explicitly allowed.
        """
        if self._training_data_exists():
            logger.info("Training data already exists - skipping seed download")
            return 0
        self._require_seed_authorization(authorized=authorized)
        if not allow_unrevised:
            for seed in self.SEED_DATASETS:
                if seed.revision is None:
                    raise ValueError(
                        f"SeedDataset {seed.name!r} has no revision pin; set revision= or pass allow_unrevised=True"
                    )

        logger.info("No training data found - seeding %d dataset(s)", len(self.SEED_DATASETS))
        manager = self._ensure_dataset_manager()
        if manager is None:
            logger.warning("ExternalDataManager unavailable even after install attempt; cannot seed training data")
            return 0

        _training_data_dir().mkdir(parents=True, exist_ok=True)
        seeded_count = 0
        for seed in self.SEED_DATASETS:
            logger.info("Seeding dataset '%s' (domain=%s, size=%d) ...", seed.name, seed.domain, seed.size)
            try:
                path = self._download_seed_dataset(manager, seed, allow_unrevised=allow_unrevised)
                logger.info("Seeded '%s' -> %s", seed.name, _redact_seed_text(path))
                seeded_count += 1
            except Exception as exc:
                logger.warning("Failed to seed dataset '%s': %s - continuing", seed.name, _redact_seed_text(exc))

        self._write_seed_marker(seeded_count)
        logger.info("Seed phase complete: %d/%d dataset(s) downloaded", seeded_count, len(self.SEED_DATASETS))
        return seeded_count

    def _initial_progress_events(self, total: int) -> Iterator[dict[str, Any]]:
        """Yield install/start events before progress downloads begin."""
        yield {"event": "installing", "message": "Checking and installing required packages..."}
        from vetinari.training.pipeline import _ensure_packages

        install_results = _ensure_packages(["datasets"])
        yield {"event": "installing", "message": "Installed datasets library", "results": install_results}
        training_results = _ensure_packages(["trl", "peft", "bitsandbytes", "transformers"])
        yield {"event": "installing", "message": "Installed training libraries", "results": training_results}
        yield {"event": "start", "total": total, "datasets": [s.name for s in self.SEED_DATASETS]}

    @staticmethod
    def _count_examples(path: str) -> int:
        """Count non-empty JSONL records in a downloaded seed file."""
        try:
            with Path(path).open(encoding="utf-8") as fh:
                return sum(1 for ln in fh if ln.strip())
        except OSError:
            logger.warning("Could not read seed file: %s", _redact_seed_text(path))
            return 0

    @staticmethod
    def _progress_eta(elapsed_times: list[float], remaining: int) -> float | None:
        """Estimate remaining seconds from completed seed timings."""
        if not elapsed_times:
            return None
        return round(sum(elapsed_times) / len(elapsed_times) * remaining, 1)

    def _download_with_progress(
        self,
        manager: Any,
        seed: SeedDataset,
        idx: int,
        total: int,
        elapsed_times: list[float],
        *,
        allow_unrevised: bool = False,
    ) -> tuple[dict[str, Any], int, bool]:
        """Download one seed and return the completion event, example count, and success."""
        t0 = time.monotonic()
        try:
            path = self._download_seed_dataset(manager, seed, allow_unrevised=allow_unrevised)
            elapsed = time.monotonic() - t0
            elapsed_times.append(elapsed)
            examples = self._count_examples(path)
            logger.info("Seeded '%s' -> %s (%d examples)", seed.name, _redact_seed_text(path), examples)
            return (
                {
                    "event": "progress",
                    "dataset": seed.name,
                    "index": idx + 1,
                    "total": total,
                    "percent": round((idx + 1) / total * 100),
                    "status": StatusEnum.COMPLETED.value,
                    "examples": examples,
                    "elapsed_seconds": round(elapsed, 1),
                    "eta_seconds": self._progress_eta(elapsed_times, total - idx - 1) if idx + 1 < total else 0,
                },
                examples,
                True,
            )
        except Exception as exc:
            elapsed_times.append(time.monotonic() - t0)
            logger.warning("Failed to seed '%s': %s", seed.name, _redact_seed_text(exc))
            return (
                {
                    "event": "progress",
                    "dataset": seed.name,
                    "index": idx + 1,
                    "total": total,
                    "percent": round((idx + 1) / total * 100),
                    "status": StatusEnum.FAILED.value,
                    "error": _redact_seed_text(exc),
                },
                0,
                False,
            )

    def seed_with_progress(
        self, *, authorized: bool = False, allow_unrevised: bool = False
    ) -> Iterator[dict[str, Any]]:
        """Download seed datasets, yielding progress events as a generator.

        Raises:
            ValueError: If revision policy rejects a configured seed dataset.
        """
        datasets = self.SEED_DATASETS
        total = len(datasets)
        if self._training_data_exists():
            yield {
                "event": "done",
                "seeded": 0,
                StatusEnum.FAILED.value: 0,
                "total_examples": 0,
                "message": "Training data already exists",
            }
            return

        self._require_seed_authorization(authorized=authorized)
        if not allow_unrevised:
            for seed in datasets:
                if seed.revision is None:
                    raise ValueError(
                        f"SeedDataset {seed.name!r} has no revision pin; set revision= or pass allow_unrevised=True"
                    )
        _training_data_dir().mkdir(parents=True, exist_ok=True)
        yield from self._initial_progress_events(total)
        self._manager = None
        manager = self._get_manager()
        if manager is None:
            yield {"event": "error", "error": "ExternalDataManager unavailable even after install attempt"}
            return
        if hasattr(manager, "is_available") and not manager.is_available():
            yield {"event": "error", "error": "The 'datasets' library is not available even after install attempt"}
            return

        seeded = failed = total_examples = 0
        elapsed_times: list[float] = []
        for idx, seed in enumerate(datasets):
            yield {
                "event": "progress",
                "dataset": seed.name,
                "index": idx + 1,
                "total": total,
                "percent": round(idx / total * 100),
                "status": "downloading",
                "eta_seconds": self._progress_eta(elapsed_times, total - idx),
            }
            event, examples, success = self._download_with_progress(
                manager, seed, idx, total, elapsed_times, allow_unrevised=allow_unrevised
            )
            yield event
            if success:
                seeded += 1
                total_examples += examples
            else:
                failed += 1

        self._write_seed_marker(seeded)
        yield {"event": "done", "seeded": seeded, StatusEnum.FAILED.value: failed, "total_examples": total_examples}

    def get_seed_status(self) -> dict[str, Any]:
        """Return the current seed status for each configured dataset.

        Returns:
            Value produced for the caller.
        """
        manager = self._get_manager()
        downloaded: list[str] = []
        pending: list[str] = []
        total_examples = 0

        for seed in self.SEED_DATASETS:
            safe_name = seed.name.replace("/", "__")
            expected_path = _training_data_dir() / f"{safe_name}.jsonl"
            if expected_path.exists() and expected_path.stat().st_size > 0:
                downloaded.append(seed.name)
                try:
                    with expected_path.open(encoding="utf-8") as fh:
                        total_examples += sum(1 for ln in fh if ln.strip())
                except OSError as exc:
                    logger.warning(
                        "Could not count examples in %s: %s",
                        _redact_seed_text(expected_path),
                        _redact_seed_text(exc),
                    )
            else:
                pending.append(seed.name)

        if manager:
            try:
                available = manager.get_available_datasets()
                seed_names = {s.name for s in self.SEED_DATASETS}
                extra = [d.name for d in available if d.downloaded and d.name in seed_names]
                downloaded = list(set(downloaded) | set(extra))
                pending = [s.name for s in self.SEED_DATASETS if s.name not in set(downloaded)]
            except Exception as exc:
                logger.warning("Could not query manager for seed status: %s", _redact_seed_text(exc))

        return {
            "total_seed_datasets": len(self.SEED_DATASETS),
            "downloaded": sorted(downloaded),
            StatusEnum.PENDING.value: sorted(pending),
            "total_examples": total_examples,
            "data_dir": _redact_seed_text(_training_data_dir()),
        }

    @staticmethod
    def _training_data_exists() -> bool:
        """Check whether any training data files already exist."""
        training_data_dir = _training_data_dir()
        if _seed_marker().exists():
            return True
        if not training_data_dir.exists():
            return False
        return any(f.stat().st_size > 0 for f in training_data_dir.glob("*.jsonl"))

    def _get_manager(self) -> Any:
        """Return a lazily initialised ExternalDataManager, or None."""
        if self._manager is not None:
            return self._manager
        self._manager = ExternalDataManager(cache_dir=_training_data_dir())
        return self._manager


_seeder_instance: TrainingDataSeeder | None = None
_seeder_instance_lock: threading.Lock = threading.Lock()


def get_training_data_seeder() -> TrainingDataSeeder:
    """Return the canonical TrainingDataSeeder singleton.

    Returns:
        Value produced for the caller.
    """
    global _seeder_instance
    if _seeder_instance is not None:
        return _seeder_instance
    with _seeder_instance_lock:
        if _seeder_instance is not None:
            return _seeder_instance
        _seeder_instance = TrainingDataSeeder()
    logger.debug("get_training_data_seeder: created new singleton")
    return _seeder_instance
