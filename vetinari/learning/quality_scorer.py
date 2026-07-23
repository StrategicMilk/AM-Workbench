"""Quality Scorer - Vetinari Self-Improvement Subsystem.

Evaluates the quality of task outputs using LLM-as-judge and heuristics.
Produces structured quality scores that feed the feedback loop.

Enhanced in Wave 4:
- SQLite persistence: scores survive restarts
- Improved LLM-as-judge: uses a DIFFERENT model from the one being evaluated
- Self-rationalization: judge generates reasoning before scoring
- Per-task-type rubrics with calibrated dimensions
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.adapters.adapter_cache import get_local_inference_adapter
from vetinari.boundary_guards import account_evidence_drop, require_score_in_range
from vetinari.constants import _PROJECT_ROOT, TRUNCATE_OUTPUT_PREVIEW
from vetinari.learning.quality_scorer_heuristics import _score_heuristic_output
from vetinari.learning.quality_scorer_signal import QualityScorerSignalMixin
from vetinari.learning.quality_scorer_storage import QualityScorerStorageMixin
from vetinari.learning.quality_scorer_tracking import QualityScorerTrackingMixin
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)

_FALLBACK_PATTERNS = frozenset({
    "",
    "{}",
    '{"content":"","sections":[]}',
    '{"content": "", "sections": []}',
})


def _clamp_score(value: Any) -> float:
    """Return a numeric score only when it is already in the valid range."""
    return require_score_in_range(value, "quality_scorer")


@dataclass
class QualityScore:
    """Structured quality assessment for a task output.

    A score of 0.0 with method="unmeasured" means "we have no data for this
    dimension" — distinct from a measured 0.0 which means "genuinely terrible".
    Check ``measured_dimensions`` to know which scores are backed by evidence.
    """

    task_id: str
    model_id: str
    task_type: str
    overall_score: float  # 0.0 - 1.0
    correctness: float = 0.0  # 0.0 = unmeasured by default, not "bad"
    completeness: float = 0.0
    efficiency: float = 0.0
    style: float = 0.0
    dimensions: dict[str, float] = field(default_factory=dict)
    measured_dimensions: list[str] = field(default_factory=list)  # Which dimensions have real data
    issues: list[str] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    method: str = "unmeasured"  # "llm" | "heuristic" | "hybrid" | "unmeasured" | "rejected"

    def __post_init__(self) -> None:
        """Reject impossible score values instead of silently clamping them."""
        self.overall_score = require_score_in_range(
            self.overall_score,
            "quality_score.overall_score",
            field_name="overall_score",
        )
        self.correctness = require_score_in_range(
            self.correctness,
            "quality_score.correctness",
            field_name="correctness",
        )
        self.completeness = require_score_in_range(
            self.completeness,
            "quality_score.completeness",
            field_name="completeness",
        )
        self.efficiency = require_score_in_range(
            self.efficiency,
            "quality_score.efficiency",
            field_name="efficiency",
        )
        self.style = require_score_in_range(self.style, "quality_score.style", field_name="style")
        self.dimensions = {
            str(key): require_score_in_range(value, f"quality_score.dimensions.{key}", field_name=str(key))
            for key, value in self.dimensions.items()
        }

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"QualityScore(task_id={self.task_id!r}, model_id={self.model_id!r},"
            f" task_type={self.task_type!r}, overall_score={self.overall_score!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)


class QualityScorer(QualityScorerSignalMixin, QualityScorerStorageMixin, QualityScorerTrackingMixin):
    """Evaluates output quality using LLM-as-judge + heuristics.

    Provides structured quality signals that feed back into model
    selection, prompt evolution, and workflow strategy learning.
    """

    # Per-task-type quality dimensions
    DIMENSIONS = {
        "coding": ["correctness", "completeness", "efficiency", "style", "test_coverage"],
        "research": ["accuracy", "completeness", "source_quality", "actionability"],
        "analysis": ["depth", "accuracy", "actionability", "clarity"],
        "documentation": ["clarity", "completeness", "accuracy", "examples"],
        "testing": ["coverage", "correctness", "clarity", "edge_cases"],
        "default": ["correctness", "completeness", "quality"],
    }

    # Flat-score detection: if last N scores within this range, force calibration
    _FLAT_SCORE_WINDOW = 5  # Number of recent scores to check
    _FLAT_SCORE_THRESHOLD = 0.05  # Max range to consider "flat"

    # Variance monitoring: warn if variance below threshold over N+ scores
    _VARIANCE_WARN_MIN_SCORES = 20  # Minimum scores before checking variance
    _VARIANCE_WARN_THRESHOLD = 0.001  # Variance below this triggers warning/rejection

    def __init__(self, adapter_manager=None):
        self._adapter_manager = adapter_manager
        self._scores: deque[QualityScore] = deque(maxlen=1000)
        self._score_count: int = 0

        # Per model+task_type score history for flat-detection and variance monitoring
        self._score_history: dict[tuple[str, str], deque[float]] = {}

        self._calibration_interval: int = 10
        self._baselines: dict[str, dict[str, float]] = {}
        self._judge_adapters: dict[str, Any] = {}
        self._judge_adapter_lock = threading.Lock()
        try:
            import yaml

            config_path = _PROJECT_ROOT / "config" / "ml_config.yaml"
            with Path(config_path).open(encoding="utf-8") as f:
                ml_config = yaml.safe_load(f)
            self._calibration_interval = ml_config.get("quality_scoring", {}).get("calibration_interval", 10)
        except Exception:
            logger.warning("Could not load ml_config.yaml — using default calibration interval of 10")

        try:
            import yaml

            baselines_path = _PROJECT_ROOT / "config" / "quality_baselines.yaml"
            with Path(baselines_path).open(encoding="utf-8") as f:
                self._baselines = yaml.safe_load(f) or {}
        except Exception:
            logger.warning("Could not load quality_baselines.yaml — using conservative 0.45 defaults")

    def score(
        self,
        task_id: str,
        model_id: str,
        task_type: str,
        task_description: str,
        output: Any,
        use_llm: bool = True,
        inference_confidence: float | None = None,
        temperature_used: float | None = None,
    ) -> QualityScore:
        """Score a task output.

        Args:
            task_id: Unique task identifier.
            model_id: Model that produced the output.
            task_type: Type of task (coding, research, etc.).
            task_description: What the task asked for.
            output: The output to evaluate.
            use_llm: Whether to attempt LLM-as-judge evaluation.
            inference_confidence: Optional 0.0-1.0 confidence from logprob
                variance analysis. Low values penalize heuristic scores.
            temperature_used: The actual temperature used during inference.
                Passed to Thompson strategy feedback so the bandit learns
                which temperatures produce better outputs. None if unknown.

        Returns:
            QualityScore with all dimensions populated.
        """
        output, fallback_reason = _normalize_output_and_fallback_reason(output)
        if fallback_reason:
            logger.warning(
                "[QualityScorer] Rejected fallback output for task %s (%s) - not scoring",
                task_id,
                fallback_reason,
            )
            return self._rejected_score(task_id, model_id, task_type, f"Rejected: {fallback_reason}")
        if not output or output.strip() in _FALLBACK_PATTERNS:
            logger.warning(
                "[QualityScorer] Rejected fallback/empty output for task %s - not scoring",
                task_id,
            )
            return self._rejected_score(task_id, model_id, task_type, "Rejected: fallback or empty output")

        dims = self.DIMENSIONS.get(task_type.lower(), self.DIMENSIONS["default"])
        self._score_count += 1
        llm_score = self._calibrated_llm_score(
            task_id,
            model_id,
            task_type,
            task_description,
            output,
            dims,
            use_llm=use_llm,
            inference_confidence=inference_confidence,
        )
        if llm_score is not None:
            self._store_score(llm_score)
            return llm_score

        score = self._score_heuristic(task_id, model_id, task_type, output, dims, inference_confidence)
        self._apply_distribution_gate(model_id, task_type, score)
        self._store_score(score)
        self._update_thompson_temperature(task_type, score.overall_score, temperature_used)
        return score

    @staticmethod
    def _rejected_score(task_id: str, model_id: str, task_type: str, issue: str) -> QualityScore:
        """Build a fail-closed rejected quality score."""
        return QualityScore(
            task_id=task_id,
            model_id=model_id,
            task_type=task_type,
            overall_score=0.0,
            correctness=0.0,
            completeness=0.0,
            efficiency=0.0,
            style=0.0,
            measured_dimensions=["correctness", "completeness", "efficiency", "style"],
            issues=[issue],
            method="rejected",
        )

    def _calibrated_llm_score(
        self,
        task_id: str,
        model_id: str,
        task_type: str,
        task_description: str,
        output: str,
        dims: list[str],
        *,
        use_llm: bool,
        inference_confidence: float | None,
    ) -> QualityScore | None:
        """Return an LLM calibration score when calibration is due and succeeds."""
        is_flat_forced = self._is_score_distribution_flat(model_id, task_type)
        is_periodic = self._score_count % self._calibration_interval == 0
        if not (use_llm and self._adapter_manager and (is_periodic or is_flat_forced)):
            return None
        if is_flat_forced:
            logger.info(
                "[QualityScorer] Forcing LLM calibration for %s/%s - last %d heuristic scores within %.2f range",
                model_id,
                task_type,
                self._FLAT_SCORE_WINDOW,
                self._FLAT_SCORE_THRESHOLD,
            )
        llm_score = self._score_with_llm(task_id, model_id, task_type, task_description, output, dims)
        if llm_score is None:
            return None
        heuristic_score = self._score_heuristic(task_id, model_id, task_type, output, dims, inference_confidence)
        delta = round(llm_score.overall_score - heuristic_score.overall_score, 3)
        logger.debug(
            "[QualityScorer] Calibration run: LLM=%.2f, heuristic=%.2f, delta=%.2f",
            llm_score.overall_score,
            heuristic_score.overall_score,
            delta,
        )
        return llm_score

    def _apply_distribution_gate(self, model_id: str, task_type: str, score: QualityScore) -> None:
        """Reject heuristic scores when recent score distribution is too flat."""
        self._record_score_history(model_id, task_type, score.overall_score)
        distribution_issue = self._check_score_distribution(model_id, task_type)
        if distribution_issue:
            score.overall_score = 0.0
            score.issues.append(distribution_issue)
            score.method = "rejected"

    def _store_score(self, score: QualityScore) -> None:
        """Append and persist one quality score."""
        self._scores.append(score)
        try:
            self._persist(score)
        except Exception:
            account_evidence_drop(score, "quality_scorer", logger=logger)
            raise

    def _score_with_llm(
        self,
        task_id: str,
        model_id: str,
        task_type: str,
        task_description: str,
        output: str,
        dims: list[str],
    ) -> QualityScore | None:
        """Use LLM-as-judge with self-rationalization to score the output.

        The judge model is deliberately chosen to be DIFFERENT from model_id
        to avoid self-evaluation bias. Uses LocalInferenceAdapter to call
        the local model rather than the adapter manager to ensure independence.

        Args:
            task_id: Unique task identifier.
            model_id: Model that produced the output (will be avoided for judging).
            task_type: Type of task being scored.
            task_description: What the task asked for.
            output: The output to evaluate.
            dims: List of dimension names to score.

        Returns:
            QualityScore with LLM-assigned scores, or None if scoring fails.
        """
        try:
            prompt = self._build_judge_prompt(task_type, task_description, output, dims)

            # Prefer a different, fast local model for judging.
            # If only one model is loaded, self-evaluation is unreliable —
            # fall back to heuristic scoring to avoid bias.
            judge_model = self._pick_judge_model(model_id)
            if judge_model == model_id:
                logger.warning(
                    "Only one model loaded (%s) — using heuristic scoring instead of self-evaluation to avoid bias",
                    model_id,
                )
                return None  # Caller falls back to heuristic scoring

            adapter = self._get_judge_adapter(judge_model)
            result = adapter.chat(
                judge_model,
                "You are an objective quality evaluator. Score honestly and precisely.",
                prompt,
            )
            text = result.get("output", "").strip()
            return self._quality_score_from_llm_text(task_id, model_id, task_type, text)
        except Exception as e:
            logger.warning("LLM quality scoring failed — quality tracking degraded: %s", e)
            return None

    @staticmethod
    def _build_judge_prompt(task_type: str, task_description: str, output: str, dims: list[str]) -> str:
        """Build the JSON-only judge prompt with untrusted content delimiters."""
        from vetinari.safety.prompt_sanitizer import _UNTRUSTED_CLOSE, _UNTRUSTED_OPEN

        dims_list = ", ".join(dims)
        dims_json_template = ", ".join(f'"{dimension}": 0.0' for dimension in dims)
        return (
            f"You are an objective quality evaluator assessing a {task_type} output.\n\n"
            f"TASK:\n{_UNTRUSTED_OPEN}\n{task_description[:400]}\n{_UNTRUSTED_CLOSE}\n\n"
            f"OUTPUT:\n{_UNTRUSTED_OPEN}\n{output[:TRUNCATE_OUTPUT_PREVIEW]}\n{_UNTRUSTED_CLOSE}\n\n"
            "Step 1 - REASONING: Briefly analyse the output strengths and weaknesses "
            f"for each dimension: {dims_list}\n\n"
            "Step 2 - SCORES: Now score each dimension 0.0-1.0 based on your reasoning.\n\n"
            "Respond ONLY with valid JSON:\n"
            '{\n  "reasoning": "your analysis here",\n'
            '  "overall": 0.0,\n'
            f'  "dimensions": {{{dims_json_template}}},\n'
            '  "issues": ["..."],\n'
            '  "confidence": 0.0\n}'
        )

    @staticmethod
    def _quality_score_from_llm_text(
        task_id: str,
        model_id: str,
        task_type: str,
        text: str,
    ) -> QualityScore | None:
        """Parse the judge JSON response into a QualityScore."""
        if not text:
            return None
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        dim_scores = data.get("dimensions", {})
        raw_overall = data.get("overall")
        if raw_overall is not None:
            overall = require_score_in_range(raw_overall, "quality_scorer.llm_overall", field_name="overall")
        elif dim_scores:
            overall = sum(_clamp_score(value) for value in dim_scores.values()) / len(dim_scores)
        else:
            overall = 0.0
        clamped_dims = {str(key): _clamp_score(value) for key, value in dim_scores.items()}
        return QualityScore(
            task_id=task_id,
            model_id=model_id,
            task_type=task_type,
            overall_score=round(overall, 3),
            correctness=clamped_dims.get("correctness", 0.0),
            completeness=clamped_dims.get("completeness", 0.0),
            efficiency=clamped_dims.get("efficiency", 0.0),
            style=clamped_dims.get("style", 0.0),
            dimensions=clamped_dims,
            measured_dimensions=list(clamped_dims.keys()),
            issues=data.get("issues", []),
            method="llm",
        )

    def _get_judge_adapter(self, judge_model: str) -> Any:
        """Return a cached judge adapter for calibration scoring.

        Args:
            judge_model: Model identifier selected for judging.

        Returns:
            Cached adapter object used to call the judge model.
        """
        with self._judge_adapter_lock:
            adapter = self._judge_adapters.get(judge_model)
            if adapter is None:
                adapter = get_local_inference_adapter(judge_model)
                self._judge_adapters[judge_model] = adapter
            return adapter

    @staticmethod
    def _pick_judge_model(evaluated_model_id: str) -> str:
        """Pick a judge model that is DIFFERENT from the model being evaluated."""
        try:
            from vetinari.models.model_registry import get_model_registry

            loaded = get_model_registry().get_loaded_local_models()
            for m in loaded:
                if m.model_id != evaluated_model_id:
                    return m.model_id
        except Exception:
            logger.warning("Failed to pick judge model different from %s", evaluated_model_id, exc_info=True)
        # Fallback: just use whatever is loaded (slight bias, but better than nothing)
        return evaluated_model_id

    def _score_heuristic(
        self,
        task_id: str,
        model_id: str,
        task_type: str,
        output: str,
        dims: list[str],
        inference_confidence: float | None = None,
    ) -> QualityScore:
        """Heuristic quality scoring with structural checks per task type.

        Args:
            task_id: Unique task identifier.
            model_id: Model that produced the output.
            task_type: Type of task (coding, research, etc.).
            output: The output to evaluate.
            dims: List of dimension names to score.
            inference_confidence: Optional confidence from logprob variance (0.0-1.0).

        Returns:
            QualityScore with heuristic dimensions populated.
        """
        return _score_heuristic_output(
            task_id=task_id,
            model_id=model_id,
            task_type=task_type,
            output=output,
            dims=dims,
            inference_confidence=inference_confidence,
            baseline_config=self._baselines,
            score_factory=QualityScore,
        )


def _normalize_output_and_fallback_reason(output: Any) -> tuple[str, str]:
    """Return scoreable text and a reason when producer metadata says fallback."""
    if isinstance(output, dict):
        metadata = output.get("metadata") if isinstance(output.get("metadata"), dict) else {}
        for flag in ("_is_fallback", "is_fallback"):
            if output.get(flag) is True or metadata.get(flag) is True:
                return "", flag
        status = str(
            output.get("fallback_status")
            or metadata.get("fallback_status")
            or output.get("status")
            or metadata.get("status")
            or ""
        ).lower()
        if status in {"fallback", "fallback_used", "degraded", "error", "failed", "failure"}:
            return "", f"status={status}"
        if output.get("error") or metadata.get("error"):
            return "", "error"
        text = output.get("output", output.get("result", output.get("content", "")))
        if text:
            return str(text), ""
        return json.dumps(output, default=str, sort_keys=True), ""
    for flag in ("_is_fallback", "is_fallback"):
        if getattr(output, flag, False) is True:
            return "", flag
    status = str(getattr(output, "fallback_status", "") or getattr(output, "status", "") or "").lower()
    if status in {"fallback", "fallback_used", "degraded", "error", "failed", "failure"}:
        return "", f"status={status}"
    if getattr(output, "error", None):
        return "", "error"
    return str(output) if output is not None else "", ""


# Singleton
_quality_scorer: QualityScorer | None = None
_quality_scorer_lock = threading.Lock()


def get_quality_scorer() -> QualityScorer:
    """Return the singleton QualityScorer instance (thread-safe).

    Returns:
        The shared QualityScorer instance.
    """
    global _quality_scorer
    if _quality_scorer is None:
        with _quality_scorer_lock:
            if _quality_scorer is None:
                _quality_scorer = QualityScorer()
    return _quality_scorer
