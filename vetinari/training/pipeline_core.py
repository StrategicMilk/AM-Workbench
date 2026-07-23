"""Training pipeline core helpers.

Contains the support classes used by the full training pipeline:
BenchmarkTracker, CloudOutputStore, TrainingRun, DataCurator,
and the ``_ensure_packages`` utility.  These are separated from the
heavier trainer and converter classes to keep each module focused.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import TRUNCATE_OUTPUT_PREVIEW, TRUNCATE_OUTPUT_SUMMARY, get_user_dir
from vetinari.exceptions import ExecutionError
from vetinari.privacy.envelope import PrivacyClass, privacy_receipt, wrap_for_persistence
from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)

_BENCHMARK_TRACKER_LOCK_TIMEOUT_SECONDS = 5.0
_BENCHMARK_TRACKER_LOCK_POLL_SECONDS = 0.02


_PINNED_PACKAGE_SPECS: dict[str, str] = {
    "bitsandbytes": ">=0.43.0,<1.0",
    "datasets": ">=2.18.0,<4.0",
    "peft": ">=0.10.0,<1.0",
    "transformers": ">=4.40.0,<5.0",
    "trl": ">=0.8.0,<1.0",
}


_AUTO_INSTALL_PACKAGE_ALLOWLIST: frozenset[str] = frozenset({
    "bitsandbytes",
    "datasets",
    "peft",
    "transformers",
})


class _BenchmarkTrackerFileLock:
    """Cross-process lock for benchmark tracker state reads and writes."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: Any | None = None

    def __enter__(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a+b")
        self._fh.seek(0, os.SEEK_END)
        if self._fh.tell() == 0:
            self._fh.write(b"\0")
            self._fh.flush()
        self._fh.seek(0)
        if os.name == "nt":
            msvcrt = __import__("msvcrt")
            deadline = time.monotonic() + _BENCHMARK_TRACKER_LOCK_TIMEOUT_SECONDS
            while True:
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        self._fh.close()
                        self._fh = None
                        raise TimeoutError(f"benchmark tracker lock timed out: {self._path}") from exc
                    time.sleep(_BENCHMARK_TRACKER_LOCK_POLL_SECONDS)
        else:
            fcntl = __import__("fcntl")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            if os.name == "nt":
                msvcrt = __import__("msvcrt")
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl = __import__("fcntl")
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            self._fh.close()
            self._fh = None


class BenchmarkTracker:
    """Track benchmark run history to detect staleness.

    Stores the timestamp of the last benchmark run in a simple JSON file
    so the curriculum can decide when to re-run benchmarks.
    """

    def _state_path(self) -> Path:
        """Return the path to the benchmark tracker state file.

        Uses get_user_dir() to store state in a consistent, writable location
        across different working directory contexts.

        Returns:
            Path to benchmark_tracker.json under the user directory.
        """
        return get_user_dir() / "benchmark_tracker.json"

    def days_since_last_run(self) -> int:
        """Return the number of days since the last benchmark run.

        Returns:
            Days since last run, or 999 if no run has been recorded.
        """
        try:
            last_run = self.last_run()
            if not last_run:
                raise ValueError("benchmark tracker has no last_run")
            last = datetime.fromisoformat(last_run)
            return (datetime.now(timezone.utc) - last).days
        except Exception:
            logger.warning(
                "Could not read training pipeline state file %s — treating last run as very stale (999 days), retraining may trigger",
                self._state_path(),
            )
            return 999  # No record — treat as very stale

    def last_run(self) -> str | None:
        """Return the recorded benchmark timestamp, if present and readable.

        Returns:
            ISO timestamp string for the last run, or ``None`` when absent/unreadable.
        """
        state_path = self._state_path()
        if not state_path.exists():
            return None
        try:
            with (
                _BenchmarkTrackerFileLock(state_path.with_suffix(state_path.suffix + ".lock")),
                state_path.open(encoding="utf-8") as f,
            ):
                data = json.load(f)
            value = data.get("last_run")
            return str(value) if value else None
        except Exception:
            logger.warning("Could not read benchmark tracker state file %s", state_path, exc_info=True)
            return None

    def record_run(self) -> None:
        """Record that a benchmark run happened now."""
        state_path = self._state_path()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"last_run": datetime.now(timezone.utc).isoformat()}
        with _BenchmarkTrackerFileLock(state_path.with_suffix(state_path.suffix + ".lock")):
            tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f)
                f.write("\n")
            tmp_path.replace(state_path)


class CloudOutputStore:
    """Store high-quality cloud model outputs for knowledge distillation.

    Collects outputs from cloud API calls (e.g., Claude, GPT-4) that score
    above a quality threshold, so they can be used to train local models.

    The store file is resolved through ``get_user_dir()`` so the path is
    stable regardless of the process working directory (defect 4 fix).
    """

    # Filename within the user dir — resolved lazily via _store_path().
    _STORE_FILENAME = "cloud_outputs.jsonl"

    def _store_path(self) -> Path:
        """Return the absolute path to the cloud output store file.

        Returns:
            Path inside the canonical Vetinari user directory.
        """
        return get_user_dir() / self._STORE_FILENAME

    def get_high_quality_outputs(self, min_score: float = 0.8) -> list[dict]:
        """Return stored cloud outputs above the quality threshold.

        Args:
            min_score: Minimum quality score to include.

        Returns:
            List of output dicts with prompt, response, score, and model fields.
        """
        outputs = []
        store_path = self._store_path()
        if not store_path.exists():
            return []
        with store_path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if isinstance(entry, dict) and "payload" in entry and "_privacy_envelope" in entry:
                        entry = entry["payload"]
                    if entry.get("score", 0) >= min_score:
                        outputs.append(entry)
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping malformed JSON line in cloud output store %s — entry ignored",
                        store_path,
                    )
                    continue
        return outputs

    def record(self, prompt: str, response: str, score: float, model: str) -> None:
        """Store a cloud model output for potential distillation.

        Args:
            prompt: The input prompt.
            response: The model's output.
            score: Quality score (0.0-1.0).
            model: The model that produced this output.
        """
        store_path = self._store_path()
        store_path.parent.mkdir(parents=True, exist_ok=True)
        safe_prompt = redact_text(prompt)
        safe_response = redact_text(response)
        entry = {
            "prompt": safe_prompt,
            "response": safe_response,
            "score": score,
            "model": model,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        wrapped = wrap_for_persistence(
            entry,
            privacy_class=PrivacyClass.SUBJECT_DATA,
            subject_id="cloud-output-distillation",
            source="training.cloud_outputs",
            redaction_applied=safe_prompt != prompt or safe_response != response,
        )
        with store_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(wrapped) + "\n")


@dataclass(frozen=True, slots=True)
class TrainingRun:
    """Result of a training pipeline run."""

    run_id: str
    timestamp: str
    base_model: str
    task_type: str
    training_examples: int
    epochs: int
    success: bool
    gradient_accumulation_steps: int = 1
    warmup_ratio: float = 0.0
    output_model_path: str = ""
    adapter_path: str = ""
    backend: str = "llama_cpp"
    model_format: str = "gguf"
    model_revision: str = ""
    model_manifest_path: str = ""
    eval_score: float = 0.0
    baseline_score: float = 0.0
    eval_status: str = "not_started"
    eval_reason: str = ""
    eval_evidence_path: str = ""
    eval_holdout_examples: int = 0
    error: str = ""

    def __repr__(self) -> str:
        return (
            f"TrainingRun(run_id={self.run_id!r}, base_model={self.base_model!r}, "
            f"task_type={self.task_type!r}, success={self.success!r}, "
            f"eval_score={self.eval_score!r})"
        )


def _set_training_run_field(run: TrainingRun, field: str, value: object) -> None:
    """Set a field while the pipeline is still assembling the frozen run record."""
    object.__setattr__(run, field, value)


class DataCurator:
    """Curates high-quality training data from the TrainingDataCollector."""

    def curate(
        self,
        task_type: str | None = None,
        min_score: float = 0.8,
        max_examples: int = 5000,
        output_dir: str = ".",
    ) -> str:
        """Curate SFT training data and write to a JSONL file.

        Returns the path to the curated dataset file.

        Args:
            task_type: The task type.
            min_score: The min score.
            max_examples: The max examples.
            output_dir: The output dir.

        Returns:
            Absolute path to the written JSONL file, named
            ``sft_<task_type>_<YYYYMMDD_HHMM>.jsonl``.

        Raises:
            ExecutionError: If no training data meets criteria.
        """
        from vetinari.learning.training_data import get_training_collector

        collector = get_training_collector()
        data = collector.export_sft_dataset(
            min_score=min_score,
            task_type=task_type,
            max_records=max_examples,
        )

        if not data:
            raise ExecutionError(f"No training data meets criteria (score>={min_score}, type={task_type})")

        # Format for Alpaca-style fine-tuning
        formatted = [
            {
                "instruction": d["prompt"][:TRUNCATE_OUTPUT_SUMMARY],
                "input": "",
                "output": d["completion"][:TRUNCATE_OUTPUT_PREVIEW],
                "metadata": {
                    "source": "training_data_collector",
                    "task_type": task_type or "general",
                    "privacy_receipt": privacy_receipt(
                        privacy_class=PrivacyClass.SUBJECT_DATA.value,
                        subject_id=task_type or "general",
                        source="training.data_curator.sft",
                        redaction_applied=True,
                    ),
                },
            }
            for d in data
        ]

        out_path = (
            Path(output_dir)
            / f"sft_{task_type or 'general'}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.jsonl"
        )
        with Path(out_path).open("w", encoding="utf-8") as f:
            f.writelines(json.dumps(item) + "\n" for item in formatted)

        logger.info("[DataCurator] Wrote %d examples to %s", len(formatted), out_path)
        return str(out_path)

    def curate_dpo(
        self,
        task_type: str | None = None,
        min_score_gap: float = 0.2,
        output_dir: str = ".",
    ) -> str:
        """Curate DPO preference pairs and write to a JSONL file.

        Args:
            task_type: The task type.
            min_score_gap: The min score gap.
            output_dir: The output dir.

        Returns:
            Absolute path to the written JSONL file, named
            ``dpo_<task_type>_<YYYYMMDD_HHMM>.jsonl``.

        Raises:
            ExecutionError: If no preference pairs are available.
        """
        from vetinari.learning.training_data import get_training_collector

        collector = get_training_collector()
        pairs = collector.export_dpo_dataset(
            task_type=task_type,
            min_score_gap=min_score_gap,
        )

        if not pairs:
            raise ExecutionError("No preference pairs available for DPO training")

        formatted_pairs = []
        for pair in pairs:
            item = dict(pair)
            metadata = dict(item.get("metadata") or {})
            metadata["privacy_receipt"] = privacy_receipt(
                privacy_class=PrivacyClass.SUBJECT_DATA.value,
                subject_id=task_type or "general",
                source="training.data_curator.dpo",
                redaction_applied=True,
            )
            metadata["task_type"] = task_type or "general"
            item["metadata"] = metadata
            formatted_pairs.append(item)

        out_path = (
            Path(output_dir)
            / f"dpo_{task_type or 'general'}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')}.jsonl"
        )
        with Path(out_path).open("w", encoding="utf-8") as f:
            f.writelines(json.dumps(pair) + "\n" for pair in formatted_pairs)

        logger.info("[DataCurator] Wrote %d DPO pairs to %s", len(formatted_pairs), out_path)
        return str(out_path)


def _ensure_packages(packages: list[str]) -> dict[str, bool]:
    """Auto-install missing Python packages via pip subprocess.

    Checks each package with importlib and installs missing ones via pip.

    Args:
        packages: List of package names to ensure are installed.

    Returns:
        Dict mapping package name to True if installed (already or newly),
        False if installation failed.
    """
    import importlib.util

    results: dict[str, bool] = {}
    for pkg in packages:
        if pkg not in _AUTO_INSTALL_PACKAGE_ALLOWLIST:
            logger.warning("_ensure_packages: refusing unapproved package %s", pkg)
            results[pkg] = False
            continue
        if importlib.util.find_spec(pkg) is not None:
            results[pkg] = True
            continue

        logger.info("_ensure_packages: %s not found, attempting pip install", pkg)
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", f"{pkg}{_PINNED_PACKAGE_SPECS.get(pkg, '')}"],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if proc.returncode == 0:
                logger.info("_ensure_packages: successfully installed %s", pkg)
                results[pkg] = True
            else:
                logger.warning(
                    "_ensure_packages: pip install %s failed (rc=%d): %s",
                    pkg,
                    proc.returncode,
                    proc.stderr[-500:] if proc.stderr else "",
                )
                results[pkg] = False
        except subprocess.TimeoutExpired:
            logger.warning("_ensure_packages: pip install %s timed out", pkg)
            results[pkg] = False
        except Exception as exc:
            logger.warning("_ensure_packages: failed to install %s: %s", pkg, exc)
            results[pkg] = False

    return results
