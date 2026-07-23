"""Model Calibration - first-load performance measurement and prior seeding.

Runs 5 short calibration prompts on a newly loaded model to measure:
- Token generation speed (tokens/sec)
- Memory usage under load
- Output quality per task type

Results seed Thompson BetaArm priors with informed values instead of
the default Beta(2,2), giving the model selector better starting points.

The calibration budget is 30 seconds total.

Usage::

    from vetinari.models.calibration import calibrate_model

    results = calibrate_model(model_id, llm_instance)
    # -> CalibrationResult with per-task metrics
"""

from __future__ import annotations

import importlib
import logging
import queue
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from typing import Any

logger = logging.getLogger(__name__)


# Total time budget for all calibration prompts (seconds)
CALIBRATION_BUDGET_SECONDS = 30

# Per-prompt time limit (seconds) - prevents one slow prompt from consuming the budget
PER_PROMPT_TIMEOUT_SECONDS = 8

# Calibration prompts: (task_type, system_prompt, user_prompt, max_tokens)
_CALIBRATION_PROMPTS: list[tuple[str, str, str, int]] = [
    (
        "coding",
        "You are a Python expert.",
        "Write a function that checks if a number is prime. Include type hints.",
        150,
    ),
    (
        "reasoning",
        "You are a logical reasoning assistant.",
        "If all roses are flowers and some flowers fade quickly, can we conclude that some roses fade quickly? Explain.",
        100,
    ),
    (
        "general",
        "You are a helpful assistant.",
        "Explain what a hash table is in 2-3 sentences.",
        80,
    ),
    (
        "instruction",
        "You follow instructions precisely.",
        "List exactly 5 programming languages that compile to native code. Format: numbered list.",
        60,
    ),
    (
        "creative",
        "You are a creative writer.",
        "Write a one-sentence opening line for a sci-fi short story set on Mars.",
        40,
    ),
]


def _calibration_params(task_type: str) -> tuple[float, float]:
    """Return effective temperature and top-p values for calibration."""
    try:
        from vetinari.config.inference_config import get_inference_config

        params = get_inference_config().get_effective_params(task_type)
        return float(params.get("temperature", 0.3)), float(params.get("top_p", 0.9))
    except Exception:
        logger.warning("Exception handled by  calibration params fallback", exc_info=True)
        return 0.3, 0.9


def _complete_calibration_task(
    task_cal: TaskCalibration, output: str, tokens_used: int, elapsed: float
) -> TaskCalibration:
    """Populate a TaskCalibration after a successful prompt response."""
    return replace(
        task_cal,
        tokens_generated=tokens_used,
        tokens_per_second=tokens_used / max(elapsed, 0.001),
        latency_ms=int(elapsed * 1000),
        output_length=len(output),
        completed=bool(output.strip()),
    )


def _run_calibration_prompt(
    llm: Any,
    task_type: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int,
    effective_timeout: float,
) -> TaskCalibration:
    """Execute one calibration prompt and return its timing metrics."""
    task_cal = TaskCalibration(task_type=task_type)
    task_start = time.time()
    try:
        temperature, top_p = _calibration_params(task_type)
        response = _call_llm_with_timeout(
            llm,
            timeout_seconds=effective_timeout,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            min_p=0.05,
        )
        elapsed = time.time() - task_start
        choices = response.get("choices", [])
        output = choices[0]["message"]["content"] if choices else ""
        usage = response.get("usage", {})
        tokens_used = usage.get("completion_tokens", 0) or max(1, len(output) // 4)
        task_cal = _complete_calibration_task(task_cal, output, tokens_used, elapsed)
        logger.debug(
            "[Calibration] %s: %d tokens in %.1fs (%.1f tok/s)",
            task_type,
            tokens_used,
            elapsed,
            task_cal.tokens_per_second,
        )
    except Exception as exc:
        elapsed = time.time() - task_start
        task_cal = replace(task_cal, latency_ms=int(elapsed * 1000))
        logger.warning("[Calibration] %s failed: %s", task_type, exc)
    return task_cal


def _call_llm_with_timeout(llm: Any, *, timeout_seconds: float, **kwargs: Any) -> dict[str, Any]:
    """Run a blocking llama-cpp call behind a wall-clock timeout."""
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def _target() -> None:
        try:
            result_queue.put(("ok", llm.create_chat_completion(**kwargs)))
        except Exception as exc:
            result_queue.put(("err", exc))

    thread = threading.Thread(target=_target, name="calibration-llm-call", daemon=True)
    thread.start()
    thread.join(max(0.001, timeout_seconds))
    if thread.is_alive():
        raise TimeoutError(f"calibration prompt exceeded {timeout_seconds:.1f}s")
    status, payload = result_queue.get_nowait()
    if status == "err":
        raise payload
    return payload


def _snapshot_memory_usage_mb() -> float:
    """Return current process RSS in MB, or zero when unavailable."""
    try:
        psutil = importlib.import_module("psutil")
        process = psutil.Process()
        return round(process.memory_info().rss / (1024 * 1024), 1)
    except Exception:
        logger.warning("Could not measure memory during calibration")
        return 0.0


def _finalize_calibration_result(result: CalibrationResult, budget_start: float) -> None:
    """Populate aggregate calibration result fields."""
    result.total_time_seconds = round(time.time() - budget_start, 1)
    completed = [task for task in result.tasks if task.completed]
    result.completed_count = len(completed)
    if completed:
        result.avg_tokens_per_second = round(sum(task.tokens_per_second for task in completed) / len(completed), 1)
    result.memory_usage_mb = _snapshot_memory_usage_mb()


@dataclass(frozen=True, slots=True)
class TaskCalibration:
    """Calibration metrics for a single task type."""

    task_type: str
    tokens_generated: int = 0
    tokens_per_second: float = 0.0
    latency_ms: int = 0
    output_length: int = 0
    completed: bool = False

    def __repr__(self) -> str:
        return (
            f"TaskCalibration(type={self.task_type!r}, tok/s={self.tokens_per_second:.1f}, latency={self.latency_ms}ms)"
        )


@dataclass
class CalibrationResult:
    """Complete calibration results for a model.

    Contains per-task metrics and aggregate statistics for seeding
    Thompson sampling priors.
    """

    model_id: str
    total_time_seconds: float = 0.0
    tasks: list[TaskCalibration] = field(default_factory=list)
    avg_tokens_per_second: float = 0.0
    memory_usage_mb: float = 0.0
    completed_count: int = 0

    def __repr__(self) -> str:
        return (
            f"CalibrationResult(model={self.model_id!r}, "
            f"avg_tok/s={self.avg_tokens_per_second:.1f}, "
            f"completed={self.completed_count}/{len(self.tasks)})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return asdict(self)

    def get_thompson_priors(self) -> dict[str, tuple[float, float]]:
        """Compute Thompson BetaArm prior (alpha, beta) per task type.

        Maps calibration quality into informed priors. A model that
        generates fast, complete output gets a higher alpha (more
        optimistic prior).

        Returns:
            Dict mapping task_type to (alpha, beta) tuple.
        """
        priors: dict[str, tuple[float, float]] = {}
        for task in self.tasks:
            if task.completed:
                # Scale alpha by tokens/sec relative to a baseline of 20 tok/s
                speed_factor = min(2.0, task.tokens_per_second / 20.0)
                alpha = 2.0 + speed_factor * 3.0  # Range: 2-8
                beta = 2.0  # Keep beta constant
            else:
                # Failed calibration - pessimistic prior
                alpha = 1.0
                beta = 3.0
            priors[task.task_type] = (round(alpha, 1), round(beta, 1))
        return priors


def calibrate_model(model_id: str, llm: Any) -> CalibrationResult:
    """Run calibration prompts on a freshly loaded model.

    Measures token generation speed, output quality, and memory usage
    within a 30-second total budget. Results are used to seed Thompson
    sampling priors.

    Args:
        model_id: Model identifier for logging and result tracking.
        llm: A loaded ``llama_cpp.Llama`` instance.

    Returns:
        CalibrationResult with per-task and aggregate metrics.
    """
    result = CalibrationResult(model_id=model_id)
    budget_start = time.time()

    logger.info("[Calibration] Starting calibration for %s (budget=%ds)", model_id, CALIBRATION_BUDGET_SECONDS)

    for task_type, system_prompt, user_prompt, max_tokens in _CALIBRATION_PROMPTS:
        elapsed = time.time() - budget_start
        if elapsed >= CALIBRATION_BUDGET_SECONDS:
            logger.info("[Calibration] Budget exhausted after %.1fs - stopping", elapsed)
            break

        remaining = CALIBRATION_BUDGET_SECONDS - elapsed
        effective_timeout = min(PER_PROMPT_TIMEOUT_SECONDS, remaining)
        result.tasks.append(
            _run_calibration_prompt(
                llm,
                task_type,
                system_prompt,
                user_prompt,
                max_tokens,
                effective_timeout,
            )
        )

    _finalize_calibration_result(result, budget_start)

    logger.info(
        "[Calibration] Completed for %s: %d/%d tasks, avg %.1f tok/s, %.1fs total",
        model_id,
        result.completed_count,
        len(result.tasks),
        result.avg_tokens_per_second,
        result.total_time_seconds,
    )

    return result


def seed_thompson_priors(model_id: str, calibration: CalibrationResult) -> None:
    """Update Thompson sampling priors with calibration data.

    Args:
        model_id: Model identifier.
        calibration: Calibration results with per-task metrics.
    """
    try:
        from vetinari.learning.model_selector import get_model_selector

        selector = get_model_selector()
        priors = calibration.get_thompson_priors()
        set_prior = getattr(selector, "set_prior", None)

        for task_type, (alpha, beta) in priors.items():
            arm_key = f"{model_id}:{task_type}"
            if callable(set_prior):
                set_prior(arm_key, alpha, beta)
                logger.debug("[Calibration] Seeded Thompson prior for %s: alpha=%.1f, beta=%.1f", arm_key, alpha, beta)

    except Exception as exc:
        logger.warning("Failed to seed Thompson priors: %s", exc)
