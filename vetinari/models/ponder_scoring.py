"""Scoring engine and result records for ponder model ranking."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from vetinari.knowledge import get_benchmark_info

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_timestamp() -> str:
    return _utc_now().isoformat()


POLICY_SENSITIVE_KEYWORDS = [
    "harmful",
    "illegal",
    "attack",
    "exploit",
    "weapon",
    "bypass",
    " jailbreak",
    "darkweb",
    "malware",
    "phishing",
    "fraud",
    "scam",
    "explicit",
    "violence",
    "hate",
    "discriminat",
    "terroris",
]


@dataclass(frozen=True, slots=True)
class ModelScore:
    """Model score."""

    model_id: str
    model_name: str
    total_score: float
    capability_score: float
    context_score: float
    memory_score: float
    heuristic_score: float
    policy_penalty: float
    reasoning: str = ""

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"ModelScore(model_id={self.model_id!r},"
            f" total_score={self.total_score!r},"
            f" policy_penalty={self.policy_penalty!r})"
        )


@dataclass
class PonderRanking:
    """Ponder ranking."""

    task_id: str
    task_description: str
    rankings: list[ModelScore]
    timestamp: str = field(default_factory=_utc_timestamp)
    phase: str = "unknown"

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return f"PonderRanking(task_id={self.task_id!r}, phase={self.phase!r}, rankings={len(self.rankings)!r})"


class PonderEngine:
    """Ponder engine."""

    def __init__(self, *, utc_now: Callable[[], datetime] | None = None) -> None:
        self.weights = {"capability": 0.40, "context": 0.20, "memory": 0.20, "heuristic": 0.20}
        self.policy_penalty = -1.0
        self._utc_now = utc_now or _utc_now

    @staticmethod
    def _get_task_capability_requirements(task_description: str) -> dict[str, Any]:
        # Safely handle None task descriptions
        if task_description is None:
            task_description = ""
        task_lower = task_description.lower()

        requirements = {
            "reasoning": 0.5,
            "code": 0.5,
            "creative": 0.3,
            "analysis": 0.5,
            "instruction_following": 0.7,
            "context_needed": 4096,
            "policy_sensitive": False,
        }

        if any(kw in task_lower for kw in ["reason", "think", "analyze", "evaluate", "assess"]):
            requirements["reasoning"] = 0.9
            requirements["analysis"] = 0.9

        if any(kw in task_lower for kw in ["code", "implement", "build", "write", "create", "function"]):
            requirements["code"] = 0.9

        if any(kw in task_lower for kw in ["write", "story", "creative", "compose", "generate"]):
            requirements["creative"] = 0.8

        if any(kw in task_lower for kw in ["search", "find", "lookup", "research"]):
            requirements["context_needed"] = 8192

        if any(kw in task_lower for kw in POLICY_SENSITIVE_KEYWORDS):
            requirements["policy_sensitive"] = True

        return requirements

    @staticmethod
    def _calculate_capability_score(model: dict, requirements: dict) -> float:
        """Score a model's capability fit for the given task requirements.

        Uses benchmark data from benchmarks.yaml when the model has stored
        benchmark_scores — each score is weighted by its relevance to the
        dominant task type.  Falls back to tag-based keyword matching for
        models without benchmark data (cold-start).

        Args:
            model: Model dict; may include a ``benchmark_scores`` key mapping
                benchmark IDs to normalised scores (0-1).
            requirements: Task requirement dict from
                ``_get_task_capability_requirements()``.

        Returns:
            Capability score in [0, 1].
        """
        base_score = 0.5

        tags = [t.lower() for t in model.get("tags", [])]

        # Use .get() with defaults to avoid KeyError
        reasoning = requirements.get("reasoning", 0)
        code = requirements.get("code", 0)
        creative = requirements.get("creative", 0)
        analysis = requirements.get("analysis", 0)

        # Determine the dominant task type for benchmark weighting
        task_scores = {
            "coding": code,
            "reasoning": reasoning,
            "creative": creative,
            "analysis": analysis,
        }
        dominant_task = max(task_scores, key=lambda k: task_scores[k])
        dominant_requirement = task_scores[dominant_task]

        # Data-driven path: use stored benchmark scores weighted by task relevance.
        # Only applies when the model has benchmark data and the task is demanding.
        benchmark_scores: dict[str, float] = model.get("benchmark_scores", {})
        if benchmark_scores and dominant_requirement > 0.7:
            weighted_sum = 0.0
            weight_total = 0.0
            for benchmark_id, score in benchmark_scores.items():
                info = get_benchmark_info(benchmark_id)
                if not info:
                    continue
                task_weight = info.get("weight_for_task", {}).get(dominant_task, 0.0)
                if task_weight > 0:
                    weighted_sum += float(score) * task_weight
                    weight_total += task_weight
            if weight_total > 0:
                # Weighted mean benchmark score replaces tag-based estimate
                return max(0.0, min(1.0, weighted_sum / weight_total))

        # Tag-based fallback — used when no benchmark data is available
        if reasoning > 0.7:
            if any(t in tags for t in ["reasoning", "reason", "think", "advanced"]):
                base_score += 0.3
            elif "coder" in tags or "code" in tags:
                base_score += 0.1

        if code > 0.7:
            if "coder" in tags or "code" in tags:
                base_score += 0.4
            elif any(t in tags for t in ["programming", "dev"]):
                base_score += 0.3

        if creative > 0.7 and any(t in tags for t in ["creative", "writing", "story"]):
            base_score += 0.3

        if analysis > 0.7 and any(t in tags for t in ["analysis", "analyze", "research"]):
            base_score += 0.3

        return min(1.0, base_score)

    @staticmethod
    def _calculate_context_score(model: dict, requirements: dict) -> float:
        # Canonicalize context length across providers (context_len vs context_length)
        ctx_len = model.get("context_len", model.get("context_length", 8192))
        needed = requirements.get("context_needed", 8192)

        if needed <= 8192:
            return 1.0 if ctx_len >= 8192 else 0.7
        elif needed <= 32768:
            return 1.0 if ctx_len >= 32768 else 0.6
        elif needed <= 65536:
            return 1.0 if ctx_len >= 65536 else 0.5
        else:
            return 1.0 if ctx_len >= 131072 else 0.4

    @staticmethod
    def _calculate_memory_score(model: dict) -> float:
        quantization = model.get("quantization", "unknown")

        if quantization in ["q4_k_m", "q5_k_s", "q5_k_m", "q6_k"]:
            return 1.0
        elif quantization in ["q4_0", "q4_1", "q4_2", "q4_3"]:
            return 0.9
        elif quantization in ["q8_0", "q8_1"]:
            return 0.7
        elif quantization in ["f16", "f32"]:
            return 0.5
        else:
            return 0.7

    @staticmethod
    def _get_thompson_score(model_id: str, task_type: str = "general") -> float | None:
        """Return the Thompson arm mean for a model+task_type pair if well-observed.

        Only used when the arm has at least 10 pulls so the posterior is
        meaningful.  Returns None for cold-start models — callers fall back
        to keyword heuristics.

        Args:
            model_id: Model identifier string.
            task_type: Task type to look up (e.g., "coding", "general").

        Returns:
            Float in [0, 1] representing arm mean, or None if cold-start.
        """
        _MIN_THOMPSON_PULLS = 10
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            selector = get_thompson_selector()
            arm_key = f"{model_id}:{task_type}"
            with selector._lock:
                arm = selector._arms.get(arm_key)
                if arm is not None and arm.total_pulls >= _MIN_THOMPSON_PULLS:
                    return arm.mean
        except Exception:
            logger.warning(
                "Thompson selector unavailable for model %s — falling back to heuristic memory score",
                model_id,
                exc_info=True,
            )
        return None

    @staticmethod
    def _calculate_heuristic_score(model: dict, task_description: str) -> float:
        base_score = 0.6
        # Safely handle None task descriptions
        if task_description is None:
            task_description = ""
        task_lower = task_description.lower()
        model_name = model.get("id", "").lower()

        if "task" in task_lower and "small" in model_name:
            base_score += 0.2
        elif "complex" in task_lower or "difficult" in task_lower:
            if "70b" in model_name or "72b" in model_name:
                base_score += 0.3
            elif "34b" in model_name or "32b" in model_name:
                base_score += 0.2

        if any(t in model_name for t in ["fast", "speed", "turbo"]):
            base_score += 0.15

        return min(1.0, base_score)

    def _check_policy_sensitivity(self, model: dict, requirements: dict) -> float:
        if not requirements.get("policy_sensitive"):
            return 0.0

        tags = [t.lower() for t in model.get("tags", [])]
        model_name = model.get("id", "").lower()

        if any(t in tags for t in ["uncensored", "unfiltered", "dirty", "explicit"]):
            return self.policy_penalty

        if any(t in model_name for t in ["uncensored", "unfiltered"]):
            return self.policy_penalty

        return 0.0

    def score_models(self, available_models: list[dict], task_description: str, top_n: int = 3) -> PonderRanking:
        """Score models.

        Returns:
            The PonderRanking result.

        Args:
            available_models: The available models.
            task_description: The task description.
            top_n: The top n.
        """
        now = self._utc_now()
        task_id = f"ponder_{now.strftime('%Y%m%d_%H%M%S')}"
        requirements = self._get_task_capability_requirements(task_description)

        scored_models = []

        for model in available_models:
            cap_score = self._calculate_capability_score(model, requirements)
            ctx_score = self._calculate_context_score(model, requirements)
            mem_score = self._calculate_memory_score(model)
            heur_score = self._calculate_heuristic_score(model, task_description)
            policy = self._check_policy_sensitivity(model, requirements)

            # Override memory score with Thompson arm mean when we have sufficient data
            model_id = model.get("id", "")
            _task_scores = {
                "coding": requirements.get("code", 0.5),
                "reasoning": requirements.get("reasoning", 0.5),
                "creative": requirements.get("creative", 0.3),
                "analysis": requirements.get("analysis", 0.5),
            }
            _effective_task_type = max(_task_scores, key=lambda k: _task_scores[k])
            thompson_score = self._get_thompson_score(model_id, _effective_task_type)
            if thompson_score is not None:
                mem_score = thompson_score

            weighted_sum = (
                cap_score * self.weights["capability"]
                + ctx_score * self.weights["context"]
                + mem_score * self.weights["memory"]
                + heur_score * self.weights["heuristic"]
            )
            # Policy penalty is multiplicative: a violation reduces the score to 30%
            # rather than subtracting 1.0 (which could produce negative totals).
            penalized_score = weighted_sum * 0.3 if policy < 0 else weighted_sum

            reasoning = []
            if cap_score > 0.7:
                reasoning.append(f"capability match: {cap_score:.2f}")
            if ctx_score > 0.8:
                reasoning.append(f"context fit: {ctx_score:.2f}")
            if mem_score > 0.8:
                reasoning.append(f"memory efficient: {mem_score:.2f}")
            if policy < 0:
                reasoning.append(f"policy penalty: {policy}")

            scored_models.append(
                ModelScore(
                    model_id=model_id or "unknown",
                    model_name=model_id or "unknown",
                    total_score=penalized_score,
                    capability_score=cap_score,
                    context_score=ctx_score,
                    memory_score=mem_score,
                    heuristic_score=heur_score,
                    policy_penalty=policy,
                    reasoning=", ".join(reasoning) if reasoning else "balanced profile",
                )
            )

        scored_models.sort(key=lambda x: x.total_score, reverse=True)

        return PonderRanking(
            task_id=task_id,
            task_description=task_description,
            rankings=scored_models[:top_n],
            timestamp=now.isoformat(),
        )

    def get_template_prompts(self) -> list[dict]:
        """Return ponder prompt templates (empty — template loading was removed).

        Returns:
            Empty list; template-file-based scoring has been superseded by
            the capability-scoring and Thompson-sampling methods on this class.
        """
        return []


__all__ = ["ModelScore", "PonderEngine", "PonderRanking"]
