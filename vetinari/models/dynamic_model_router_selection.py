"""Selection and scoring mixin for :mod:`vetinari.models.dynamic_model_router`."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from vetinari.boundary_guards import account_evidence_drop
from vetinari.models.dynamic_model_router_scoring import DynamicModelRouterScoringMixin
from vetinari.models.model_router_scoring import (
    assess_difficulty,
    calculate_confidence,
    generate_reasoning,
    parse_model_size_b,
)
from vetinari.models.model_router_types import ModelInfo, ModelSelection, TaskType
from vetinari.types import ModelProvider

if TYPE_CHECKING:
    from vetinari.models.best_of_n import BestOfNSelector

logger = logging.getLogger(__name__)

__all__ = ["DynamicModelRouterSelectionMixin"]

PONDER_SCORE_UNAVAILABLE: float | None = None


class DynamicModelRouterSelectionMixin(DynamicModelRouterScoringMixin):
    """Selection, scoring, and confidence behavior for DynamicModelRouter."""

    if TYPE_CHECKING:
        _compute_awareness_confidence: Any
        _last_implicit_boost: Any
        _ponder_engine: Any
        _score_model: Any
        _selection_history: Any
        _task_defaults: Any
        max_latency_ms: Any
        max_memory_gb: Any
        models: Any

    def set_ponder_engine(self, engine: Any) -> None:
        """Inject a PonderEngine instance for scoring.

        Args:
            engine: PonderEngine instance with a ``score_models()`` method.
        """
        self._ponder_engine = engine

    def _ponder_score(self, model: ModelInfo, task_description: str) -> float | None:
        """Use PonderEngine (if available) to score a model for a task.

        Args:
            model: The model to score.
            task_description: Free-text task description passed to PonderEngine.

        Returns:
            Score in [0, 1] or None if PonderEngine is not configured.
        """
        if self._ponder_engine is None:
            return PONDER_SCORE_UNAVAILABLE

        try:
            model_dict = {
                "id": model.id,
                "name": model.name,
                "context_len": model.context_length,
                "memory_gb": model.memory_gb,
                "tags": model.capabilities.tags,
                "capabilities": model.capabilities.tags,
            }
            ranking = self._ponder_engine.score_models([model_dict], task_description, top_n=1)
            if ranking.rankings:
                return ranking.rankings[0].total_score
        except Exception as exc:
            logger.warning("PonderEngine scoring failed for %s - using internal scoring: %s", model.id, exc)

        return PONDER_SCORE_UNAVAILABLE

    @staticmethod
    def _resolve_difficulty_score(
        task_type: TaskType,
        task_description: str,
        difficulty_score: float | None,
    ) -> float | None:
        """Return caller-supplied or heuristic task difficulty."""
        if difficulty_score is not None or not task_description:
            return difficulty_score
        calibration = 0.0
        try:
            from vetinari.learning.difficulty_feedback import get_calibration_bias

            calibration = get_calibration_bias(task_type.value)
        except Exception:
            logger.info(
                "Calibration bias unavailable for %s - using uncalibrated difficulty heuristic",
                task_type.value,
            )
        return assess_difficulty(task_description, task_type.value, calibration_bias=calibration)

    def _available_selection_vram_gb(self) -> float:
        """Return the VRAM budget used for candidate filtering."""
        try:
            from vetinari.models.vram_manager import get_vram_manager

            available_vram_gb = min(get_vram_manager().get_free_vram_gb(), self.max_memory_gb)
            logger.debug("VRAM-aware routing: %.1f GB available", available_vram_gb)
            return available_vram_gb
        except Exception:
            logger.warning("VRAMManager unavailable, using max_memory_gb=%s", self.max_memory_gb)
            return self.max_memory_gb

    def _candidate_models(
        self,
        required_capabilities: list[str] | None,
        context_length_needed: int | None,
    ) -> list[ModelInfo]:
        """Return available models that meet hard routing constraints."""
        available_vram_gb = self._available_selection_vram_gb()
        config = getattr(self, "_model_config", {}) or {}
        cpu_offload = config.get("local_inference", {}).get("cpu_offload_enabled", True)
        return [
            model
            for model in self.models.values()
            if self._model_passes_selection_filters(
                model,
                available_vram_gb=available_vram_gb,
                cpu_offload=cpu_offload,
                required_capabilities=required_capabilities,
                context_length_needed=context_length_needed,
            )
        ]

    def _model_passes_selection_filters(
        self,
        model: ModelInfo,
        *,
        available_vram_gb: float,
        cpu_offload: bool,
        required_capabilities: list[str] | None,
        context_length_needed: int | None,
    ) -> bool:
        """Check hard constraints before a model is scored."""
        if not model.is_available:
            return False
        if model.memory_gb > available_vram_gb:
            can_offload = model.provider == ModelProvider.LOCAL and cpu_offload
            if not can_offload:
                return False
        if model.avg_latency_ms > self.max_latency_ms and model.avg_latency_ms > 0:
            return False
        if context_length_needed and model.context_length < context_length_needed:
            return False
        return self._model_has_required_capabilities(model, required_capabilities)

    @staticmethod
    def _model_has_required_capabilities(model: ModelInfo, required_capabilities: list[str] | None) -> bool:
        """Return whether a model satisfies explicit capability requirements."""
        if not required_capabilities:
            return True
        caps = model.capabilities
        return not any(
            (req == "code_gen" and not caps.code_gen)
            or (req == "reasoning" and not caps.reasoning)
            or (req == "docs" and not caps.docs)
            for req in required_capabilities
        )

    def _preferred_model_ids(self, task_type: TaskType, preferred_models: list[str] | None) -> list[str]:
        """Merge caller preferences with configured task defaults."""
        task_key = task_type.value.lower()
        boosted_preferred = list(preferred_models or [])
        default_id = self._task_defaults.get(task_key, "")
        if default_id and default_id not in boosted_preferred:
            boosted_preferred.append(default_id)
        return boosted_preferred

    def _score_candidates(
        self,
        candidates: list[ModelInfo],
        task_type: TaskType,
        task_description: str,
        preferred_models: list[str],
        implicit_boosts: dict[str, dict[str, Any]],
    ) -> list[tuple[ModelInfo, float]]:
        """Score candidate models before global selection adjustments."""
        return [
            (model, self._score_model(model, task_type, task_description, preferred_models, implicit_boosts))
            for model in candidates
        ]

    def _apply_selection_adjustments(
        self,
        scored: list[tuple[ModelInfo, float]],
        *,
        task_type: TaskType,
        difficulty_score: float | None,
        agent_role: str,
    ) -> list[tuple[ModelInfo, float]]:
        """Apply cost, difficulty, and role-specific score adjustments."""
        adjusted = self._apply_cost_bonus(scored, task_type)
        if difficulty_score is not None and difficulty_score > 0.7:
            adjusted = [
                (
                    model,
                    score
                    + (
                        0.08
                        if parse_model_size_b(model.id) >= 30
                        else 0.04
                        if parse_model_size_b(model.id) >= 14
                        else 0
                    ),
                )
                for model, score in adjusted
            ]
        if agent_role == "inspector":
            adjusted = [
                (model, score + 0.06) if model.capabilities.reasoning else (model, score) for model, score in adjusted
            ]
        adjusted.sort(key=lambda item: item[1], reverse=True)
        return adjusted

    @staticmethod
    def _apply_cost_bonus(
        scored: list[tuple[ModelInfo, float]],
        task_type: TaskType,
    ) -> list[tuple[ModelInfo, float]]:
        """Boost the cheapest adequate model when cost optimization is available."""
        try:
            from vetinari.learning.cost_optimizer import get_cost_optimizer

            cheapest = get_cost_optimizer().select_cheapest_adequate(
                task_type=task_type.value,
                models=[model.id for model, _score in scored],
                min_quality=0.6,
            )
        except Exception:
            logger.warning("CostOptimizer unavailable for scoring bonus")
            return scored
        if not cheapest:
            return scored
        return [(model, score + 0.05) if model.id == cheapest else (model, score) for model, score in scored]

    @staticmethod
    def _selection_context(
        task_type: TaskType,
        best_score: float,
        confidence: float,
        confidence_result: Any,
    ) -> dict[str, Any]:
        """Build audit context for a model selection decision."""
        return {
            "task_type": task_type.value,
            "score": round(best_score, 3),
            "confidence": round(confidence, 3),
            "confidence_level": confidence_result.level.value if confidence_result else "unknown",
        }

    def _log_model_selection_decision(
        self,
        best_model: ModelInfo,
        task_type: TaskType,
        best_score: float,
        confidence: float,
        confidence_result: Any,
        reasoning_text: str,
        alternatives: list[ModelInfo],
    ) -> None:
        """Best-effort audit logging for a model selection decision."""
        try:
            from vetinari.audit import get_audit_logger

            get_audit_logger().log_decision(
                decision_type="model_selection",
                choice=best_model.id,
                reasoning=reasoning_text,
                alternatives=[model.id for model in alternatives],
                context=self._selection_context(task_type, best_score, confidence, confidence_result),
            )
        except Exception:
            logger.warning("Audit logging failed during model selection", exc_info=True)

    def _record_selection_history(self, task_type: TaskType, best_model: ModelInfo, best_score: float) -> None:
        """Append the selected model and implicit boost evidence to history."""
        boost_entry = self._last_implicit_boost.get(best_model.id, {"boost": 0.0, "source": None})
        self._selection_history.append({
            "task_type": task_type.value,
            "selected_model": best_model.id,
            "score": best_score,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "implicit_boost": boost_entry["boost"],
            "implicit_boost_source": boost_entry["source"],
        })

    def _record_fallback_selection_history(
        self, task_type: TaskType, model: ModelInfo, score: float, reason: str
    ) -> None:
        selection_record = {
            "task_type": task_type.value,
            "selected_model": model.id,
            "score": score,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "fallback": True,
        }
        account_evidence_drop(selection_record, "router_audit_log", logger=logger)
        self._selection_history.append(selection_record)

    def select_model(
        self,
        task_type: TaskType,
        task_description: str = "",
        required_capabilities: list[str] | None = None,
        preferred_models: list[str] | None = None,
        context_length_needed: int | None = None,
        difficulty_score: float | None = None,
        agent_role: str = "",
    ) -> ModelSelection:
        """Select the best model for a given task.

        Args:
            task_type: Type of task to perform.
            task_description: Description of the task.
            required_capabilities: List of required capabilities.
            preferred_models: List of preferred model IDs (in order).
            context_length_needed: Required context length.
            difficulty_score: Optional pre-computed difficulty (0.0-1.0).
            agent_role: Agent role hint (``"inspector"`` prefers reasoning models).

        Returns:
            ModelSelection with chosen model and reasoning.

        Raises:
            RuntimeError: If no registered model can satisfy the requested
                capabilities or context window.
        """
        implicit_boosts: dict[str, dict[str, Any]] = {}
        difficulty_score = self._resolve_difficulty_score(task_type, task_description, difficulty_score)
        candidates = self._candidate_models(required_capabilities, context_length_needed)

        if not candidates:
            if required_capabilities or context_length_needed:
                raise RuntimeError(
                    "ModelSelection unavailable: no registered model satisfies required "
                    "capabilities/context length constraints"
                )
            return self._fallback_selection(task_type)

        boosted_preferred = self._preferred_model_ids(task_type, preferred_models)
        scored = self._score_candidates(candidates, task_type, task_description, boosted_preferred, implicit_boosts)
        self._last_implicit_boost = implicit_boosts
        scored = self._apply_selection_adjustments(
            scored,
            task_type=task_type,
            difficulty_score=difficulty_score,
            agent_role=agent_role,
        )

        best_model, best_score = scored[0]
        alternatives = [m for m, _s in scored[1:4]]
        confidence = calculate_confidence(scored)
        reasoning_text = generate_reasoning(best_model, task_type, best_score)

        # Multi-signal confidence computation (Session 11 - awareness layer)
        confidence_result, unknown_situations = self._compute_awareness_confidence(
            best_model,
            task_type,
            confidence,
        )
        if confidence_result is not None:
            confidence = confidence_result.score

        self._log_model_selection_decision(
            best_model,
            task_type,
            best_score,
            confidence,
            confidence_result,
            reasoning_text,
            alternatives,
        )
        self._record_selection_history(task_type, best_model, best_score)

        return ModelSelection(
            model=best_model,
            score=best_score,
            reasoning=reasoning_text,
            alternatives=alternatives,
            confidence=confidence,
            confidence_result=confidence_result,
            unknown_situations=unknown_situations,
        )

    def _fallback_selection(self, task_type: TaskType) -> ModelSelection:
        """Return a fallback model selection when no candidates pass filters.

        Tries the task-type default first, then any available model at random.

        Args:
            task_type: The requested task type (used for default lookup).

        Returns:
            A low-confidence ModelSelection.
        """
        task_key = task_type.value.lower()
        default_id = self._task_defaults.get(task_key, "")
        if default_id and default_id in self.models:
            default_model = self.models[default_id]
            if default_model.is_available:
                logger.info("Using task default model %s for %s", default_id, task_key)
                fallback_cr, fallback_protocols = self._compute_awareness_confidence(
                    default_model,
                    task_type,
                    0.5,
                )
                self._record_fallback_selection_history(
                    task_type,
                    default_model,
                    0.5,
                    f"Task default: {default_id} configured for {task_key} tasks",
                )
                return ModelSelection(
                    model=default_model,
                    score=0.5,
                    reasoning=f"Task default: {default_id} configured for {task_key} tasks",
                    confidence=fallback_cr.score if fallback_cr else 0.5,
                    confidence_result=fallback_cr,
                    unknown_situations=fallback_protocols,
                )

        available = [m for m in self.models.values() if m.is_available]
        if not available:
            logger.warning("No models available - model selection cannot proceed")
            raise RuntimeError("ModelSelection unavailable: no models registered in router")

        fallback = secrets.choice(available)
        self._record_fallback_selection_history(task_type, fallback, 0.0, "Fallback: no models matched criteria")
        return ModelSelection(
            model=fallback,
            score=0.0,
            reasoning="Fallback: no models matched criteria",
            confidence=0.1,
        )

    def get_best_of_n_selector(
        self,
        generate_fn: Callable[[str], str],
    ) -> BestOfNSelector:
        """Return a BestOfNSelector wired to the provided generation function.

        Creates a new ``BestOfNSelector`` on each call so the caller can supply
        a fresh ``generate_fn`` that captures the current model and context.
        The selector is intentionally stateless (its only state is the callable),
        so there is no benefit to caching it on the router.

        Args:
            generate_fn: Callable that accepts a prompt string and returns a
                single candidate string.  Called N times per selection request.

        Returns:
            BestOfNSelector instance ready for use.
        """
        from vetinari.models.best_of_n import BestOfNSelector

        return BestOfNSelector(generate_fn=generate_fn)
