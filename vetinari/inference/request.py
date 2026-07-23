"""Request and configuration contracts for CPU-tier inference."""

from __future__ import annotations

from dataclasses import dataclass

from vetinari.security.fail_closed import sanitize_untrusted_text
from vetinari.security.redaction import redact_repr


@dataclass(frozen=True, slots=True)
class RoutedInferenceRequest:
    """A typed inference request routed by capability and lane.

    Attributes:
        capability: Capability key from ``config/compute_routing.yaml``.
        prompt: Input text for the selected model.
        max_tokens: Maximum output tokens.
        lane: Work lane such as ``interactive`` or ``batch``.
        latency_budget_s: Maximum acceptable latency in seconds.
        quality_floor: Minimum quality label the target must satisfy.
        caller_subsystem: Optional subsystem name for provenance.
        grammar: Optional GBNF grammar to enforce in the selected backend.
        task_type: Optional task-type key for automatic grammar lookup.
    """

    capability: str
    prompt: str
    max_tokens: int
    lane: str
    latency_budget_s: float
    quality_floor: str = "standard"
    caller_subsystem: str = ""
    grammar: str | None = None
    task_type: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "capability", sanitize_untrusted_text(self.capability, max_length=120))
        object.__setattr__(self, "prompt", sanitize_untrusted_text(self.prompt, max_length=20_000))
        object.__setattr__(self, "lane", sanitize_untrusted_text(self.lane, max_length=80))
        object.__setattr__(self, "quality_floor", sanitize_untrusted_text(self.quality_floor, max_length=80))
        if self.caller_subsystem:
            object.__setattr__(
                self,
                "caller_subsystem",
                sanitize_untrusted_text(self.caller_subsystem, max_length=120),
            )
        if self.task_type:
            object.__setattr__(self, "task_type", sanitize_untrusted_text(self.task_type, max_length=120))
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.latency_budget_s <= 0:
            raise ValueError("latency_budget_s must be positive")

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return redact_repr(
            "RoutedInferenceRequest",
            {
                "capability": self.capability,
                "prompt": self.prompt,
                "max_tokens": self.max_tokens,
                "lane": self.lane,
            },
        )


@dataclass(frozen=True, slots=True)
class CpuTierConfig:
    """Configuration for the resident synthesis tier.

    Attributes:
        model_path: GGUF or model identifier for the synthesis tier.
        smoke_test_timeout_s: Timeout for the post-load smoke test.
        process_mode: Runtime process model. ``in_process`` is v1.
        mlock_weights: Whether to request locked model pages.
        release_timeout_s: Default timeout for memory handoff.
    """

    model_path: str
    smoke_test_timeout_s: float
    process_mode: str = "in_process"
    mlock_weights: bool = False
    release_timeout_s: float = 30.0

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return redact_repr(
            "CpuTierConfig",
            {
                "model_path": self.model_path,
                "smoke_test_timeout_s": self.smoke_test_timeout_s,
                "process_mode": self.process_mode,
            },
        )


@dataclass(frozen=True, slots=True)
class MemoryPressureConfig:
    """Configuration for the memory-pressure watchdog.

    Attributes:
        poll_interval_s: Seconds between memory probes.
        release_threshold_mb: Free-memory threshold that triggers release.
        reload_threshold_mb: Free-memory threshold that permits reload.
        release_timeout_s: Timeout passed to ``request_release``.
    """

    poll_interval_s: float
    release_threshold_mb: int
    reload_threshold_mb: int
    release_timeout_s: float

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"MemoryPressureConfig(poll_interval_s={self.poll_interval_s!r}, release_threshold_mb={self.release_threshold_mb!r}, reload_threshold_mb={self.reload_threshold_mb!r})"


@dataclass(frozen=True, slots=True)
class EmbedderConfig:
    """Configuration for the CPU embedder.

    Attributes:
        model_id: Sentence-transformers model id or local path.
        batch_size: Default embedding batch size.
        device: Runtime device, normally ``cpu`` for this tier.
    """

    model_id: str
    batch_size: int = 32
    device: str = "cpu"


@dataclass(frozen=True, slots=True)
class BonsaiConfig:
    """Configuration for the bonsai classifier.

    Attributes:
        model_id: Small classifier model id.
        escalation_margin: Minimum top-two logprob margin before trusting bonsai.
        max_new_tokens: Maximum constrained output tokens.
    """

    model_id: str
    escalation_margin: float = 0.15
    max_new_tokens: int = 64


@dataclass(frozen=True, slots=True)
class PersistentJobsConfig:
    """Configuration for the durable event-driven job queue.

    Attributes:
        db_path: SQLite path for durable background inference jobs.
        worker_poll_interval_s: Worker sleep interval when idle.
        max_in_flight: Maximum concurrent worker-owned jobs.
        claim_timeout_s: Stuck-job reclaim threshold. A row in ``running``
            whose ``updated_at`` is older than this becomes a recovery
            candidate when ``recover_stuck_jobs`` runs. Workers MUST send
            ``heartbeat`` calls below half this value.
        heartbeat_interval_s: Recommended worker heartbeat cadence; should
            be well below ``claim_timeout_s``.
    """

    db_path: str
    worker_poll_interval_s: float = 1.0
    max_in_flight: int = 4
    claim_timeout_s: float = 300.0
    heartbeat_interval_s: float = 30.0

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return redact_repr(
            "PersistentJobsConfig",
            {
                "db_path": self.db_path,
                "worker_poll_interval_s": self.worker_poll_interval_s,
                "max_in_flight": self.max_in_flight,
            },
        )


__all__ = [
    "BonsaiConfig",
    "CpuTierConfig",
    "EmbedderConfig",
    "MemoryPressureConfig",
    "PersistentJobsConfig",
    "RoutedInferenceRequest",
]
