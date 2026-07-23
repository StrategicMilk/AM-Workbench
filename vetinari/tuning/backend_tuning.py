"""Fail-closed contracts for benchmark-backed backend tuning candidates."""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from vetinari.boundary_guards import route_enum_error

SUPPORTED_BACKENDS = {"vllm", "llama_cpp", "retry_policy", "http", "search", "scheduler"}
METRIC_NAMES = {
    "quality_score",
    "latency_ms",
    "throughput_tokens_s",
    "cost_usd",
    "memory_mb",
    "error_rate",
    "fallback_rate",
}
LOWER_IS_BETTER = {"latency_ms", "cost_usd", "memory_mb", "error_rate", "fallback_rate"}
HIGHER_IS_BETTER = {"quality_score", "throughput_tokens_s"}
DEFAULT_PROTECTED_METRICS = ("quality_score", "latency_ms", "cost_usd", "memory_mb", "error_rate", "fallback_rate")


class TuningBlockedError(ValueError):
    """Raised when a tuning candidate lacks safe benchmark or policy proof."""


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    """A reversible target for a failed backend tuning candidate."""

    target_profile: str
    command: str
    window_hours: int


@dataclass(frozen=True, slots=True)
class TuningProposal:
    """A validated candidate knob set for one backend."""

    backend: str
    knobs: dict[str, Any]
    profile: str
    previous_profile: str
    rollback: RollbackPlan

    def identity_material(self) -> dict[str, Any]:
        """Return stable material used by benchmark and proposal identities."""
        return {
            "backend": self.backend,
            "knobs": self.knobs,
            "profile": self.profile,
            "previous_profile": self.previous_profile,
            "rollback": {
                "target_profile": self.rollback.target_profile,
                "command": self.rollback.command,
                "window_hours": self.rollback.window_hours,
            },
        }

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TuningProposal(backend={self.backend!r}, knobs={self.knobs!r}, profile={self.profile!r})"


@dataclass(frozen=True, slots=True)
class BenchmarkMetricSnapshot:
    """Aggregate benchmark metrics for a backend/profile window."""

    backend: str
    profile: str
    task_count: int
    metrics: dict[str, float]
    task_ids: tuple[str, ...]
    noise: dict[str, float] = field(default_factory=dict)
    rollback: RollbackPlan | None = None
    raw_hash: str = ""

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BenchmarkMetricSnapshot(backend={self.backend!r}, profile={self.profile!r}, task_count={self.task_count!r})"


@dataclass(frozen=True, slots=True)
class BenchmarkVerdict:
    """Outcome of comparing a candidate against a baseline."""

    passed: bool
    status: str
    backend: str
    baseline_profile: str
    candidate_profile: str
    baseline_hash: str
    candidate_hash: str
    representative_task_count: int
    confidence: str
    metric_deltas: dict[str, float]
    resource_cost: dict[str, float]
    rollback: RollbackPlan
    blockers: tuple[str, ...] = ()

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BenchmarkVerdict(passed={self.passed!r}, status={self.status!r}, backend={self.backend!r})"


@dataclass(frozen=True, slots=True)
class PromotionThresholds:
    """Promotion thresholds that every tuning policy must declare."""

    min_representative_tasks: int
    max_noise_coefficient: float
    min_quality_delta: float
    min_latency_improvement_ratio: float
    min_throughput_improvement_ratio: float
    max_cost_regression_ratio: float
    max_memory_regression_ratio: float
    max_error_regression_ratio: float
    max_fallback_regression_ratio: float

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"PromotionThresholds(min_representative_tasks={self.min_representative_tasks!r}, max_noise_coefficient={self.max_noise_coefficient!r}, min_quality_delta={self.min_quality_delta!r})"


@dataclass(frozen=True, slots=True)
class BackendTuningPolicy:
    """Allowed knobs and benchmark policy for a backend family."""

    backend: str
    allowed_knobs: frozenset[str]
    required_metrics: tuple[str, ...]
    thresholds: PromotionThresholds
    rollback_window_hours: int

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"BackendTuningPolicy(backend={self.backend!r}, allowed_knobs={self.allowed_knobs!r}, required_metrics={self.required_metrics!r})"


@dataclass(frozen=True, slots=True)
class BackendTuningConfig:
    """Full backend tuning policy loaded from YAML."""

    schema_version: str
    policies: dict[str, BackendTuningPolicy]


@dataclass(frozen=True, slots=True)
class TuningApplicationResult:
    """Pure candidate-overlay application result."""

    status: str
    backend: str
    settings: dict[str, Any]
    applied_knobs: tuple[str, ...] = ()
    blocked_reasons: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        """Whether the candidate was refused."""
        return self.status == "blocked"

    def __repr__(self) -> str:
        """Return a compact diagnostic representation."""
        return f"TuningApplicationResult(status={self.status!r}, backend={self.backend!r}, settings={self.settings!r})"


def stable_hash(payload: Any) -> str:
    """Hash JSON-serialisable payloads with deterministic key ordering.

    Returns:
        str value produced by stable_hash().
    """
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_backend_tuning_config(path: str | Path) -> BackendTuningConfig:
    """Load and validate backend tuning policy YAML.

        The loader fails closed: missing metric requirements, unknown backends or knobs,
        weak thresholds, and invalid rollback windows all raise ``TuningBlockedError``.

    Returns:
        Resolved backend tuning config value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    config_path = Path(path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TuningBlockedError(f"backend tuning policy unreadable: {config_path}") from exc
    if not isinstance(raw, dict):
        raise TuningBlockedError("backend tuning policy must be a mapping")
    schema_version = str(raw.get("schema_version", "")).strip()
    if not schema_version:
        raise TuningBlockedError("backend tuning policy missing schema_version")
    raw_policies = raw.get("backends")
    if not isinstance(raw_policies, dict) or not raw_policies:
        raise TuningBlockedError("backend tuning policy must declare backends")

    policies: dict[str, BackendTuningPolicy] = {}
    for backend, value in raw_policies.items():
        _require_supported_backend(str(backend), field_name="backend_policy")
        if not isinstance(value, dict):
            raise TuningBlockedError(f"backend policy must be a mapping: {backend}")
        knobs = value.get("allowed_knobs")
        if not isinstance(knobs, list) or not all(isinstance(k, str) and k.strip() for k in knobs):
            raise TuningBlockedError(f"{backend} must declare allowed_knobs")
        allowed = _allowed_knobs_for_backend(backend)
        unknown = sorted(set(knobs) - allowed)
        if unknown:
            raise TuningBlockedError(f"{backend} declares unsupported knobs: {unknown}")
        required_metrics = tuple(value.get("required_metrics") or ())
        if not required_metrics or not set(DEFAULT_PROTECTED_METRICS).issubset(required_metrics):
            raise TuningBlockedError(f"{backend} missing protected metric requirements")
        if unknown_metrics := sorted(set(required_metrics) - METRIC_NAMES):
            raise TuningBlockedError(f"{backend} declares unknown metrics: {unknown_metrics}")
        rollback_window = _positive_int(value.get("rollback_window_hours"), f"{backend}.rollback_window_hours")
        policies[backend] = BackendTuningPolicy(
            backend=backend,
            allowed_knobs=frozenset(knobs),
            required_metrics=required_metrics,
            thresholds=_load_thresholds(value.get("promotion_thresholds"), backend),
            rollback_window_hours=rollback_window,
        )
    return BackendTuningConfig(schema_version=schema_version, policies=policies)


def load_metric_snapshot(path: str | Path) -> BenchmarkMetricSnapshot:
    """Load one benchmark metrics file and fail closed on missing proof.

    Returns:
        Resolved metric snapshot value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    metrics_path = Path(path)
    try:
        raw = json.loads(metrics_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TuningBlockedError(f"benchmark metrics unreadable: {metrics_path}") from exc
    return metric_snapshot_from_mapping(raw, raw_hash=stable_hash(raw))


def metric_snapshot_from_mapping(raw: dict[str, Any], *, raw_hash: str | None = None) -> BenchmarkMetricSnapshot:
    """Build an aggregate metric snapshot from fixture or runner output.

    Returns:
        BenchmarkMetricSnapshot value produced by metric_snapshot_from_mapping().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not isinstance(raw, dict):
        raise TuningBlockedError("benchmark metrics must be a mapping")
    backend = _require_text(raw.get("backend"), "metrics.backend")
    _require_supported_backend(backend, field_name="metrics.backend")
    profile = _require_text(raw.get("profile"), "metrics.profile")
    samples = raw.get("samples")
    if not isinstance(samples, list) or not samples:
        raise TuningBlockedError("benchmark metrics require non-empty samples")
    task_ids: list[str] = []
    values: dict[str, list[float]] = {metric: [] for metric in METRIC_NAMES}
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise TuningBlockedError(f"benchmark sample {index} must be a mapping")
        task_ids.append(_require_text(sample.get("task_id"), f"samples[{index}].task_id"))
        for metric in DEFAULT_PROTECTED_METRICS:
            if metric not in sample:
                raise TuningBlockedError(f"sample {index} missing metric: {metric}")
        for metric in METRIC_NAMES:
            if metric in sample:
                values[metric].append(_finite_float(sample[metric], f"samples[{index}].{metric}"))
    aggregates = {metric: sum(items) / len(items) for metric, items in values.items() if items}
    noise = {
        metric: _coefficient_of_variation(items)
        for metric, items in values.items()
        if len(items) > 1 and metric in DEFAULT_PROTECTED_METRICS
    }
    rollback = None
    if rollback_raw := raw.get("rollback"):
        rollback = _load_rollback(rollback_raw, int(raw.get("rollback_window_hours") or 24))
    return BenchmarkMetricSnapshot(
        backend=backend,
        profile=profile,
        task_count=len(samples),
        metrics=aggregates,
        task_ids=tuple(task_ids),
        noise=noise,
        rollback=rollback,
        raw_hash=raw_hash or stable_hash(raw),
    )


def validate_candidate(config: BackendTuningConfig, candidate: TuningProposal) -> None:
    """Validate that a candidate only names policy-supported knobs.

    Args:
        config: Config value consumed by validate_candidate().
        candidate: Candidate value consumed by validate_candidate().

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    if not stable_hash(candidate.identity_material()):
        raise TuningBlockedError("candidate identity material must be stable-hashable")
    policy = config.policies.get(candidate.backend)
    _require_supported_backend(candidate.backend, field_name="candidate.backend")
    if policy is None:
        raise TuningBlockedError(f"candidate backend is not policy-supported: {candidate.backend}")
    unknown = sorted(set(candidate.knobs) - policy.allowed_knobs)
    if unknown:
        raise TuningBlockedError(f"candidate uses unsupported knobs: {unknown}")
    if candidate.rollback.window_hours <= 0:
        raise TuningBlockedError("candidate rollback window must be positive")
    if not candidate.previous_profile.strip() or not candidate.rollback.target_profile.strip():
        raise TuningBlockedError("candidate requires previous profile and rollback target")


def evaluate_metric_windows(
    config: BackendTuningConfig,
    baseline: BenchmarkMetricSnapshot,
    candidate: BenchmarkMetricSnapshot,
) -> BenchmarkVerdict:
    """Compare baseline and candidate snapshots and return a fail-closed verdict.

    Args:
        config: Config value consumed by evaluate_metric_windows().
        baseline: Baseline value consumed by evaluate_metric_windows().
        candidate: Candidate value consumed by evaluate_metric_windows().

    Returns:
        BenchmarkVerdict value produced by evaluate_metric_windows().
    """
    blockers: list[str] = []
    if baseline.backend != candidate.backend:
        blockers.append("backend mismatch")
    policy = config.policies.get(candidate.backend)
    if policy is None:
        blockers.append("unsupported backend")
        policy = next(iter(config.policies.values()))
    required = set(policy.required_metrics)
    missing_baseline = sorted(required - set(baseline.metrics))
    missing_candidate = sorted(required - set(candidate.metrics))
    if missing_baseline:
        blockers.append(f"baseline missing metrics: {missing_baseline}")
    if missing_candidate:
        blockers.append(f"candidate missing metrics: {missing_candidate}")
    if candidate.task_count < policy.thresholds.min_representative_tasks:
        blockers.append("representative task count below policy minimum")
    if not candidate.rollback:
        blockers.append("candidate missing rollback metadata")
    blockers.extend(
        f"candidate metric too noisy: {metric}"
        for metric in required & set(candidate.noise)
        if candidate.noise[metric] > policy.thresholds.max_noise_coefficient
    )

    deltas = {
        metric: candidate.metrics[metric] - baseline.metrics[metric]
        for metric in sorted(set(baseline.metrics) & set(candidate.metrics))
    }
    if not blockers:
        _append_regression_blockers(policy.thresholds, baseline.metrics, candidate.metrics, blockers)

    rollback = candidate.rollback or RollbackPlan(
        target_profile=baseline.profile,
        command=f"restore backend profile {baseline.profile}",
        window_hours=policy.rollback_window_hours,
    )
    passed = not blockers
    return BenchmarkVerdict(
        passed=passed,
        status="proposed" if passed else "blocked",
        backend=candidate.backend,
        baseline_profile=baseline.profile,
        candidate_profile=candidate.profile,
        baseline_hash=baseline.raw_hash,
        candidate_hash=candidate.raw_hash,
        representative_task_count=candidate.task_count,
        confidence=_confidence(candidate.task_count, candidate.noise),
        metric_deltas=deltas,
        resource_cost={
            "cost_usd_delta": deltas.get("cost_usd", 0.0),
            "memory_mb_delta": deltas.get("memory_mb", 0.0),
        },
        rollback=rollback,
        blockers=tuple(blockers),
    )


def _append_regression_blockers(
    thresholds: PromotionThresholds,
    baseline: dict[str, float],
    candidate: dict[str, float],
    blockers: list[str],
) -> None:
    quality_delta = candidate["quality_score"] - baseline["quality_score"]
    if quality_delta < thresholds.min_quality_delta:
        blockers.append("quality did not meet minimum delta")
    baseline_latency = baseline["latency_ms"]
    if baseline_latency <= 0:
        blockers.append("baseline latency must be positive")
    else:
        latency_improvement = (baseline_latency - candidate["latency_ms"]) / baseline_latency
        if latency_improvement < thresholds.min_latency_improvement_ratio:
            blockers.append("latency improvement below threshold")
    baseline_throughput = baseline.get("throughput_tokens_s", 0.0)
    if baseline_throughput <= 0:
        blockers.append("baseline throughput must be positive")
        throughput_improvement = 0.0
    else:
        throughput_improvement = (candidate.get("throughput_tokens_s", 0.0) - baseline_throughput) / baseline_throughput
    if baseline_throughput > 0 and throughput_improvement < thresholds.min_throughput_improvement_ratio:
        blockers.append("throughput improvement below threshold")
    _block_if_regressed("cost_usd", thresholds.max_cost_regression_ratio, baseline, candidate, blockers)
    _block_if_regressed("memory_mb", thresholds.max_memory_regression_ratio, baseline, candidate, blockers)
    _block_if_regressed("error_rate", thresholds.max_error_regression_ratio, baseline, candidate, blockers)
    _block_if_regressed("fallback_rate", thresholds.max_fallback_regression_ratio, baseline, candidate, blockers)


def _block_if_regressed(
    metric: str,
    allowed_ratio: float,
    baseline: dict[str, float],
    candidate: dict[str, float],
    blockers: list[str],
) -> None:
    base = baseline.get(metric, 0.0)
    current = candidate.get(metric, 0.0)
    allowed = base * (1.0 + allowed_ratio)
    if current > allowed:
        blockers.append(f"{metric} regressed")


def _confidence(task_count: int, noise: dict[str, float]) -> str:
    worst_noise = max(noise.values(), default=0.0)
    if task_count >= 5 and worst_noise <= 0.05:
        return "high"
    if task_count >= 3 and worst_noise <= 0.15:
        return "medium"
    return "low"


def _load_thresholds(raw: Any, backend: str) -> PromotionThresholds:
    if not isinstance(raw, dict):
        raise TuningBlockedError(f"{backend} must declare promotion_thresholds")
    return PromotionThresholds(
        min_representative_tasks=_positive_int(
            raw.get("min_representative_tasks"),
            f"{backend}.promotion_thresholds.min_representative_tasks",
        ),
        max_noise_coefficient=_bounded_float(raw.get("max_noise_coefficient"), 0.0, 1.0, "max_noise_coefficient"),
        min_quality_delta=_bounded_float(raw.get("min_quality_delta"), 0.0, 1.0, "min_quality_delta"),
        min_latency_improvement_ratio=_bounded_float(
            raw.get("min_latency_improvement_ratio"),
            0.0,
            1.0,
            "min_latency_improvement_ratio",
        ),
        min_throughput_improvement_ratio=_bounded_float(
            raw.get("min_throughput_improvement_ratio"),
            0.0,
            1.0,
            "min_throughput_improvement_ratio",
        ),
        max_cost_regression_ratio=_bounded_float(raw.get("max_cost_regression_ratio"), 0.0, 1.0, "cost"),
        max_memory_regression_ratio=_bounded_float(raw.get("max_memory_regression_ratio"), 0.0, 1.0, "memory"),
        max_error_regression_ratio=_bounded_float(raw.get("max_error_regression_ratio"), 0.0, 1.0, "error"),
        max_fallback_regression_ratio=_bounded_float(raw.get("max_fallback_regression_ratio"), 0.0, 1.0, "fallback"),
    )


def _load_rollback(raw: Any, fallback_window_hours: int) -> RollbackPlan:
    if not isinstance(raw, dict):
        raise TuningBlockedError("rollback metadata must be a mapping")
    return RollbackPlan(
        target_profile=_require_text(raw.get("target_profile"), "rollback.target_profile"),
        command=_require_text(raw.get("command"), "rollback.command"),
        window_hours=_positive_int(raw.get("window_hours", fallback_window_hours), "rollback.window_hours"),
    )


def _allowed_knobs_for_backend(backend: str) -> set[str]:
    _require_supported_backend(backend, field_name="backend")
    return {
        "vllm": {"max_num_batched_tokens", "enable_prefix_caching", "prefix_cache_salt"},
        "llama_cpp": {"n_gpu_layers", "n_batch", "n_ctx", "type_k", "type_v", "prompt_cache"},
        "retry_policy": {"max_attempts", "base_delay_ms", "max_delay_ms"},
        "http": {"pool_max_connections", "timeout_s"},
        "search": {"fallback_order", "timeout_s"},
        "scheduler": {"lane_budgets"},
    }[backend]


def _require_supported_backend(backend: str, *, field_name: str) -> str:
    if backend not in SUPPORTED_BACKENDS:
        route_enum_error(
            field_name=field_name,
            received=backend,
            allowed=SUPPORTED_BACKENDS,
            route_id="backend-tuning",
        )
        raise TuningBlockedError(f"unsupported backend: {backend}")
    return backend


def _positive_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise TuningBlockedError(f"{field_name} must be a positive integer")
    return value


def _bounded_float(value: Any, minimum: float, maximum: float, field_name: str) -> float:
    value = _finite_float(value, field_name)
    if not minimum <= value <= maximum:
        raise TuningBlockedError(f"{field_name} must be between {minimum} and {maximum}")
    return float(value)


def _finite_float(value: Any, field_name: str) -> float:
    if not isinstance(value, int | float):
        raise TuningBlockedError(f"{field_name} must be numeric")
    result = float(value)
    if result != result or result in {float("inf"), float("-inf")}:
        raise TuningBlockedError(f"{field_name} must be finite")
    return result


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TuningBlockedError(f"{field_name} must be non-empty")
    return value


def _coefficient_of_variation(values: list[float]) -> float:
    mean = sum(values) / len(values)
    if mean == 0:
        return 0.0
    return abs(statistics.pstdev(values) / mean)
