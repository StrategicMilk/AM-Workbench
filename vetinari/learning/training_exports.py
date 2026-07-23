"""Training data export and trace methods for TrainingDataCollector.

Extracted from training_collector.py to stay under the 550-line ceiling.
All methods here operate on self._output_path, self._lock, self.flush(),
and self._load_all(); they assume those attributes exist on the concrete
class that inherits this mixin.

This is step 3 of the training data pipeline:
Intake -> Record -> **Export/Trace** -> Adapter -> Model update.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from vetinari.boundary_guards import require_nonempty
from vetinari.privacy_rights import subject_is_opted_out

logger = logging.getLogger(__name__)

_SAFE_TRACE_ID_RE = re.compile(r"[A-Za-z0-9_-]+")


if TYPE_CHECKING:
    from vetinari.learning.training_record import TrainingRecord


class _TrainingExportOwner(Protocol):
    _output_path: Path
    flush: Callable[[], None]
    _load_all: Callable[[], list[TrainingRecord]]


def _training_export_owner(value: object) -> _TrainingExportOwner:
    return cast(_TrainingExportOwner, value)


def _redact_trace_value(value: Any) -> Any:
    """Return a JSON-like trace value with PII removed from string leaves."""
    from vetinari.safety.guardrails import redact_pii_payload

    return redact_pii_payload(value)


def _dpo_completion(value: Any) -> Any:
    """Return a JSON-compatible DPO completion without Python repr wrappers."""
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    if stripped[0] not in "{[":
        return value
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(stripped)
        except (ValueError, SyntaxError) as exc:
            logger.warning(
                "Structured-looking DPO completion could not be parsed; keeping raw string",
                exc_info=exc,
            )
            return value
    if isinstance(parsed, dict | list):
        return parsed
    return value


def _training_task_group_key(record: TrainingRecord) -> str:
    """Return a non-truncated, task-type-aware key for preference grouping."""
    return json.dumps(
        {
            "task_type": str(getattr(record, "task_type", "")),
            "task": str(getattr(record, "task", "")).strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _safe_trace_dir(base_dir: Path, task_id: str) -> Path:
    """Return a trace directory confined under ``base_dir`` for any task id."""
    raw = require_nonempty(str(task_id), field_name="trace_id")
    if not _SAFE_TRACE_ID_RE.fullmatch(raw):
        raise ValueError(f"trace_id contains unsafe characters: {task_id!r}")
    slug = raw
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    safe_name = f"{slug[:80] or 'task'}-{digest}"
    resolved_base = base_dir.resolve()
    trace_dir = (resolved_base / safe_name).resolve()
    if trace_dir != resolved_base and resolved_base not in trace_dir.parents:
        raise ValueError(f"trace task_id resolves outside trace store: {task_id!r}")
    return trace_dir


def _read_trace_dir(trace_dir: Path, task_id: str) -> dict[str, Any]:
    """Read a resolved trace directory."""
    result: dict[str, Any] = {
        "prompt": (trace_dir / "prompt.txt").read_text(encoding="utf-8"),
        "output": (trace_dir / "output.txt").read_text(encoding="utf-8"),
    }
    task_id_path = trace_dir / "task_id.txt"
    if task_id_path.exists():
        result["task_id"] = task_id_path.read_text(encoding="utf-8")
    verdict_path = trace_dir / "inspector_verdict.json"
    if verdict_path.exists():
        try:
            result["inspector_verdict"] = json.loads(verdict_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Corrupt inspector_verdict.json for task %s - returning empty dict", task_id)
            result["inspector_verdict"] = {}
    errors_path = trace_dir / "errors.log"
    if errors_path.exists():
        result["errors"] = errors_path.read_text(encoding="utf-8").splitlines()
    return result


def _record_subject(record: TrainingRecord) -> str | None:
    metadata = getattr(record, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        return None
    for key in ("subject", "subject_id", "privacy_subject_id", "user_id"):
        value = metadata.get(key)
        if value:
            return str(value)
    receipt = metadata.get("privacy_receipt") or metadata.get("_privacy_envelope")
    if isinstance(receipt, dict) and receipt.get("subject_id"):
        return str(receipt["subject_id"])
    return None


def _record_allowed_for_training_export(record: TrainingRecord) -> bool:
    metadata = getattr(record, "metadata", {}) or {}
    if isinstance(metadata, dict):
        consent = (
            str(
                metadata.get("training_consent")
                or metadata.get("consent_basis")
                or metadata.get("data_use_consent")
                or "local-only"
            )
            .strip()
            .lower()
        )
        if consent in {"", "none", "denied", "revoked", "opt_out", "opted_out", "no_training"}:
            return False
    subject = _record_subject(record)
    return not (subject and subject_is_opted_out(subject))


class _TrainingExportSupport:
    """Export and trace methods for TrainingDataCollector.

    Inheritors MUST provide:
    - self._output_path (Path)
    - self._lock (threading.Lock)
    - self.flush() -> None
    - self._load_all() -> list[TrainingRecord]
    """

    # ------------------------------------------------------------------
    # Dataset exports
    # ------------------------------------------------------------------

    def export_sft_dataset(
        self,
        min_score: float = 0.8,
        task_type: str | None = None,
        max_records: int = 10000,
    ) -> list[dict[str, Any]]:
        """Export high-quality completions for supervised fine-tuning.

        Args:
            min_score: Minimum quality score to include.
            task_type: Optional task type filter; None includes all types.
            max_records: Maximum number of records to return.

        Returns:
            List of ``{"prompt": ..., "completion": ..., "score": ...,
            "task_type": ...}`` dicts, sorted best-score-first.
        """
        owner = _training_export_owner(self)
        owner.flush()
        all_records = owner._load_all()
        filtered = [
            r
            for r in all_records
            if _record_allowed_for_training_export(r)
            and r.score >= min_score
            and r.success
            and (task_type is None or r.task_type == task_type)
        ]
        filtered.sort(key=lambda r: -r.score)
        return [r.to_sft_pair() for r in filtered[:max_records]]

    def export_dpo_dataset(
        self,
        task_type: str | None = None,
        min_score_gap: float = 0.2,
    ) -> list[dict[str, Any]]:
        """Export preference pairs for DPO alignment training.

        Groups records by task text; pairs highest-scoring with lowest-scoring
        responses for the same task (if gap >= min_score_gap).

        Args:
            task_type: Optional task type filter; None includes all types.
            min_score_gap: Minimum score gap between chosen and rejected.

        Returns:
            List of ``{"prompt": ..., "chosen": ..., "rejected": ...,
            "chosen_score": ..., "rejected_score": ..., "task_type": ...}``
            dicts, one per task with at least two responses meeting the gap.
        """
        owner = _training_export_owner(self)
        owner.flush()
        all_records = owner._load_all()
        all_records = [r for r in all_records if _record_allowed_for_training_export(r)]
        if task_type:
            all_records = [r for r in all_records if r.task_type == task_type]

        by_task: dict[str, list[TrainingRecord]] = defaultdict(list)
        for r in all_records:
            key = _training_task_group_key(r)
            by_task[key].append(r)

        pairs: list[dict[str, Any]] = []
        for _task_key, group in by_task.items():
            if len(group) < 2:
                continue
            group.sort(key=lambda r: r.score)
            worst = group[0]
            best = group[-1]
            if (best.score - worst.score) >= min_score_gap:
                pair: dict[str, Any] = {
                    "prompt": best.task,
                    "chosen": _dpo_completion(best.response),
                    "rejected": _dpo_completion(worst.response),
                    "chosen_score": best.score,
                    "rejected_score": worst.score,
                    "task_type": best.task_type,
                }
                # Carry rejection reason into DPO pair so the model learns
                # WHY the chosen response is better, not just that it is.
                if getattr(worst, "rejection_reason", ""):
                    pair["why_chosen_is_better"] = worst.rejection_reason
                if getattr(worst, "rejection_category", ""):
                    pair["rejection_category"] = worst.rejection_category
                pairs.append(pair)
        return pairs

    def export_prompt_dataset(self) -> list[dict[str, Any]]:
        """Export prompt variant performance for DSPy/A-B analysis.

        Returns:
            List of dicts with keys: prompt_variant_id, task_type, score,
            latency_ms, tokens_used, model_id - one entry per record that
            has a non-empty prompt_variant_id.
        """
        owner = _training_export_owner(self)
        owner.flush()
        all_records = owner._load_all()
        variant_records = [r for r in all_records if _record_allowed_for_training_export(r) and r.prompt_variant_id]
        return [
            {
                "prompt_variant_id": r.prompt_variant_id,
                "task_type": r.task_type,
                "score": r.score,
                "latency_ms": r.latency_ms,
                "tokens_used": r.tokens_used,
                "model_id": r.model_id,
            }
            for r in variant_records
        ]

    def export_hf_dataset(
        self,
        min_score: float = 0.8,
        task_type: str | None = None,
    ) -> list[dict[str, str]]:
        """Export records in HuggingFace Datasets / Alpaca format.

        Does NOT require the ``datasets`` library - returns plain Python dicts.

        Args:
            min_score: Minimum quality score to include.
            task_type: Optional task type filter; None includes all types.

        Returns:
            List of ``{"instruction": ..., "input": ..., "output": ...}``
            dicts sorted best-score-first.
        """
        owner = _training_export_owner(self)
        owner.flush()
        all_records = owner._load_all()
        filtered = [
            r
            for r in all_records
            if _record_allowed_for_training_export(r)
            and r.score >= min_score
            and r.success
            and (task_type is None or r.task_type == task_type)
        ]
        filtered.sort(key=lambda r: -r.score)
        return [
            {
                "instruction": r.task,
                "input": r.prompt,
                "output": r.response,
            }
            for r in filtered
        ]

    def export_few_shot_examples(
        self,
        task_type: str,
        k: int = 5,
    ) -> list[dict[str, str]]:
        """Return top-k highest-scoring examples for a specific task type.

        Args:
            task_type: Task type to filter by.
            k: Maximum number of examples to return.

        Returns:
            List of ``{"input": ..., "output": ...}`` pairs from the top-k
            highest-scoring successful episodes, suitable for few-shot prompts.
        """
        owner = _training_export_owner(self)
        owner.flush()
        all_records = owner._load_all()
        filtered = [
            r for r in all_records if _record_allowed_for_training_export(r) and r.task_type == task_type and r.success
        ]
        filtered.sort(key=lambda r: -r.score)
        return [{"input": r.task, "output": r.response} for r in filtered[:k]]

    def export_ranking_dataset(self) -> list[dict[str, Any]]:
        """Export grouped, ranked responses for reward-model training.

        Groups records by task text, ranks all responses best-to-worst by score.
        Only groups with 2+ responses are included.

        Returns:
            List of ``{"prompt": ..., "responses": [...]}`` dicts where
            ``responses`` is a list of ``{"response": ..., "score": ...}``
            dicts ordered from highest to lowest score.
        """
        owner = _training_export_owner(self)
        owner.flush()
        all_records = [r for r in owner._load_all() if _record_allowed_for_training_export(r)]

        by_task: dict[str, list[TrainingRecord]] = defaultdict(list)
        for r in all_records:
            key = _training_task_group_key(r)
            by_task[key].append(r)

        result: list[dict[str, Any]] = []
        for group in by_task.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda r: -r.score)
            result.append({
                "prompt": group[0].task,
                "responses": [{"response": r.response, "score": r.score} for r in group],
            })
        return result

    # ------------------------------------------------------------------
    # Structured trace storage
    # ------------------------------------------------------------------

    def store_trace(
        self,
        task_id: str,
        prompt: str,
        output: str,
        model_id: str | None = None,
        inspector_verdict: dict[str, Any] | None = None,
        errors: list[str] | None = None,
    ) -> Path:
        """Write a structured execution trace for a single task to disk.

        Creates a subdirectory under ``{output_dir}/traces/{task_id}/`` and
        writes ``prompt.txt``, ``output.txt``, and optionally
        ``inspector_verdict.json`` and ``errors.log``. Both prompt and output
        are truncated to 10 000 characters.

        Args:
            task_id: Unique identifier for the task being traced.
            prompt: The full prompt sent to the model.
            output: The raw model output.
            model_id: Optional model identifier for provenance tracking.
            inspector_verdict: Optional dict from the Inspector agent
                (e.g. ``{"passed": False, "issues": [...]}``) - serialised
                to JSON if provided.
            errors: Optional list of error strings encountered during
                execution - written one-per-line to ``errors.log``.

        Returns:
            Path to the created trace directory.

        Raises:
            ValueError: If ``model_id`` is provided but blank.
            OSError: If the trace directory or files cannot be written.
        """
        if model_id is not None and not str(model_id).strip():
            raise ValueError("model_id is required when provided")

        _TRACE_TRUNCATE = 10000
        traces_dir = _training_export_owner(self)._output_path.parent / "traces"
        trace_dir = _safe_trace_dir(traces_dir, task_id)
        trace_dir.mkdir(parents=True, exist_ok=True)

        redacted_prompt = str(_redact_trace_value(prompt))
        redacted_output = str(_redact_trace_value(output))
        (trace_dir / "prompt.txt").write_text(redacted_prompt[:_TRACE_TRUNCATE], encoding="utf-8")
        (trace_dir / "output.txt").write_text(redacted_output[:_TRACE_TRUNCATE], encoding="utf-8")
        (trace_dir / "task_id.txt").write_text(str(task_id), encoding="utf-8")

        if inspector_verdict is not None:
            redacted_verdict = _redact_trace_value(inspector_verdict)
            (trace_dir / "inspector_verdict.json").write_text(
                json.dumps(redacted_verdict, ensure_ascii=False),
                encoding="utf-8",
            )

        if errors is not None:
            redacted_errors = [str(_redact_trace_value(error)) for error in errors]
            (trace_dir / "errors.log").write_text(
                "\n".join(redacted_errors),
                encoding="utf-8",
            )

        if model_id:
            logger.debug("Stored trace for task %s (model=%s)", task_id, model_id)

        return trace_dir

    def load_trace(self, task_id: str) -> dict[str, Any] | None:
        """Read a previously stored trace back from disk.

        Args:
            task_id: The task identifier whose trace directory to load.

        Returns:
            Dict with ``prompt``, ``output``, and optionally
            ``inspector_verdict`` and ``errors`` keys, or ``None`` if no
            trace directory exists for the given task_id.
        """
        traces_dir = _training_export_owner(self)._output_path.parent / "traces"
        trace_dir = _safe_trace_dir(traces_dir, task_id)
        if not trace_dir.is_dir():
            return None
        return _read_trace_dir(trace_dir, task_id)

    def get_recent_traces(
        self,
        limit: int = 20,
        failed_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Return recently stored traces, newest first.

        Args:
            limit: Maximum number of traces to return.
            failed_only: When ``True``, only return traces where
                ``inspector_verdict.get("passed")`` is ``False``.

        Returns:
            List of trace dicts each with an added ``task_id`` key, sorted
            newest-first by directory mtime, capped at ``limit`` entries.
        """
        traces_dir = _training_export_owner(self)._output_path.parent / "traces"
        if not traces_dir.is_dir():
            return []

        subdirs = sorted(
            (d for d in traces_dir.iterdir() if d.is_dir()),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )

        results: list[dict[str, Any]] = []
        for subdir in subdirs:
            if len(results) >= limit:
                break
            trace = _read_trace_dir(subdir, subdir.name)
            trace.setdefault("task_id", subdir.name)
            if failed_only:
                verdict = trace.get("inspector_verdict")
                if not isinstance(verdict, dict) or verdict.get("passed") is not False:
                    continue
            results.append(trace)

        return results
