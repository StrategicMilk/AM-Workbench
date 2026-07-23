"""TrainingRecord dataclass — the atomic unit of training data.

Each record represents a single agent execution: the task, the full prompt
sent to the model, the model's response, and quality metadata.  Records are
written to JSONL by TrainingDataCollector and consumed by the SFT/DPO/ranking
export methods.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from vetinari.utils.serialization import dataclass_to_dict


@dataclass(frozen=True, slots=True)
class TrainingRecord:
    """A single execution record for training data collection.

    Attributes:
        record_id: Unique identifier prefixed with ``tr_``.
        timestamp: ISO-8601 UTC creation timestamp.
        task: Short human-readable task description (user prompt).
        prompt: Full prompt sent to the LLM (system + user).
        response: Raw LLM output.
        score: Quality score in 0.0-1.0 range.
        model_id: Identifier of the model that produced the response.
        task_type: Broad task category (coding, research, analysis, etc.).
        prompt_variant_id: A/B variant identifier; empty when not applicable.
        agent_type: Agent that executed the task; empty when not applicable.
        latency_ms: Wall-clock inference latency in milliseconds.
        tokens_used: Total tokens consumed (prompt + completion).
        success: Whether the task completed without an error.
        vram_used_gb: VRAM usage at inference time in GB.
        benchmark_suite: Name of the benchmark suite that validated this output.
        benchmark_pass: Whether the output passed benchmark validation.
        benchmark_score: Benchmark score in 0.0-1.0 range.
        rejection_reason: Why the Inspector rejected this output; empty string
            when the output was accepted.
        rejection_category: Short failure category label (e.g.
            ``"missing_error_handling"``); empty string when accepted.
        inspector_feedback: Full Inspector feedback summary text; empty string
            when no feedback was recorded.
        schema_version: JSONL schema evolution version.
        metadata: Free-form dict of additional context.
    """

    record_id: str
    timestamp: str
    task: str  # Task description / user prompt
    prompt: str  # Full prompt sent to LLM (system + user)
    response: str  # LLM output
    score: float  # Quality score (0.0-1.0)
    model_id: str  # Model that produced the response
    task_type: str  # coding / research / analysis / etc.
    prompt_variant_id: str = ""  # For A/B tracking
    agent_type: str = ""  # Agent that executed this task
    latency_ms: int = 0
    tokens_used: int = 0
    success: bool = True
    vram_used_gb: float = 0.0
    benchmark_suite: str = ""  # Benchmark suite name (e.g. "toolbench", "swe-bench")
    benchmark_pass: bool = False  # Whether the output passed benchmark validation
    benchmark_score: float = 0.0  # Benchmark score 0.0-1.0 (pass_rate or avg_score)
    rejection_reason: str = ""  # Why Inspector rejected this output (empty if accepted)
    rejection_category: str = ""  # Failure category of the rejection (e.g., "missing_error_handling")
    inspector_feedback: str = ""  # Full Inspector feedback text
    trace_id: str = ""  # Pipeline trace identifier for end-to-end observability
    schema_version: int = 1  # JSONL schema evolution version; increment when record layout changes
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"TrainingRecord(record_id={self.record_id!r}, model_id={self.model_id!r}, "
            f"task_type={self.task_type!r}, score={self.score!r}, success={self.success!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary using dataclasses.asdict.

        Returns:
            Dict with all TrainingRecord fields as JSON-serializable values.
        """
        return dataclass_to_dict(self)

    def to_sft_pair(self) -> dict[str, str | float]:
        """Convert to a (prompt, completion) pair for SFT training.

        Returns:
            Dict with keys: prompt, completion, score, task_type.
        """
        return {
            "prompt": self.prompt,
            "completion": self.response,
            "score": self.score,
            "task_type": self.task_type,
        }
