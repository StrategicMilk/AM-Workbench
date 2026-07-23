"""Training Quality Gate - evaluates trained models against baseline before deployment."""

from __future__ import annotations

import gc
import json
import logging
import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any

from vetinari.guards import GateError

logger = logging.getLogger(__name__)


def _optional_import_attr(module_name: str, attr_name: str) -> Any | None:
    try:
        return getattr(import_module(module_name), attr_name)
    except Exception as exc:
        logger.warning(
            "[TrainingQualityGate] Optional dependency %s.%s unavailable: %s",
            module_name,
            attr_name,
            exc,
        )
        return None


LocalInferenceAdapter: Any | None = _optional_import_attr(
    "vetinari.adapters.llama_cpp_local_adapter", "LocalInferenceAdapter"
)
get_quality_scorer: Any | None = _optional_import_attr("vetinari.learning.quality_scorer", "get_quality_scorer")


def _adapter_fallback_reason(result: dict[str, object]) -> str:
    """Return a reason when an adapter result must not feed training decisions."""
    if result.get("_is_fallback") is True or result.get("is_fallback") is True:
        return "_is_fallback"
    status = str(result.get("fallback_status") or "").lower()
    if status in {"fallback", "fallback_used", "degraded"}:
        return f"fallback_status={status}"
    if result.get("error"):
        return "adapter_error"
    return ""


def _dependency_unavailable_scores(eval_tasks: list[dict[str, str]], reason: str) -> list[dict[str, Any]]:
    return [
        {
            "quality": 0.0,
            "latency_ms": 0.0,
            "tokens": 0.0,
            "_dependency_unavailable": 1.0,
            "_failure_reason": reason,
        }
        for _ in eval_tasks
    ]


def _normalise_expected_answer(value: str) -> str:
    return " ".join(value.strip().split()).casefold()


def _matches_expected_answer(expected: str, output: str) -> bool:
    return _normalise_expected_answer(output) == _normalise_expected_answer(expected)


_quality_gate: TrainingQualityGate | None = None
_quality_gate_lock = threading.Lock()
_QUALITY_REJECT_THRESHOLD: float = -0.03
_QUALITY_DEPLOY_THRESHOLD: float = 0.02
_LATENCY_REJECT_RATIO: float = 2.0
_TOKEN_REJECT_RATIO: float = 1.5
FALLBACK_SENTINEL: str = "fallback"
_PEFT_ARTIFACT_TYPE = "peft_adapter"
_SAFETENSORS_FORMAT = "safetensors"


@dataclass(frozen=True, slots=True)
class TrainingGateDecision:
    """Result of a training quality gate evaluation."""

    decision: str
    baseline_quality: float
    candidate_quality: float
    quality_delta: float
    baseline_latency_ms: float
    candidate_latency_ms: float
    latency_ratio: float
    token_efficiency: float
    reasoning: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    eval_tasks_run: int = 0

    def __repr__(self) -> str:
        return (
            f"GateDecision(decision={self.decision!r}, "
            f"quality_delta={self.quality_delta:+.3f}, "
            f"latency_ratio={self.latency_ratio:.2f})"
        )


@dataclass(frozen=True, slots=True)
class TrainingEvaluationArtifact:
    """Typed provenance contract for evaluating a just-trained model artifact."""

    artifact_type: str
    model_format: str
    path: str
    base_model_id: str
    base_model_revision: str
    device: str


def _validate_training_artifact(artifact: TrainingEvaluationArtifact) -> Path:
    """Validate the supported PEFT+safetensors contract or fail closed."""
    if artifact.artifact_type != _PEFT_ARTIFACT_TYPE or artifact.model_format != _SAFETENSORS_FORMAT:
        raise GateError(
            "training_quality_gate",
            f"unsupported training artifact contract: {artifact.artifact_type}/{artifact.model_format}",
        )
    if artifact.device not in {"cpu", "cuda"}:
        raise GateError("training_quality_gate", f"unsupported evaluation device: {artifact.device}")
    if not artifact.base_model_revision or artifact.base_model_revision == "main":
        raise GateError("training_quality_gate", "immutable base-model revision is required")
    adapter_root = Path(artifact.path).resolve()
    config_path = adapter_root / "adapter_config.json"
    if not adapter_root.is_dir() or not config_path.is_file():
        raise GateError("training_quality_gate", "PEFT adapter configuration is missing")
    if not any(adapter_root.glob("*.safetensors")):
        raise GateError("training_quality_gate", "PEFT safetensors weights are missing")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError("training_quality_gate", "PEFT adapter configuration is unreadable") from exc
    configured_base = str(config.get("base_model_name_or_path") or "").strip().rstrip("/")
    expected_base = artifact.base_model_id.strip().rstrip("/")
    if not configured_base or configured_base != expected_base:
        raise GateError("training_quality_gate", "PEFT adapter base-model provenance mismatch")
    return adapter_root


class TrainingQualityGate:
    """Evaluates trained models against baseline before deployment."""

    def __init__(self) -> None:
        self._decisions: list[TrainingGateDecision] = []
        self._lock = threading.Lock()
        self._adapter: Any = None
        self._adapter_factory: Any = LocalInferenceAdapter

    @staticmethod
    def _no_eval_decision(candidate_model_id: str, baseline_model_id: str) -> TrainingGateDecision:
        """Return the fail-closed decision for an empty eval set."""
        logger.warning(
            "[TrainingQualityGate] No eval tasks available for %s vs %s - rejecting",
            candidate_model_id,
            baseline_model_id,
        )
        return TrainingGateDecision(
            decision="reject",
            baseline_quality=0.0,
            candidate_quality=0.0,
            quality_delta=0.0,
            baseline_latency_ms=0.0,
            candidate_latency_ms=0.0,
            latency_ratio=1.0,
            token_efficiency=1.0,
            reasoning="No evaluation tasks available - cannot certify training quality; rejecting to fail closed",
            eval_tasks_run=0,
        )

    @staticmethod
    def _unavailable_decision(
        eval_tasks: list[dict[str, str]],
        reasons: set[str],
    ) -> TrainingGateDecision:
        """Return the fail-closed decision for unavailable evaluation dependencies."""
        reasoning = (
            "Evaluation dependency unavailable: "
            + ", ".join(sorted(reason for reason in reasons if reason))
            + "; rejecting to fail closed"
        )
        return TrainingGateDecision(
            decision="reject",
            baseline_quality=0.0,
            candidate_quality=0.0,
            quality_delta=0.0,
            baseline_latency_ms=0.0,
            candidate_latency_ms=0.0,
            latency_ratio=1.0,
            token_efficiency=1.0,
            reasoning=reasoning,
            eval_tasks_run=len(eval_tasks),
        )

    @staticmethod
    def _aggregate_scores(
        baseline_scores: list[dict[str, Any]],
        candidate_scores: list[dict[str, Any]],
    ) -> tuple[float, float, float, float, float, float]:
        """Aggregate per-task scores into quality, latency, and token metrics."""
        b_quality = sum(s["quality"] for s in baseline_scores) / max(len(baseline_scores), 1)
        c_quality = sum(s["quality"] for s in candidate_scores) / max(len(candidate_scores), 1)
        b_latency = sum(s["latency_ms"] for s in baseline_scores) / max(len(baseline_scores), 1)
        c_latency = sum(s["latency_ms"] for s in candidate_scores) / max(len(candidate_scores), 1)
        b_tokens = sum(s["tokens"] for s in baseline_scores) / max(len(baseline_scores), 1)
        c_tokens = sum(s["tokens"] for s in candidate_scores) / max(len(candidate_scores), 1)
        return b_quality, c_quality, b_latency, c_latency, b_tokens, c_tokens

    def _record_decision(
        self,
        gate_decision: TrainingGateDecision,
        candidate_model_id: str,
        baseline_model_id: str,
    ) -> TrainingGateDecision:
        """Persist and log a gate decision."""
        with self._lock:
            self._decisions.append(gate_decision)
        logger.info(
            "[TrainingQualityGate] %s | %s vs %s | quality_delta=%+.3f latency_ratio=%.2f token_ratio=%.2f | %s",
            gate_decision.decision.upper(),
            candidate_model_id,
            baseline_model_id,
            gate_decision.quality_delta,
            gate_decision.latency_ratio,
            gate_decision.token_efficiency,
            gate_decision.reasoning,
        )
        return gate_decision

    def evaluate(
        self,
        candidate_model_id: str,
        baseline_model_id: str,
        eval_tasks: list[dict[str, str]] | None = None,
        *,
        candidate_artifact: TrainingEvaluationArtifact | None = None,
    ) -> TrainingGateDecision:
        """Run quality gate evaluation comparing candidate against baseline.

        Args:
            candidate_model_id: Model identifier used for routing or lookup.
            baseline_model_id: Model identifier used for routing or lookup.
            eval_tasks: Eval tasks value consumed by evaluate().
            candidate_artifact: Typed PEFT artifact contract for direct
                Transformers evaluation of a just-trained adapter.

        Returns:
            Value produced for the caller.

        Raises:
            Exception: Propagates model evaluation failures from the configured evaluator.
        """
        if eval_tasks is None:
            eval_tasks = self._get_default_eval_set()
        if not eval_tasks:
            return self._no_eval_decision(candidate_model_id, baseline_model_id)

        if candidate_artifact is None:
            baseline_scores = self._evaluate_model(baseline_model_id, eval_tasks)
            candidate_scores = self._evaluate_model(candidate_model_id, eval_tasks)
        else:
            if baseline_model_id != candidate_artifact.base_model_id:
                raise GateError("training_quality_gate", "baseline model does not match artifact provenance")
            if Path(candidate_model_id).resolve() != Path(candidate_artifact.path).resolve():
                raise GateError("training_quality_gate", "candidate model does not match artifact path")
            baseline_scores, candidate_scores = self._evaluate_hf_peft_pair(candidate_artifact, eval_tasks)
        if baseline_scores and candidate_scores:
            baseline_fallback = all(score.get("_fallback_sentinel") == FALLBACK_SENTINEL for score in baseline_scores)
            candidate_fallback = all(score.get("_fallback_sentinel") == FALLBACK_SENTINEL for score in candidate_scores)
            if baseline_fallback and candidate_fallback:
                raise GateError(
                    "training_quality_gate",
                    "mutual-fallback masking detected: both baseline and candidate are fallback outputs - gate cannot evaluate",
                )
        unavailable = {
            str(score.get("_failure_reason"))
            for score in (*baseline_scores, *candidate_scores)
            if score.get("_dependency_unavailable")
        }
        if unavailable:
            return self._unavailable_decision(eval_tasks, unavailable)

        b_quality, c_quality, b_latency, c_latency, b_tokens, c_tokens = self._aggregate_scores(
            baseline_scores,
            candidate_scores,
        )
        quality_delta = c_quality - b_quality
        latency_ratio = c_latency / max(b_latency, 1.0)
        token_ratio = c_tokens / max(b_tokens, 1.0)
        decision, reasoning = self._make_decision(quality_delta, latency_ratio, token_ratio)
        gate_decision = TrainingGateDecision(
            decision=decision,
            baseline_quality=round(b_quality, 4),
            candidate_quality=round(c_quality, 4),
            quality_delta=round(quality_delta, 4),
            baseline_latency_ms=round(b_latency, 1),
            candidate_latency_ms=round(c_latency, 1),
            latency_ratio=round(latency_ratio, 3),
            token_efficiency=round(token_ratio, 3),
            reasoning=reasoning,
            eval_tasks_run=len(eval_tasks),
        )
        return self._record_decision(gate_decision, candidate_model_id, baseline_model_id)

    def _evaluate_hf_peft_pair(
        self,
        artifact: TrainingEvaluationArtifact,
        eval_tasks: list[dict[str, str]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Evaluate immutable HF baseline and matching PEFT adapter directly."""
        adapter_root = _validate_training_artifact(artifact)
        try:
            import torch
            from peft import PeftModel
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except Exception as exc:
            logger.warning("[TrainingQualityGate] HF/PEFT evaluator dependencies unavailable: %s", exc)
            unavailable = _dependency_unavailable_scores(eval_tasks, "hf_peft_evaluator_unavailable")
            return unavailable, list(unavailable)
        if artifact.device == "cuda" and not torch.cuda.is_available():
            unavailable = _dependency_unavailable_scores(eval_tasks, "cuda_unavailable")
            return unavailable, list(unavailable)

        tokenizer: Any = None
        model: Any = None
        try:
            tokenizer = AutoTokenizer.from_pretrained(
                artifact.base_model_id,
                revision=artifact.base_model_revision,
            )
            model = AutoModelForCausalLM.from_pretrained(
                artifact.base_model_id,
                revision=artifact.base_model_revision,
                dtype=torch.bfloat16 if artifact.device == "cuda" else torch.float32,
            ).to(artifact.device)
            model.eval()
            self._warm_hf_model(model, tokenizer, artifact.device, eval_tasks[0]["prompt"])
            baseline_scores = self._evaluate_loaded_hf_model(
                model,
                tokenizer,
                artifact.device,
                eval_tasks,
            )
            model = PeftModel.from_pretrained(model, adapter_root, is_trainable=False).to(artifact.device)
            model.eval()
            self._warm_hf_model(model, tokenizer, artifact.device, eval_tasks[0]["prompt"])
            candidate_scores = self._evaluate_loaded_hf_model(
                model,
                tokenizer,
                artifact.device,
                eval_tasks,
            )
            self._apply_expected_loss_quality_scale(baseline_scores, candidate_scores)
            return baseline_scores, candidate_scores
        except GateError:
            raise
        except Exception as exc:
            logger.warning("[TrainingQualityGate] HF/PEFT evaluation failed closed: %s", exc)
            unavailable = _dependency_unavailable_scores(eval_tasks, "hf_peft_evaluation_failed")
            return unavailable, list(unavailable)
        finally:
            del model
            del tokenizer
            gc.collect()
            if artifact.device == "cuda" and torch.cuda.is_available():
                torch.cuda.empty_cache()

    @staticmethod
    def _expected_answer_loss(
        model: Any,
        tokenizer: Any,
        device: str,
        task: dict[str, str],
    ) -> tuple[float, int]:
        """Measure deterministic expected-continuation loss for one task."""
        import torch

        prompt = str(task.get("prompt") or "").strip()
        expected = str(task.get("expected") or "").strip()
        if not prompt or not expected:
            raise GateError("training_quality_gate", "HF/PEFT eval tasks require prompt and expected text")
        prompt_tokens = tokenizer(prompt, return_tensors="pt")["input_ids"]
        inputs = tokenizer(f"{prompt} {expected}", return_tensors="pt")
        inputs = {name: value.to(device) for name, value in inputs.items()}
        prompt_length = int(prompt_tokens.shape[-1])
        labels = inputs["input_ids"].clone()
        labels[:, :prompt_length] = -100
        target_tokens = int((labels != -100).sum().item())
        if target_tokens <= 0:
            raise GateError("training_quality_gate", "HF/PEFT eval task produced no expected-answer tokens")
        with torch.inference_mode():
            loss = model(**inputs, labels=labels).loss
        if not bool(torch.isfinite(loss)):
            raise GateError("training_quality_gate", "HF/PEFT expected-answer loss is not finite")
        return float(loss.item()), target_tokens

    @classmethod
    def _warm_hf_model(cls, model: Any, tokenizer: Any, device: str, prompt: str) -> None:
        """Warm deterministic scoring so latency comparison excludes one-time setup."""
        cls._expected_answer_loss(
            model,
            tokenizer,
            device,
            {"prompt": prompt, "expected": "warmup"},
        )

    @staticmethod
    def _apply_expected_loss_quality_scale(
        baseline_scores: list[dict[str, Any]],
        candidate_scores: list[dict[str, Any]],
    ) -> None:
        """Map paired expected-loss improvement onto the gate's quality scale."""
        if len(baseline_scores) != len(candidate_scores):
            raise GateError("training_quality_gate", "HF/PEFT score rows are not paired")
        for baseline, candidate in zip(baseline_scores, candidate_scores, strict=True):
            baseline_loss = float(baseline["expected_loss"])
            candidate_loss = float(candidate["expected_loss"])
            baseline["quality"] = 0.5
            candidate["quality"] = min(1.0, max(0.0, 0.5 + baseline_loss - candidate_loss))

    def _evaluate_loaded_hf_model(
        self,
        model: Any,
        tokenizer: Any,
        device: str,
        eval_tasks: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """Collect deterministic expected-answer losses for a loaded HF model."""
        import torch

        scores: list[dict[str, Any]] = []
        for task in eval_tasks:
            latencies: list[float] = []
            expected_loss = 0.0
            tokens = 0
            for _ in range(7):
                if device == "cuda":
                    torch.cuda.synchronize()
                start = time.monotonic()
                expected_loss, tokens = self._expected_answer_loss(model, tokenizer, device, task)
                if device == "cuda":
                    torch.cuda.synchronize()
                latencies.append((time.monotonic() - start) * 1000)
            scores.append({
                "quality": 0.0,
                "expected_loss": expected_loss,
                "latency_ms": statistics.median(latencies),
                "tokens": float(tokens),
            })
        return scores

    def _make_decision(
        self,
        quality_delta: float,
        latency_ratio: float,
        token_ratio: float,
    ) -> tuple[str, str]:
        """Apply threshold logic to evaluation metrics and return a verdict."""
        if quality_delta < _QUALITY_REJECT_THRESHOLD:
            return (
                "reject",
                f"Quality regression: {quality_delta:+.3f} is below reject threshold {_QUALITY_REJECT_THRESHOLD:+.3f}",
            )
        if latency_ratio > _LATENCY_REJECT_RATIO:
            return (
                "reject",
                f"Latency regression: candidate is {latency_ratio:.2f}x baseline "
                f"(max allowed {_LATENCY_REJECT_RATIO:.1f}x)",
            )
        if token_ratio > _TOKEN_REJECT_RATIO:
            return (
                "reject",
                f"Token efficiency regression: candidate uses {token_ratio:.2f}x baseline tokens "
                f"(max allowed {_TOKEN_REJECT_RATIO:.1f}x)",
            )
        if quality_delta >= _QUALITY_DEPLOY_THRESHOLD and latency_ratio <= 1.2 and token_ratio <= 1.2:
            return (
                "deploy",
                f"Quality improved by {quality_delta:+.3f} with acceptable overhead "
                f"(latency {latency_ratio:.2f}x, tokens {token_ratio:.2f}x)",
            )
        return (
            "flag_for_review",
            f"Marginal result: quality_delta={quality_delta:+.3f}, "
            f"latency_ratio={latency_ratio:.2f}, token_ratio={token_ratio:.2f}",
        )

    def _load_eval_dependencies(
        self, model_id: str, eval_tasks: list[dict[str, str]]
    ) -> tuple[Any, Any] | list[dict[str, Any]]:
        """Load scorer and adapter, returning fail-closed scores if unavailable."""
        if get_quality_scorer is None:
            logger.warning("[TrainingQualityGate] Quality scorer unavailable for model %s", model_id)
            return _dependency_unavailable_scores(eval_tasks, "quality_scorer_unavailable")
        try:
            scorer = get_quality_scorer()
        except Exception as exc:
            logger.warning("[TrainingQualityGate] Could not load quality scorer for model %s: %s", model_id, exc)
            return _dependency_unavailable_scores(eval_tasks, "quality_scorer_load_failed")
        if self._adapter_factory is None:
            logger.warning("[TrainingQualityGate] LocalInferenceAdapter unavailable for model %s", model_id)
            return _dependency_unavailable_scores(eval_tasks, "local_inference_adapter_unavailable")
        if self._adapter is None:
            try:
                self._adapter = self._adapter_factory()
            except Exception as exc:
                logger.warning(
                    "[TrainingQualityGate] Could not load LocalInferenceAdapter for model %s: %s", model_id, exc
                )
                return _dependency_unavailable_scores(eval_tasks, "local_inference_adapter_load_failed")
        return scorer, self._adapter

    @staticmethod
    def _score_eval_output(
        scorer: Any,
        task: dict[str, str],
        model_id: str,
        index: int,
        output: str,
    ) -> float:
        """Score one eval output and apply expected-answer fail-closed checks."""
        quality_score = scorer.score(
            task_id=f"gate_eval_{index}",
            model_id=model_id,
            task_type=task.get("task_type", "general"),
            task_description=task.get("prompt", ""),
            output=output,
            use_llm=False,
        )
        expected = str(task.get("expected") or "").strip()
        if expected and not _matches_expected_answer(expected, output):
            logger.warning(
                "[TrainingQualityGate] Eval task %d for model %s missed expected answer; forcing quality to 0.0",
                index,
                model_id,
            )
            return 0.0
        return quality_score.overall_score

    def _evaluate_task(
        self,
        adapter: Any,
        scorer: Any,
        model_id: str,
        task: dict[str, str],
        index: int,
    ) -> dict[str, Any]:
        """Run and score one eval task for one model."""
        prompt = task.get("prompt", "")
        start = time.monotonic()
        result = adapter.chat(model_id, "You are a helpful assistant.", prompt)
        elapsed_ms = (time.monotonic() - start) * 1000
        output = result.get("output", "")
        tokens = float(result.get("tokens_used", max(len(output) // 4, 1)))
        fallback_reason = _adapter_fallback_reason(result)
        if fallback_reason:
            logger.warning(
                "[TrainingQualityGate] Eval task %d for model %s returned fallback output (%s)",
                index,
                model_id,
                fallback_reason,
            )
            return {
                "quality": 0.0,
                "latency_ms": elapsed_ms,
                "tokens": tokens,
                "_fallback_sentinel": FALLBACK_SENTINEL,
            }
        return {
            "quality": self._score_eval_output(scorer, task, model_id, index, output),
            "latency_ms": elapsed_ms,
            "tokens": tokens,
        }

    def _evaluate_model(self, model_id: str, eval_tasks: list[dict[str, str]]) -> list[dict[str, Any]]:
        """Run all eval tasks through a single model and return per-task scores."""
        deps = self._load_eval_dependencies(model_id, eval_tasks)
        if isinstance(deps, list):
            return deps
        scorer, adapter = deps
        scores: list[dict[str, Any]] = []
        for i, task in enumerate(eval_tasks):
            try:
                scores.append(self._evaluate_task(adapter, scorer, model_id, task, i))
            except Exception as exc:
                logger.warning(
                    "[TrainingQualityGate] Eval task %d failed for model %s - "
                    "recording dependency-unavailable marker; gate will reject: %s",
                    i,
                    model_id,
                    exc,
                )
                scores.append({
                    "quality": 0.0,
                    "latency_ms": 0.0,
                    "tokens": 0.0,
                    "_dependency_unavailable": 1.0,
                    "_failure_reason": f"eval_task_{i}_exception",
                })
        return scores

    @staticmethod
    def _get_default_eval_set() -> list[dict[str, str]]:
        """Return a minimal default evaluation set covering common task types."""
        return [
            {"prompt": "Write a Python function to sort a list of integers.", "task_type": "coding", "expected": "def"},
            {"prompt": "Explain the difference between TCP and UDP.", "task_type": "documentation", "expected": "TCP"},
            {"prompt": "Analyze the time complexity of merge sort.", "task_type": "analysis", "expected": "O(n log n)"},
            {
                "prompt": "Create a plan for migrating a production database with zero downtime.",
                "task_type": "planning",
                "expected": "rollback",
            },
            {
                "prompt": (
                    "Review this code for security issues: "
                    "def login(user, pw): return db.query(f'SELECT * FROM users WHERE name={user}')"
                ),
                "task_type": "review",
                "expected": "SQL injection",
            },
        ]

    def get_history(self) -> list[dict[str, Any]]:
        """Return all gate decisions made this session, most recent first.

        Returns:
            Value produced for the caller.
        """
        with self._lock:
            return [asdict(d) for d in reversed(self._decisions)]


def get_training_quality_gate() -> TrainingQualityGate:
    """Return the singleton TrainingQualityGate instance.

    Returns:
        Value produced for the caller.
    """
    global _quality_gate
    if _quality_gate is None:
        with _quality_gate_lock:
            if _quality_gate is None:
                _quality_gate = TrainingQualityGate()
    return _quality_gate
