"""Vetinari Self-Refinement Loop.

================================
Implements iterative self-correction for agent outputs.

After an agent produces an initial response, the loop:
1. Critiques the output using a second pass (optionally a different model)
2. If the critique identifies improvements, asks the agent to revise
3. Repeats up to MAX_ROUNDS times or until "no improvements needed"

Calibration for local llama-cpp-python models (5090, 32B Q4 model):
- Each round costs ~50-135 seconds of generation time
- Limit to 2 rounds for tasks where latency matters
- 3 rounds only for high-importance tasks (score below threshold)
- Trigger only when initial quality score < 0.7

Usage::

    from vetinari.learning.self_refinement import SelfRefinementLoop

    refiner = SelfRefinementLoop(adapter_manager)
    refined = refiner.refine(
        task_description="Implement a Redis cache wrapper",
        initial_output="class RedisCacheWrapper: pass",
        task_type="coding",
        model_id="qwen3-vl-32b",
        importance=0.9,   # 0.0-1.0; high importance = more rounds
    )
    logger.debug(refined.output)
    logger.debug(refined.rounds_used)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from vetinari.constants import (
    MAX_TOKENS_CRITIQUE,
    MAX_TOKENS_REVISION,
    TEMPERATURE_LOW,
    TEMPERATURE_VERY_LOW,
    TRUNCATE_OUTPUT_PREVIEW,
)
from vetinari.safety.prompt_sanitizer import sanitize_task_description, sanitize_worker_output

logger = logging.getLogger(__name__)


# Maximum rounds per importance tier
_ROUNDS_BY_IMPORTANCE = {
    "high": 3,  # importance >= 0.8
    "medium": 2,  # importance >= 0.5
    "low": 1,  # importance < 0.5
}

# Only trigger refinement when initial quality is below this.
# This is the fallback — task-type-specific thresholds from
# config/quality_thresholds.yaml take precedence when available.
_QUALITY_TRIGGER_THRESHOLD = float(os.environ.get("VETINARI_REFINE_THRESHOLD", "0.70"))


def _parse_refinement_decision(response: str) -> dict:
    """Parse structured JSON refinement decision from the judge response."""
    try:
        stripped = response.strip()
        if stripped.startswith("```"):
            lines = stripped.splitlines()
            stripped = "\n".join(lines[1:-1]) if len(lines) > 2 else stripped
        data = json.loads(stripped)
        return {
            "needs_refinement": bool(data.get("needs_refinement", False)),
            "reason": str(data.get("reason", "")),
            "confidence": float(data.get("confidence", 0.0)),
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning(
            "Self-refinement judge returned non-JSON response - treating as no-refinement-needed (FSA-0209). "
            "Response prefix: %s",
            response[:80],
        )
        return {"needs_refinement": False, "reason": "parse_failed", "confidence": 0.0}


def get_quality_threshold(task_type: str) -> float:
    """Load the quality trigger threshold for a specific task type.

    Reads from ``config/quality_thresholds.yaml``. Falls back to the
    module-level ``_QUALITY_TRIGGER_THRESHOLD`` for unknown task types
    or when the config file is missing.

    Args:
        task_type: Task type string (e.g. ``"security_audit"``, ``"code_review"``).

    Returns:
        Quality threshold as a float between 0.0 and 1.0.

    Raises:
        ValueError: If a threshold value is outside [0.0, 1.0].
    """
    from pathlib import Path

    try:
        import yaml

        config_path = Path(__file__).resolve().parent.parent.parent / "config" / "quality_thresholds.yaml"
        if not config_path.exists():
            return _QUALITY_TRIGGER_THRESHOLD

        raw = config_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw) or {}
        thresholds = data.get("thresholds", {})
        default = data.get("default", _QUALITY_TRIGGER_THRESHOLD)

        # Validate default threshold
        if not (0.0 <= default <= 1.0):
            raise ValueError(f"quality_thresholds.yaml 'default' value {default!r} is outside [0.0, 1.0]")

        # Get the resolved threshold
        resolved = float(thresholds.get(task_type, default))

        # Validate resolved threshold
        if not (0.0 <= resolved <= 1.0):
            raise ValueError(
                f"quality_thresholds.yaml value for task_type '{task_type}' = {resolved!r} is outside [0.0, 1.0]"
            )

        return resolved
    except Exception:
        logger.warning(
            "Failed to load quality threshold for task_type=%s — using default %.2f",
            task_type,
            _QUALITY_TRIGGER_THRESHOLD,
        )
        return _QUALITY_TRIGGER_THRESHOLD


@dataclass(frozen=True, slots=True)
class RefinementResult:
    """Result of a self-refinement cycle."""

    output: str
    rounds_used: int
    initial_quality: float
    final_quality: float
    improved: bool
    critique_summary: str = ""

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"RefinementResult(rounds_used={self.rounds_used!r},"
            f" initial_quality={self.initial_quality!r},"
            f" final_quality={self.final_quality!r}, improved={self.improved!r})"
        )


class SelfRefinementLoop:
    """Iterative critique-and-revise loop for agent outputs.

    Calibrated for local llama-cpp-python inference speed:
    - Importance >= 0.8 -> max 3 rounds
    - Importance 0.5-0.8 -> max 2 rounds
    - Importance < 0.5  -> max 1 round (single critique only)
    """

    def __init__(self, adapter_manager=None):
        self._adapter_manager = adapter_manager

    @staticmethod
    def _skipped_result(initial_output: str, quality: float, summary: str) -> RefinementResult:
        """Return a no-op refinement result."""
        return RefinementResult(
            output=initial_output,
            rounds_used=0,
            initial_quality=quality,
            final_quality=quality,
            improved=False,
            critique_summary=summary,
        )

    def _run_refinement_rounds(
        self,
        task_description: str,
        initial_output: str,
        task_type: str,
        model_id: str,
        max_rounds: int,
    ) -> tuple[str, int, str]:
        """Run critique/revision rounds and return output, rounds, and summary."""
        current_output = initial_output
        critique_summary = ""
        rounds_used = 0
        for round_num in range(1, max_rounds + 1):
            critique = self._critique(task_description, current_output, task_type, model_id)
            decision = _parse_refinement_decision(critique if critique is not None else "")
            if not decision["needs_refinement"]:
                logger.debug("[SelfRefinement] Round %s: no improvements needed", round_num)
                break
            critique_summary = decision["reason"][:300]
            revised = self._revise(task_description, current_output, decision["reason"], task_type, model_id)
            if not revised or revised == current_output:
                logger.debug("[SelfRefinement] Round %s: revision unchanged, stopping", round_num)
                break
            current_output = revised
            rounds_used = round_num
            logger.info(
                "[SelfRefinement] Round %d/%d completed for task '%s'",
                round_num,
                max_rounds,
                task_description[:50],
            )
        return current_output, rounds_used, critique_summary

    @staticmethod
    def _rescore_refined_output(
        task_description: str,
        task_type: str,
        model_id: str,
        current_output: str,
        initial_quality: float,
    ) -> float:
        """Return post-refinement quality, preserving initial score on scorer failure."""
        try:
            from vetinari.learning.quality_scorer import get_quality_scorer

            score_result = get_quality_scorer().score(
                task_id="refinement",
                model_id=model_id,
                task_type=task_type,
                task_description=task_description,
                output=current_output,
            )
            return float(score_result.overall_score)
        except Exception:
            logger.warning(
                "Quality scorer unavailable for refinement re-scoring of task '%s'"
                " - treating refinement as no improvement",
                task_description[:60],
            )
            return initial_quality

    def refine(
        self,
        task_description: str,
        initial_output: str,
        task_type: str = "general",
        model_id: str = "default",
        importance: float = 0.5,
        initial_quality: float | None = None,
    ) -> RefinementResult:
        """Run the self-refinement loop.

        Args:
            task_description: What the task asked for
            initial_output:   The agent's first response
            task_type:        coding / research / analysis / etc.
            model_id:         Model that produced the initial output
            importance:       Task importance (0.0-1.0); controls max rounds
            initial_quality:  Pre-computed quality score; skip if already good

        Returns:
            RefinementResult with the (possibly improved) output.
        """
        # Check if refinement is even needed (use per-task-type threshold from YAML)
        _threshold = get_quality_threshold(task_type)
        if initial_quality is not None and initial_quality >= _threshold:
            return self._skipped_result(initial_output, initial_quality, "Skipped: quality already above threshold")

        if not self._adapter_manager:
            return self._skipped_result(initial_output, initial_quality or 0.7, "Skipped: no adapter available")

        max_rounds = self._get_max_rounds(importance)
        current_output, rounds_used, critique_summary = self._run_refinement_rounds(
            task_description,
            initial_output,
            task_type,
            model_id,
            max_rounds,
        )

        # Re-score the output after refinement. Compute final_quality BEFORE
        # setting improved so that the improved flag reflects actual quality gain,
        # not just textual difference (a rewrite can look different but score worse).
        _initial_quality = initial_quality or 0.5
        final_quality = _initial_quality
        if rounds_used > 0 and current_output != initial_output:
            final_quality = self._rescore_refined_output(
                task_description,
                task_type,
                model_id,
                current_output,
                _initial_quality,
            )

        # Improvement is only real when the quality score actually went up.
        # Textual difference alone is insufficient — a rewrite can look different
        # but score worse, e.g. when the model adds padding or loses precision.
        improved = final_quality > _initial_quality

        return RefinementResult(
            output=current_output,
            rounds_used=rounds_used,
            initial_quality=_initial_quality,
            final_quality=final_quality,
            improved=improved,
            critique_summary=critique_summary,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _critique(
        self,
        task_description: str,
        output: str,
        task_type: str,
        model_id: str,
    ) -> str | None:
        """Ask the model to critique its own output."""
        safe_task = sanitize_task_description(task_description)
        safe_output = sanitize_worker_output(output)
        critique_prompt = (
            f"Review this {task_type} output critically.\n\n"
            f"TASK: {safe_task[:300]}\n\n"
            f"OUTPUT:\n{safe_output[:TRUNCATE_OUTPUT_PREVIEW]}\n\n"
            "Respond ONLY with valid JSON in this format:\n"
            '{"needs_refinement": true, "reason": "one concrete sentence", "confidence": 0.0}\n'
            "Use needs_refinement=false when the output is complete and correct."
        )
        try:
            return self._call_llm(
                model_id=model_id,
                system="You are a rigorous quality reviewer. Be honest and specific.",
                user=critique_prompt,
                max_tokens=MAX_TOKENS_CRITIQUE,
                temperature=TEMPERATURE_LOW,
            )
        except Exception:
            logger.error("[SelfRefinement] Critique failed; aborting refinement", exc_info=True)
            raise

    def _revise(
        self,
        task_description: str,
        original_output: str,
        critique: str,
        task_type: str,
        model_id: str,
    ) -> str | None:
        """Ask the model to produce a revised output based on the critique."""
        safe_task = sanitize_task_description(task_description)
        safe_output = sanitize_worker_output(original_output)
        safe_critique = sanitize_worker_output(critique)
        revise_prompt = (
            f"Revise this {task_type} output based on the critique.\n\n"
            f"TASK: {safe_task[:300]}\n\n"
            f"PREVIOUS OUTPUT:\n{safe_output[:TRUNCATE_OUTPUT_PREVIEW]}\n\n"
            f"CRITIQUE:\n{safe_critique[:500]}\n\n"
            "Provide the improved, complete output. Apply all critique points."
        )
        try:
            return self._call_llm(
                model_id=model_id,
                system="You are an expert reviser. Apply the critique precisely.",
                user=revise_prompt,
                max_tokens=MAX_TOKENS_REVISION,
                temperature=TEMPERATURE_VERY_LOW,
            )
        except Exception as e:
            logger.warning("[SelfRefinement] Revise failed — returning original output: %s", e)
            return None

    def _call_llm(
        self,
        model_id: str,
        system: str,
        user: str,
        max_tokens: int = 512,
        temperature: float = 0.2,
    ) -> str | None:
        """Run inference through the adapter manager, falling back to LocalInferenceAdapter.

        Uses ``self._adapter_manager`` when available so that the caller's
        inference provider (including cascade routing, permission checks, and
        metrics) is honoured.  Falls back to a direct ``LocalInferenceAdapter``
        only when no manager was supplied.

        Args:
            model_id: Model identifier to use for inference.
            system: System prompt text.
            user: User prompt text.
            max_tokens: Maximum tokens to generate.
            temperature: Sampling temperature.

        Returns:
            Generated text or None if inference failed.
        """
        from vetinari.adapters.base import InferenceRequest

        if self._adapter_manager is not None:
            req = InferenceRequest(
                model_id=model_id,
                prompt=user,
                system_prompt=system,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            resp = self._adapter_manager.infer(req)
            output = resp.output if resp else ""
            return output.strip() if output else None

        # Fallback when no adapter manager was supplied at construction time.
        from vetinari.adapters.adapter_cache import get_local_inference_adapter

        adapter = get_local_inference_adapter(model_id)
        result = adapter.infer(
            InferenceRequest(
                model_id=model_id,
                prompt=user,
                system_prompt=system,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        )
        output = result.output if result else ""
        return output.strip() if output else None

    @staticmethod
    def _get_max_rounds(importance: float) -> int:
        if importance >= 0.8:
            return _ROUNDS_BY_IMPORTANCE["high"]
        if importance >= 0.5:
            return _ROUNDS_BY_IMPORTANCE["medium"]
        return _ROUNDS_BY_IMPORTANCE["low"]


# ---------------------------------------------------------------------------
# Module-level accessor
# ---------------------------------------------------------------------------

_refiner: SelfRefinementLoop | None = None
_refiner_lock = __import__("threading").Lock()


def get_self_refiner(adapter_manager=None) -> SelfRefinementLoop:
    """Return the global SelfRefinementLoop singleton.

    When the singleton already exists but was created without an adapter
    manager and a real one is now provided, the manager is injected into
    the existing instance so that subsequent calls use it (defect 3 fix).

    Args:
        adapter_manager: Optional adapter manager to inject.  Ignored when
            the singleton already has a non-``None`` manager.

    Returns:
        The shared SelfRefinementLoop instance for this process.
    """
    global _refiner
    if _refiner is None:
        with _refiner_lock:
            if _refiner is None:
                _refiner = SelfRefinementLoop(adapter_manager)
    elif adapter_manager is not None and _refiner._adapter_manager is None:
        # Upgrade the existing singleton with the real manager now available.
        _refiner._adapter_manager = adapter_manager
    return _refiner
