"""External training data manager for acquiring datasets from HuggingFace Hub.

This module provides utilities to discover, download, and mix external datasets
for fine-tuning Vetinari's models. It supports SFT and DPO formats, converts
all datasets to Alpaca-style JSONL, and manages a local cache.
"""

from __future__ import annotations

import json
import logging
import os
import random
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir
from vetinari.privacy.envelope import require_privacy_envelope
from vetinari.security.redaction import redact_text
from vetinari.training.external_data_conversion import ExternalDataConversionMixin
from vetinari.utils import privacy_receipt

logger = logging.getLogger(__name__)


DEFAULT_OWN_DATA_RATIO = 0.6

DEFAULT_MAX_MIXED_TOTAL = 10000

CACHE_SUBDIR = ".vetinari/training_data"

CROSS_DOMAIN_DATASET_RATIONALE = (
    "cross_domain training mixes code, reasoning, instruction, and alignment "
    "sources only when license and immutable-revision gates pass."
)


def _has_immutable_revision(spec: DatasetSpec) -> bool:
    """Return whether a dataset spec has non-mutable source revision evidence."""
    revision = (spec.revision or "").strip()
    return bool(revision and revision != "main" and not revision.startswith("blocked:"))


def _default_training_allowed(spec: DatasetSpec) -> bool:
    """Return effective default eligibility after license and revision gates."""
    return bool(spec.default_training_allowed and _has_immutable_revision(spec))


def _safe_path_text(path: Path | str) -> str:
    """Return a log/API-safe rendering of a local filesystem path."""
    return redact_text(str(path))


def _require_training_privacy_evidence(row: dict[str, Any], *, source: str) -> None:
    """Reject training rows that lack privacy-envelope or receipt evidence."""
    if "_privacy_envelope" in row:
        require_privacy_envelope(row)
        return
    metadata = row.get("metadata")
    receipt = metadata.get("privacy_receipt") if isinstance(metadata, dict) else None
    if not isinstance(receipt, dict):
        raise ValueError(f"Training row from {source} lacks privacy receipt")
    privacy_receipt(
        privacy_class=str(receipt.get("privacy_class", "")),
        subject_id=receipt.get("subject_id"),
        retention_days=int(receipt.get("retention_days", 0)),
        source=str(receipt.get("source", "")),
        erasure_token=receipt.get("erasure_token"),
        redaction_applied=bool(receipt.get("redaction_applied", False)),
    )


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """Specification for a HuggingFace dataset to acquire.

    Args:
        name: HuggingFace dataset identifier, e.g. "mbpp".
        domain: Broad topic area, e.g. "coding", "reasoning", "instruction", "alignment".
        format: Training format — "sft" or "dpo".
        description: Human-readable summary of the dataset.
        max_examples: Maximum number of examples to retain after download.
        subset: Optional dataset subset / config name on HuggingFace Hub.
        revision: Immutable HuggingFace dataset revision. Remote dataset loads
            fail closed without this pin.
        license_ref: Primary-source license classification from the current
            license review.
        default_training_allowed: Whether default training/release flows may
            use this source without an explicit internal-only override.
    """

    name: str
    domain: str
    format: str
    description: str
    max_examples: int
    subset: str | None = None
    revision: str | None = None
    license_ref: str = "review-required:unknown"
    default_training_allowed: bool = False

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"DatasetSpec(name={self.name!r}, domain={self.domain!r}, format={self.format!r})"


@dataclass(frozen=True, slots=True)
class DatasetInfo:
    """Runtime information about a dataset, including download status.

    Args:
        name: HuggingFace dataset identifier.
        domain: Broad topic area.
        size: Number of examples available (or expected).
        estimated_train_minutes: Rough training-time estimate in minutes.
        downloaded: Whether the dataset is present in the local cache.
        path: Local path to the JSONL file if downloaded, else None.
        license_ref: Primary-source license classification used for default
            training eligibility.
        default_training_allowed: Whether default training/release flows may
            use this source without an explicit internal-only override.
    """

    name: str
    domain: str
    size: int
    estimated_train_minutes: int
    downloaded: bool = False
    path: Path | None = None
    license_ref: str = "review-required:unknown"
    default_training_allowed: bool = False

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"DatasetInfo(name={self.name!r}, domain={self.domain!r}, downloaded={self.downloaded!r})"


class ExternalDataManager(ExternalDataConversionMixin):
    """Manages acquisition and preparation of external training datasets.

    Downloads datasets from HuggingFace Hub, converts them to Alpaca-style
    JSONL, and supports mixing with locally-generated data for fine-tuning.

    Attributes:
        DATASET_CATALOG: Class-level registry of curated datasets by category.
    """

    CURATED_TRAINING_SOURCE = bool("reviewed")
    DATASET_CATALOG: dict[str, list[DatasetSpec]] = {
        "code_sft": [
            DatasetSpec(
                name="bigcode/the-stack-v2-dedup",
                domain="coding",
                format="sft",
                description="Deduplicated source-code corpus from Software Heritage (Python subset).",
                max_examples=50000,
                subset="python",
                revision="blocked:other-per-file-license-filter-required",
                license_ref="blocked:other-per-file-license-filter-required",
            ),
            DatasetSpec(
                name="deepmind/code_contests",
                domain="coding",
                format="sft",
                description="Competitive programming problems and solutions from DeepMind.",
                max_examples=10000,
                revision="802411c3010cb00d1b05bad57ca77365a3c699d6",
                license_ref="cc-by-4.0",
                default_training_allowed=CURATED_TRAINING_SOURCE,
            ),
            DatasetSpec(
                name="mbpp",
                domain="coding",
                format="sft",
                description="Mostly Basic Python Problems benchmark dataset.",
                max_examples=1000,
                revision="4bb6404fdc6cacfda99d4ac4205087b89d32030c",
                license_ref="cc-by-4.0",
                default_training_allowed=CURATED_TRAINING_SOURCE,
            ),
            DatasetSpec(
                name="codeparrot/apps",
                domain="coding",
                format="sft",
                description="APPS coding challenge dataset with test cases.",
                max_examples=10000,
                revision="21e74ddf8de1a21436da12e3e653065c5213e9d1",
                license_ref="mit",
                default_training_allowed=CURATED_TRAINING_SOURCE,
            ),
        ],
        "reasoning_sft": [
            DatasetSpec(
                name="codeparrot/apps",
                domain="reasoning",
                format="sft",
                description="Live competitive programming problems for reasoning evaluation.",
                max_examples=5000,
                revision="21e74ddf8de1a21436da12e3e653065c5213e9d1",
                license_ref="mit",
                default_training_allowed=CURATED_TRAINING_SOURCE,
            ),
            DatasetSpec(
                name="hendrycks/competition_math",
                domain="reasoning",
                format="sft",
                description="Competition-level mathematics problems with step-by-step solutions.",
                max_examples=12500,
                revision="71b758ecc688b2822d07ffa7f8393299f1dc7cac",
                license_ref="mit",
                default_training_allowed=CURATED_TRAINING_SOURCE,
            ),
            DatasetSpec(
                name="codeparrot/codecontests",
                domain="reasoning",
                format="sft",
                description="Code contest problems collected by CodeParrot.",
                max_examples=10000,
                revision="blocked:primary-source-inaccessible-2026-05-08",
                license_ref="blocked:primary-source-inaccessible-2026-05-08",
            ),
        ],
        "instruction_sft": [
            DatasetSpec(
                name="HuggingFaceTB/smoltalk",
                domain="instruction",
                format="sft",
                description="SmolTalk instruction-following dataset from HuggingFace.",
                max_examples=50000,
                revision="blocked:missing-primary-source-license-2026-05-08",
                license_ref="blocked:missing-primary-source-license-2026-05-08",
            ),
            DatasetSpec(
                name="tatsu-lab/alpaca",
                domain="instruction",
                format="sft",
                description="Stanford Alpaca instruction-following dataset.",
                max_examples=52000,
                revision="blocked:cc-by-nc-4.0-noncommercial",
                license_ref="blocked:cc-by-nc-4.0-noncommercial",
            ),
            DatasetSpec(
                name="Open-Orca/OpenOrca",
                domain="instruction",
                format="sft",
                description="OpenOrca augmented instruction dataset.",
                max_examples=50000,
                revision="e9c87b4abb2609913751f9b26553fdb9c061796c",
                license_ref="mit",
                default_training_allowed=CURATED_TRAINING_SOURCE,
            ),
        ],
        "preference_dpo": [
            DatasetSpec(
                name="Anthropic/hh-rlhf",
                domain="alignment",
                format="dpo",
                description="Anthropic human-preference data for RLHF/DPO training.",
                max_examples=50000,
                revision="blocked:mit-privacy-review-required",
                license_ref="mit:privacy-review-required",
            ),
            DatasetSpec(
                name="argilla/ultrafeedback-binarized-preferences",
                domain="alignment",
                format="dpo",
                description="UltraFeedback binarised preference pairs from Argilla.",
                max_examples=60000,
                revision="blocked:missing-primary-source-license-2026-05-08",
                license_ref="blocked:missing-primary-source-license-2026-05-08",
            ),
        ],
    }

    def __init__(
        self,
        cache_dir: Path | None = None,
        hf_token: str | None = None,
    ) -> None:
        """Initialise the manager with a cache directory and optional HF token.

        Args:
            cache_dir: Directory for storing downloaded datasets.
                Defaults to ``~/.vetinari/training_data``.
            hf_token: HuggingFace API token. Falls back to the ``HF_TOKEN``
                environment variable if not provided.
        """
        self.cache_dir: Path = cache_dir or (get_user_dir() / "training_data")
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.hf_token: str | None = hf_token or os.environ.get("HF_TOKEN")
        logger.info("ExternalDataManager initialised with cache_dir=%s", _safe_path_text(self.cache_dir))

    # ── Public API ──────────────────────────────────────────────────────────────

    def get_available_datasets(self, domain: str | None = None, *, include_blocked: bool = False) -> list[DatasetInfo]:
        """Return a prioritised list of available datasets, optionally filtered by domain.

        Checks which datasets are already present in the local cache and marks
        them accordingly. Downloaded datasets are listed first.

        Args:
            domain: If given, only return datasets whose domain matches this string.
            include_blocked: Include sources that fail the default-promotion
                license gate. Blocked sources are hidden by default.

        Returns:
            List of DatasetInfo objects sorted so downloaded datasets appear first.
        """
        infos: list[DatasetInfo] = []

        for _category, specs in self.DATASET_CATALOG.items():
            for spec in specs:
                if domain is not None and spec.domain != domain:
                    continue
                effective_training_allowed = _default_training_allowed(spec)
                if not include_blocked and not effective_training_allowed:
                    continue

                local_path = self._expected_path(spec)
                is_downloaded = local_path.exists()

                # Rough heuristic: 1 minute per 500 examples at typical GPU speed
                est_minutes = max(1, spec.max_examples // 500)

                infos.append(
                    DatasetInfo(
                        name=spec.name,
                        domain=spec.domain,
                        size=spec.max_examples,
                        estimated_train_minutes=est_minutes,
                        downloaded=is_downloaded,
                        path=local_path if is_downloaded else None,
                        license_ref=spec.license_ref,
                        default_training_allowed=effective_training_allowed,
                    ),
                )
        infos.sort(key=lambda d: (not d.downloaded, d.name))
        logger.debug(
            "get_available_datasets(domain=%s) -> %d entries",
            domain,
            len(infos),
        )
        return infos

    def download_dataset(self, spec: DatasetSpec, *, allow_blocked_license: bool = False) -> Path:
        """Download a dataset from HuggingFace Hub and convert it to Alpaca JSONL.

        Uses a late import of ``datasets.load_dataset`` so that the ``datasets``
        library remains an optional dependency.  The dataset is subsampled to
        ``spec.max_examples`` before writing.

        Args:
            spec: Specification describing which dataset to download.
            allow_blocked_license: Permit an explicitly blocked or unreviewed
                source for a recorded internal-only experiment.

        Returns:
            Path to the resulting JSONL file in the local cache.

        Raises:
            ImportError: If the ``datasets`` library is not installed.
            ValueError: If the source is blocked for default training.
            RuntimeError: If the download or conversion fails.
        """
        effective_training_allowed = _default_training_allowed(spec)
        if not effective_training_allowed and not allow_blocked_license:
            raise ValueError(
                f"Dataset {spec.name!r} is blocked for default training: {spec.license_ref}; "
                "default training requires an approved license and immutable revision provenance. "
                "Pass allow_blocked_license=True only for explicitly recorded internal-only experiments."
            )
        if not self.is_available():
            raise ImportError(
                "The 'datasets' library is required for downloading external data. "
                "Install it with: pip install datasets",
            )

        from datasets import load_dataset

        output_path = self._expected_path(spec)
        if output_path.exists():
            logger.info("Dataset already cached at %s, skipping download", _safe_path_text(output_path))
            return output_path

        logger.info("Downloading dataset %s (subset=%s)", spec.name, spec.subset)

        load_kwargs: dict[str, Any] = {"path": spec.name, "split": "train"}
        if spec.subset:
            load_kwargs["name"] = spec.subset
        revision = (spec.revision or "").strip()
        if not revision or revision == "main" or revision.startswith("blocked:"):
            raise ValueError(
                f"Dataset {spec.name!r} requires an immutable HuggingFace revision before download; "
                "update DatasetSpec.revision with a tag or commit hash."
            )
        load_kwargs["revision"] = revision
        if self.hf_token:
            load_kwargs["token"] = self.hf_token

        try:
            ds = load_dataset(**load_kwargs)
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load dataset '{spec.name}' from HuggingFace: {exc}",
            ) from exc

        # Subsample if needed
        total = len(ds)
        if total > spec.max_examples:
            indices = random.sample(range(total), spec.max_examples)
            ds = ds.select(indices)
            logger.info(
                "Subsampled %s from %d -> %d examples",
                spec.name,
                total,
                spec.max_examples,
            )

        count = self._convert_to_training_format(ds, spec, output_path)
        logger.info(
            "Dataset %s written to %s (%d records)",
            spec.name,
            _safe_path_text(output_path),
            count,
        )
        return output_path

    @staticmethod
    def _load_own_rows(own_data_path: Path | None, own_quota: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if own_data_path is None or not own_data_path.exists():
            return rows
        with own_data_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed JSON line in %s", _safe_path_text(own_data_path))
                    continue
                if isinstance(row, dict):
                    _require_training_privacy_evidence(row, source=_safe_path_text(own_data_path))
                    rows.append(row)
        if len(rows) > own_quota:
            rows = random.sample(rows, own_quota)
        logger.info("Own data: %d examples from %s", len(rows), _safe_path_text(own_data_path))
        return rows

    def _load_external_rows(self, external_specs: list[DatasetSpec], quota: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for spec in external_specs:
            if len(rows) >= quota:
                break
            try:
                path = self.download_dataset(spec)
            except Exception as exc:
                logger.warning("Could not obtain external dataset %s: %s", spec.name, redact_text(str(exc)))
                continue
            rows.extend(self._read_external_rows(path, quota - len(rows)))
        return rows

    @staticmethod
    def _read_external_rows(path: Path, limit: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if len(rows) >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed JSON line in %s", _safe_path_text(path))
                    continue
                if isinstance(row, dict):
                    _require_training_privacy_evidence(row, source=_safe_path_text(path))
                    rows.append(row)
        return rows

    def create_mixed_dataset(
        self,
        own_data_path: Path | None,
        external_specs: list[DatasetSpec],
        ratio: float = DEFAULT_OWN_DATA_RATIO,
        max_total: int = DEFAULT_MAX_MIXED_TOTAL,
    ) -> Path:
        """Create a mixed JSONL dataset from own data and external datasets.

        Blends locally-generated examples (``ratio`` fraction) with external
        data (``1 - ratio`` fraction), capped at ``max_total`` records total.

        Args:
            own_data_path: Path to an existing Alpaca-style JSONL file produced
                by Vetinari, or ``None`` if no own data is available.
            external_specs: List of DatasetSpec objects for external datasets to
                include. Each will be downloaded on demand if not cached.
            ratio: Fraction of the final dataset that should come from own data.
                Must be in ``[0, 1]``. Defaults to 0.6.
            max_total: Maximum number of records in the mixed dataset.

        Returns:
            Path to the mixed JSONL file inside the cache directory.

        Raises:
            ValueError: If ``ratio`` is outside ``[0, 1]``.
        """
        if not 0.0 <= ratio <= 1.0:
            raise ValueError(f"ratio must be between 0 and 1, got {ratio}")

        own_quota = int(max_total * ratio)
        ext_quota = max_total - own_quota

        own_rows = self._load_own_rows(own_data_path, own_quota)
        own_shortfall = own_quota - len(own_rows)
        effective_ext_quota = ext_quota + own_shortfall
        ext_rows = self._load_external_rows(external_specs, effective_ext_quota)

        all_rows = own_rows + ext_rows
        random.shuffle(all_rows)

        mixed_path = self.cache_dir / "mixed_dataset.jsonl"
        with mixed_path.open("w", encoding="utf-8") as out:
            for row in all_rows:
                out.write(json.dumps(row) + "\n")

        logger.info(
            "Mixed dataset written to %s (%d own + %d external = %d total)",
            _safe_path_text(mixed_path),
            len(own_rows),
            len(ext_rows),
            len(all_rows),
        )
        return mixed_path

    def get_stats(self) -> dict[str, Any]:
        """Return summary statistics about the local dataset cache.

        Returns:
            Dictionary with the following keys:

            - ``total_datasets``: Total number of datasets in the catalog.
            - ``downloaded_count``: Number of datasets present in the cache.
            - ``total_examples``: Sum of examples across all downloaded datasets.
            - ``cache_dir_size_bytes``: Total size of the cache directory in bytes.
            - ``cache_dir``: Absolute path to the cache directory as a string.
        """
        all_specs: list[DatasetSpec] = [spec for specs in self.DATASET_CATALOG.values() for spec in specs]

        downloaded_count = 0
        total_examples = 0

        for spec in all_specs:
            path = self._expected_path(spec)
            if path.exists():
                downloaded_count += 1
                # Count lines as a proxy for example count
                try:
                    with path.open(encoding="utf-8") as fh:
                        total_examples += sum(1 for ln in fh if ln.strip())
                except OSError as exc:
                    logger.warning("Could not count examples in %s: %s", _safe_path_text(path), redact_text(str(exc)))

        cache_size = sum(f.stat().st_size for f in self.cache_dir.rglob("*") if f.is_file())

        return {
            "total_datasets": len(all_specs),
            "downloaded_count": downloaded_count,
            "total_examples": total_examples,
            "cache_dir_size_bytes": cache_size,
            "cache_dir": _safe_path_text(self.cache_dir),
        }

    def is_available(self) -> bool:
        """Check whether the optional ``datasets`` library is discoverable.

        Returns:
            True if ``datasets`` can be resolved, False otherwise.
        """
        try:
            found = find_spec("datasets") is not None
        except Exception:
            logger.warning(
                "datasets library spec probe failed; external dataset loading unavailable",
                exc_info=True,
            )
            return False
        if not found:
            logger.warning(
                "HuggingFace datasets library not installed — external dataset loading unavailable; "
                "install with: pip install datasets"
            )
            return False
        return True

    # ── Internal helpers ────────────────────────────────────────────────────────

    def _expected_path(self, spec: DatasetSpec) -> Path:
        """Return the canonical local cache path for a given DatasetSpec.

        Args:
            spec: The dataset specification.

        Returns:
            Path object pointing to the expected JSONL file location.
        """
        # Sanitise name to a filesystem-safe slug
        safe_name = spec.name.replace("/", "__")
        suffix = f"__{spec.subset}" if spec.subset else ""
        filename = f"{safe_name}{suffix}.jsonl"
        return self.cache_dir / filename
