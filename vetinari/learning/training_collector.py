"""TrainingDataCollector — thread-safe training data recorder with background I/O.

Records every agent execution to a JSONL file in a fire-and-forget manner via
a background writer thread, imposing zero latency on the inference hot path.

Export methods (SFT, DPO, prompt-variant, HuggingFace, few-shot, ranking) and
trace storage methods live in ``training_exports._TrainingExportSupport``.
JSONL storage, retention, deletion, and stats methods live in helper mixins to
keep this file under the size ceiling.

The module-level ``get_training_collector()`` returns the process-wide singleton
protected by a double-checked lock.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

from vetinari.boundary_guards import account_evidence_drop
from vetinari.constants import (
    _PROJECT_ROOT,
    QUEUE_TIMEOUT,
    THREAD_JOIN_TIMEOUT,
    TRAINING_COLLECTOR_QUEUE_SIZE,
)
from vetinari.errors import FailClosedError
from vetinari.types import EvidenceBasis

from .training_collector_storage import _TrainingCollectorStorageSupport
from .training_collector_validation import (
    _build_training_record,
    _record_training_rejection,
    _TrainingRecordInput,
)
from .training_exports import _TrainingExportSupport
from .training_record import TrainingRecord

logger = logging.getLogger(__name__)


BOUNDARY_ADR = "ADR-0132"
CANONICAL_BOUNDARY = "evaluation.training_records"
_DEFAULT_PATH = os.environ.get(
    "VETINARI_TRAINING_DATA_PATH",
    str(_PROJECT_ROOT / "training_data.jsonl"),
)
TRAINING_DATA_MAX_RECORDS = int(os.environ.get("VETINARI_TRAINING_DATA_MAX_RECORDS", "50000"))
TRAINING_DATA_PURGE_INTERVAL_RECORDS = int(os.environ.get("VETINARI_TRAINING_PURGE_INTERVAL_RECORDS", "1000"))

# Known fallback response patterns — records matching these are rejected to


class TrainingDataCollector(_TrainingCollectorStorageSupport, _TrainingExportSupport):
    """Thread-safe training data recorder with background I/O.

    Records agent executions to a JSONL file via a background writer thread,
    keeping the inference hot-path non-blocking.  Enforces data quality gates
    at record time (rejects fallback/mock outputs, zero-latency records, and
    secrets-containing prompts).

    Use ``get_training_collector()`` (module-level) rather than instantiating
    directly.

    Side effects:
      - On construction (non-sync mode): starts a background daemon thread
        named ``TrainingDataWriter`` that writes queued records to disk.
    """

    _instance: TrainingDataCollector | None = None
    _cls_lock = threading.Lock()

    def __init__(self, output_path: str = _DEFAULT_PATH, sync: bool = False) -> None:
        self._output_path = Path(output_path)
        self._queue: queue.Queue[TrainingRecord | None] = queue.Queue(maxsize=TRAINING_COLLECTOR_QUEUE_SIZE)
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._sync = sync  # When True, writes happen inline (no background thread)
        self._record_count = 0
        self._worker: threading.Thread | None = None

        if not sync:
            self._worker = threading.Thread(
                target=self._write_worker,
                name="TrainingDataWriter",
                daemon=True,
            )
            self._worker.start()

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls, output_path: str = _DEFAULT_PATH) -> TrainingDataCollector:
        """Return the class-level singleton, creating it if needed.

        Starts the background writer thread on first creation. Subsequent
        calls with a different output_path are ignored — the first caller
        sets the path.

        Args:
            output_path: Path to the JSONL file where records are written.

        Returns:
            The shared TrainingDataCollector instance for this process.
        """
        with cls._cls_lock:
            if cls._instance is None:
                cls._instance = cls(output_path=output_path)
        return cls._instance

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        task: str,
        prompt: str,
        response: str,
        score: float,
        model_id: str,
        task_type: str = "general",
        prompt_variant_id: str = "",
        agent_type: str = "",
        latency_ms: int = 0,
        tokens_used: int = 0,
        success: bool = True,
        metadata: dict[str, Any] | None = None,
        benchmark_suite: str = "",
        benchmark_pass: bool = False,
        benchmark_score: float = 0.0,
        rejection_reason: str = "",
        rejection_category: str = "",
        inspector_feedback: str = "",
        trace_id: str = "",
        evidence_basis: EvidenceBasis | None = None,
        target_stream: str | None = None,
    ) -> None:
        """Validate and queue a task execution as training data.

        Fallback/mock records, zero-token/zero-latency records, blocked
        licenses, secrets, and LLM-judgment data aimed at tool-evidence streams
        are rejected before persistence. Typed provenance arguments override
        legacy metadata keys.

        Args:
            task: Human-readable task description.
            prompt: Prompt text sent to the model.
            response: Raw model response text.
            score: Quality score in the range 0.0-1.0.
            model_id: Actual model that produced the response.
            task_type: Broad task category.
            prompt_variant_id: Prompt variant used for A/B comparison.
            agent_type: Agent identifier for the execution.
            latency_ms: Wall-clock inference latency; zero is rejected.
            tokens_used: Prompt plus completion tokens; zero is rejected.
            success: Whether the task completed without an error.
            metadata: Optional dynamic context such as temperature or strategy.
            benchmark_suite: Benchmark suite that validated this output.
            benchmark_pass: Whether the output passed benchmark validation.
            benchmark_score: Benchmark score in the range 0.0-1.0.
            rejection_reason: Inspector rejection reason, when rejected.
            rejection_category: Inspector failure category label.
            inspector_feedback: Inspector feedback summary text.
            trace_id: Pipeline trace identifier for end-to-end observability.
            evidence_basis: Typed basis used by basis-aware filtering.
            target_stream: Target training stream name.

        Raises:
            FailClosedError: If the queue is full or synchronous persistence
                cannot store an accepted record.
        """
        rec = _build_training_record(
            _TrainingRecordInput(
                task,
                prompt,
                response,
                score,
                model_id,
                task_type,
                prompt_variant_id,
                agent_type,
                latency_ms,
                tokens_used,
                success,
                metadata,
                benchmark_suite,
                benchmark_pass,
                benchmark_score,
                rejection_reason,
                rejection_category,
                inspector_feedback,
                trace_id,
                evidence_basis,
                target_stream,
            ),
            record_rejection_fn=_record_training_rejection,
        )
        if rec is None:
            return
        if self._sync:
            # Synchronous mode: write directly (used in tests)
            self._append(rec)
            return

        try:
            self._queue.put_nowait(rec)
        except queue.Full as exc:
            account_evidence_drop(rec, "training_collector", logger=logger)
            raise FailClosedError(
                "training_collector.queue",
                "training record queue is full; record was not accepted",
                recovery="flush the collector or increase TRAINING_COLLECTOR_QUEUE_SIZE",
            ) from exc

    # ------------------------------------------------------------------
    # Background writer
    # ------------------------------------------------------------------

    def _write_worker(self) -> None:
        """Background thread: drain the record queue and write to JSONL."""
        while not self._shutdown.is_set():
            got_record = False
            try:
                rec = self._queue.get(timeout=QUEUE_TIMEOUT)
                got_record = True
                if rec is None:
                    # Sentinel placed by shutdown() — exit immediately.
                    return
                self._append(rec)
            except queue.Empty:
                logger.warning("Training data queue poll timed out — no pending records, will retry")
                continue  # Normal timeout — no pending records
            except Exception as e:
                logger.exception("[TrainingDataCollector] Write error")
                self._shutdown.set()
                raise FailClosedError(
                    "training_collector.writer",
                    "background writer could not persist a training record",
                    recovery="inspect the output path and restart the collector",
                ) from e
            finally:
                if got_record:
                    with contextlib.suppress(ValueError):
                        self._queue.task_done()

    def _append(self, rec: TrainingRecord) -> None:
        """Append a record to the JSONL file (thread-safe).

        Args:
            rec: The TrainingRecord to persist.
        """
        with self._lock:
            try:
                self._output_path.parent.mkdir(parents=True, exist_ok=True)
                with Path(self._output_path).open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec.to_dict(), ensure_ascii=False) + "\n")
                self._record_count += 1
                self._trim_to_recent_records_locked(TRAINING_DATA_MAX_RECORDS)
                if (
                    TRAINING_DATA_PURGE_INTERVAL_RECORDS > 0
                    and self._record_count % TRAINING_DATA_PURGE_INTERVAL_RECORDS == 0
                ):
                    self._purge_expired_records_locked(cutoff_days=30)
            except Exception as e:
                logger.exception("[TrainingDataCollector] Append failed")
                raise FailClosedError(
                    "training_collector.append",
                    "training record could not be persisted",
                    recovery=f"verify write access to {self._output_path}",
                ) from e

    def flush(self, timeout: float = THREAD_JOIN_TIMEOUT) -> None:
        """Wait for the queue to drain, with timeout to avoid deadlocks.

        Args:
            timeout: Maximum seconds to wait for the queue to drain.
        """
        if self._sync:
            return

        deadline = time.monotonic() + max(timeout, 0.0)
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "[TrainingDataCollector] Flush timed out with %d unfinished record(s)",
                        self._queue.unfinished_tasks,
                    )
                    return
                self._queue.all_tasks_done.wait(timeout=min(0.1, remaining))

    def shutdown(self, timeout: float = THREAD_JOIN_TIMEOUT) -> None:
        """Flush remaining records and stop the background worker.

        Signals the writer thread via the shutdown event and a sentinel queue
        entry so it unblocks immediately, then waits up to ``timeout`` seconds
        for it to exit.

        Args:
            timeout: Maximum seconds to wait for the worker thread to finish.
        """
        self.flush()
        self._shutdown.set()
        # Sentinel wakes the worker if it is blocked in queue.get().
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        if self._worker is not None:
            if self._worker.ident is None and not self._worker.is_alive():
                self._worker = None
                return
            self._worker.join(timeout=timeout)
            if not self._worker.is_alive():
                self._worker = None

    def close(self, timeout: float = THREAD_JOIN_TIMEOUT) -> None:
        """Alias for shutdown() for resource-style lifecycle callers."""
        self.shutdown(timeout=timeout)

    def count_reasoning_episodes(self) -> int:
        """Count records with reasoning-related task types.

        Counts records where ``task_type`` is one of planning, reasoning,
        decomposition, or analysis — used by the curriculum to decide when
        self-play reasoning training is worthwhile.

        Returns:
            Number of reasoning-related records.

        Raises:
            FailClosedError: If the training data file cannot be read.
        """
        reasoning_types = {"planning", "reasoning", "decomposition", "analysis"}
        count = 0
        try:
            with self._lock:
                if self._output_path.exists():
                    with open(self._output_path, encoding="utf-8") as fh:
                        for line in fh:
                            with contextlib.suppress(json.JSONDecodeError):
                                rec = json.loads(line)
                                if rec.get("task_type", "") in reasoning_types:
                                    count += 1
        except OSError as exc:
            raise FailClosedError(
                "training_collector.reasoning_count",
                "could not read training data for reasoning episode count",
                recovery=f"verify read access to {self._output_path}",
            ) from exc
        return count

    def count_execution_traces(self) -> int:
        """Count records with code-execution task types.

        Counts records where ``task_type`` is one of coding, implementation,
        bug_fix, or refactoring — used by the curriculum to decide when RLEF
        (Reinforcement Learning from Execution Feedback) training is worthwhile.

        Returns:
            Number of code-execution-related records.

        Raises:
            FailClosedError: If the training data file cannot be read.
        """
        execution_types = {"coding", "implementation", "bug_fix", "refactoring"}
        count = 0
        try:
            with self._lock:
                if self._output_path.exists():
                    with open(self._output_path, encoding="utf-8") as fh:
                        for line in fh:
                            with contextlib.suppress(json.JSONDecodeError):
                                rec = json.loads(line)
                                if rec.get("task_type", "") in execution_types:
                                    count += 1
        except OSError as exc:
            raise FailClosedError(
                "training_collector.execution_count",
                "could not read training data for execution trace count",
                recovery=f"verify read access to {self._output_path}",
            ) from exc
        return count


# ---------------------------------------------------------------------------
# Module-level accessor (double-checked locking)
# ---------------------------------------------------------------------------

# Module-level singleton state.
# Written by: get_training_collector() on first call.
# Read by: all callers that need the shared instance.
# Lock: _collector_lock guards the check-then-create sequence.
_collector: TrainingDataCollector | None = None
_collector_lock = threading.Lock()


def get_training_collector(
    output_path: str = _DEFAULT_PATH,
) -> TrainingDataCollector:
    """Return the global TrainingDataCollector singleton.

    Thread-safe: uses double-checked locking to ensure a single instance and
    background writer thread are created even under concurrent startup.

    Args:
        output_path: Path to the JSONL file where records are written.
            Only the first call's value takes effect.

    Returns:
        The shared TrainingDataCollector instance for this process.
    """
    global _collector
    if _collector is None:
        with _collector_lock:
            if _collector is None:
                _collector = TrainingDataCollector.get_instance(output_path)
    elif output_path != _DEFAULT_PATH and str(_collector._output_path) != output_path:
        logger.warning(
            "get_training_collector() called with output_path=%r but singleton already "
            "created with output_path=%r — ignoring new path; data will continue to be "
            "written to the existing path",
            output_path,
            str(_collector._output_path),
        )
    return _collector
