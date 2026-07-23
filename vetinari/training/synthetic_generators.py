"""Synthetic data generators: Magpie zero-seed and EvolveR strategy distillation.

Provides two specialised generators that complement ``SyntheticDataGenerator``
(which mines past episodes):

- ``MagpieGenerator`` — zero-seed instruction generation (ICLR 2025) via
  auto-regressive bootstrapping from the model's own distribution.
- ``StrategyDistiller`` — EvolveR-style offline self-distillation that extracts
  reusable abstract strategies from successful execution traces.

Both classes import vetinari dependencies lazily inside each method so that
they can be instantiated even when the backing stores or LLM adapters are
absent.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from vetinari.adapters.adapter_cache import get_local_inference_adapter

logger = logging.getLogger(__name__)


# Minimum instruction length accepted by quality filter
_MIN_INSTRUCTION_LEN = 10
# Maximum instruction length accepted by quality filter
_MAX_INSTRUCTION_LEN = 500
# Magpie over-generates by this multiplier then filters down to the target count
_MAGPIE_OVERSAMPLE_FACTOR = 2
_MAGPIE_INSTRUCTION_TEMPERATURE = 0.9
_MAGPIE_RESPONSE_TEMPERATURE = 0.7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Return a normalised lowercase version of text for deduplication.

    Strips leading/trailing whitespace and collapses internal runs of
    whitespace to a single space so that cosmetically different but
    semantically identical strings compare equal.

    Args:
        text: Raw instruction string.

    Returns:
        Normalised string suitable for equality comparison.
    """
    return re.sub(r"\s+", " ", text.strip().lower())


# ---------------------------------------------------------------------------
# MagpieGenerator
# ---------------------------------------------------------------------------


class MagpieGenerator:
    """Zero-seed synthetic instruction generation via auto-regressive bootstrapping.

    Implements the Magpie method (ICLR 2025): feed only a chat-template
    preamble to the model and let it auto-regressively complete the user
    turn — generating diverse instructions without seed data.  Instructions
    are quality-filtered and deduplicated before responses are generated.

    Reference: Xu et al., "Magpie: Alignment Data Synthesis from Scratch by
    Prompting Aligned LLMs with Nothing", ICLR 2025.
    """

    def __init__(self, system_prompt: str = "") -> None:
        """Initialise the Magpie generator.

        Args:
            system_prompt: Optional system prompt to focus instruction
                generation on a particular domain.  Leave empty for
                general-purpose generation.
        """
        self._system_prompt = system_prompt
        self._adapter: Any | None = None
        self._adapter_checked = False

    def generate_instructions(
        self,
        count: int = 500,
        domain: str | None = None,
    ) -> list[dict[str, Any]]:
        """Generate instruction-response pairs from the model's own distribution.

        Steps:
          1. Build a chat-template preamble (system + partial human turn).
          2. Feed the preamble to the model at temperature 0.9 to let it
             auto-complete a diverse instruction.
          3. Quality-filter: skip instructions shorter than 10 chars or
             longer than 500 chars.
          4. Deduplicate via normalised string comparison.
          5. Generate a response at temperature 0.7 for each kept instruction.
          6. Return the collected pairs.

        Over-generates by ``_MAGPIE_OVERSAMPLE_FACTOR`` x ``count`` then
        filters down to ``count`` to compensate for quality and dedup losses.

        Args:
            count: Number of instruction-response pairs to return.
            domain: Optional domain hint injected into the preamble (e.g.
                "Python coding", "data analysis").

        Returns:
            List of dicts with keys ``instruction``, ``input``, ``output``,
            ``source``, ``domain``.  Empty list if no adapter is available.
        """
        if not self.is_available():
            logger.warning("[MagpieGenerator] No adapter available — returning empty dataset")
            return []

        adapter = self._get_adapter()
        if adapter is None:
            return []

        target = count
        attempts = target * _MAGPIE_OVERSAMPLE_FACTOR
        results: list[dict[str, Any]] = []

        for _ in range(attempts):
            if len(results) >= target:
                break

            instruction = self._generate_instruction(adapter, domain)
            instruction = self._normalise_instruction_length(instruction)
            if not instruction:
                continue

            # Step 3: Deduplication
            if self._is_duplicate(instruction, results):
                continue

            response = self._generate_response(adapter, instruction)
            if not response:
                continue

            results.append({
                "instruction": instruction,
                "input": "",
                "output": response,
                "source": "magpie",
                "domain": domain or "general",
            })

        logger.info(
            "[MagpieGenerator] Generated %d / %d requested pairs",
            len(results),
            target,
        )
        return results[:target]

    def _generate_instruction(self, adapter: Any, domain: str | None) -> str:
        try:
            instr_result = adapter.chat(
                model_id="default",
                system_prompt=self._system_prompt or "You are a helpful assistant. Generate a single user instruction.",
                input_text=self._build_preamble(self._system_prompt, domain),
                temperature=_MAGPIE_INSTRUCTION_TEMPERATURE,
            )
            return instr_result.get("output", "").strip()
        except Exception as exc:
            logger.warning("[MagpieGenerator] Instruction generation failed: %s", exc)
            return ""

    @staticmethod
    def _normalise_instruction_length(instruction: str) -> str:
        if len(instruction) < _MIN_INSTRUCTION_LEN:
            return ""
        if len(instruction) <= _MAX_INSTRUCTION_LEN:
            return instruction
        truncated = instruction[:_MAX_INSTRUCTION_LEN].rsplit(".", 1)[0] + "."
        return truncated if len(truncated) >= _MIN_INSTRUCTION_LEN else ""

    def _generate_response(self, adapter: Any, instruction: str) -> str:
        try:
            resp_result = adapter.chat(
                model_id="default",
                system_prompt=self._system_prompt or "You are a helpful assistant.",
                input_text=instruction,
                temperature=_MAGPIE_RESPONSE_TEMPERATURE,
            )
            return resp_result.get("output", "").strip()
        except Exception as exc:
            logger.warning("[MagpieGenerator] Response generation failed: %s", exc)
            return ""

    @staticmethod
    def _build_preamble(system_prompt: str, domain: str | None) -> str:
        """Build the chat-template prefix used to elicit instructions.

        The preamble is fed as the user turn so the model auto-regressively
        completes an instruction-like utterance.

        Args:
            system_prompt: Optional system prompt for domain focus.
            domain: Optional domain label (e.g. "Python coding").

        Returns:
            Preamble string passed as ``input_text`` to the adapter.
        """
        parts = ["Please give me a task"]
        if domain:
            parts.append(f"related to {domain}")
        if system_prompt:
            parts.append(f"within the context of: {system_prompt}")
        parts.append("as a single clear instruction.")
        return " ".join(parts)

    @staticmethod
    def _is_duplicate(instruction: str, existing: list[dict[str, Any]]) -> bool:
        """Return True if instruction is already in existing (normalised compare).

        Args:
            instruction: Candidate instruction string.
            existing: List of already-accepted instruction dicts.

        Returns:
            True if a normalised match is found in existing.
        """
        normalised = _normalize(instruction)
        return any(_normalize(item.get("instruction", "")) == normalised for item in existing)

    def is_available(self) -> bool:
        """Return True if the local inference adapter is importable.

        Returns:
            True when ``vetinari.adapters.llama_cpp_adapter`` can be imported
            and an adapter instance can be created.
        """
        return self._get_adapter() is not None

    def _get_adapter(self) -> Any | None:
        """Return a local inference adapter if available, else None.

        Returns:
            ``LocalInferenceAdapter`` instance or ``None``.
        """
        try:
            if not self._adapter_checked:
                self._adapter = get_local_inference_adapter()
                self._adapter_checked = True
            return self._adapter
        except Exception as exc:
            logger.warning("[MagpieGenerator] Adapter import failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# StrategyDistiller
# ---------------------------------------------------------------------------


class StrategyDistiller:
    """EvolveR-style offline self-distillation from execution traces.

    Analyses successful episodes to extract abstract, reusable strategies
    (principles) that can be injected into future agent prompts or used as
    training data for strategy-aware fine-tuning.

    Reference: EvolveR (offline RL self-distillation paradigm) — extract
    high-level behavioural patterns from rollout trajectories and represent
    them as transferable principles.
    """

    def __init__(self) -> None:
        """Initialise with no required arguments.

        All vetinari module imports are deferred to method bodies.
        """
        self._adapter: Any | None = None
        self._adapter_checked = False

    def distill_strategies(
        self,
        min_score: float = 0.8,
        max_episodes: int = 100,
    ) -> list[dict[str, Any]]:
        """Extract abstract strategies from high-scoring execution episodes.

        For each successful episode above ``min_score``, generates a
        concise principle (what made this approach work), when to apply it,
        and a confidence estimate derived from the episode quality score.

        Args:
            min_score: Quality score threshold.  Only episodes at or above
                this score contribute to strategy extraction.
            max_episodes: Maximum episodes to analyse in a single call.

        Returns:
            List of dicts with keys ``principle``, ``when_to_apply``,
            ``evidence``, ``confidence``.  Empty list if episode memory is
            unavailable or no qualifying episodes exist.
        """
        try:
            from vetinari.learning.episode_memory import get_episode_memory
        except ImportError:
            logger.warning("[StrategyDistiller] episode_memory unavailable — returning empty strategies")
            return []

        try:
            mem = get_episode_memory()
        except Exception as exc:
            logger.warning("[StrategyDistiller] Could not access episode memory: %s", exc)
            return []

        episodes = mem.recall(
            "successful high quality execution",
            k=max_episodes,
            min_score=min_score,
            successful_only=True,
        )

        if not episodes:
            logger.info(
                "[StrategyDistiller] No qualifying episodes for strategy distillation (min_score=%.2f)",
                min_score,
            )
            return []

        # Try adapter for LLM-assisted distillation
        adapter = self._get_adapter()
        strategies: list[dict[str, Any]] = []
        seen_principles: list[str] = []

        for ep in episodes:
            principle, when_to_apply = self._distill_episode_strategy(ep, adapter, min_score)

            # Dedup by normalised principle text
            if _normalize(principle) in [_normalize(p) for p in seen_principles]:
                continue

            seen_principles.append(principle)
            strategies.append({
                "principle": principle,
                "when_to_apply": when_to_apply,
                "evidence": ep.episode_id,
                "confidence": round(ep.quality_score, 3),
            })

        logger.info(
            "[StrategyDistiller] Distilled %d strategies from %d episodes",
            len(strategies),
            len(episodes),
        )
        return strategies

    def _distill_episode_strategy(self, ep: Any, adapter: Any | None, min_score: float) -> tuple[str, str]:
        principle = ""
        when_to_apply = ""
        if adapter is not None:
            try:
                result = adapter.chat(
                    model_id="default",
                    system_prompt=(
                        "You are an expert at extracting reusable software "
                        "engineering principles from execution traces."
                    ),
                    input_text=self._distillation_prompt(ep),
                )
                principle, when_to_apply = self._parse_distillation(result.get("output", ""))
            except Exception as exc:
                logger.warning("[StrategyDistiller] LLM distillation failed for %s: %s", ep.episode_id, exc)
        if principle:
            return principle, when_to_apply
        return (
            f"For {ep.task_type} tasks: apply the approach shown in '{ep.task_summary[:60]}'",
            f"When handling {ep.task_type} tasks with quality >= {min_score:.1f}",
        )

    @staticmethod
    def _distillation_prompt(ep: Any) -> str:
        return (
            "Analyse this successful task execution and extract ONE reusable principle or strategy.\n\n"
            f"Task: {ep.task_summary}\n"
            f"Approach: {ep.output_summary}\n"
            f"Quality score: {ep.quality_score:.2f}\n\n"
            "Respond in this exact format:\n"
            "Principle: <one sentence principle>\n"
            "When to apply: <brief description of contexts>\n"
        )

    def store_strategies(self, strategies: list[dict[str, Any]]) -> int:
        """Store distilled strategies as episodes in episodic memory.

        Each strategy is recorded as a synthetic episode of type
        ``strategy`` so it can be recalled during future task planning.

        Args:
            strategies: List of strategy dicts as returned by
                ``distill_strategies``.

        Returns:
            Number of strategies successfully stored.  Returns 0 if
            episode memory is unavailable.
        """
        if not strategies:
            return 0

        try:
            from vetinari.learning.episode_memory import get_episode_memory
        except ImportError:
            logger.warning("[StrategyDistiller] episode_memory unavailable — strategies not stored")
            return 0

        try:
            mem = get_episode_memory()
        except Exception as exc:
            logger.warning(
                "[StrategyDistiller] Could not access episode memory for storage: %s",
                exc,
            )
            return 0

        stored = 0
        for strat in strategies:
            try:
                mem.record(
                    task_description=strat.get("when_to_apply", "strategy"),
                    agent_type="StrategyDistiller",
                    task_type="strategy",
                    output_summary=strat.get("principle", ""),
                    quality_score=float(strat.get("confidence", 0.8)),
                    success=True,
                    metadata={
                        "evidence": strat.get("evidence", ""),
                        "when_to_apply": strat.get("when_to_apply", ""),
                    },
                )
                stored += 1
            except Exception as exc:
                logger.warning("[StrategyDistiller] Failed to store strategy: %s", exc)

        logger.info("[StrategyDistiller] Stored %d / %d strategies", stored, len(strategies))
        return stored

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_distillation(raw: str) -> tuple[str, str]:
        """Parse the structured distillation response from the LLM.

        Expects the format:
          Principle: <text>
          When to apply: <text>

        Args:
            raw: Raw LLM response string.

        Returns:
            Tuple of (principle, when_to_apply).  Empty strings if parsing
            fails.
        """
        principle = ""
        when_to_apply = ""

        for line in raw.splitlines():
            if line.lower().startswith("principle:"):
                principle = line.split(":", 1)[1].strip()
            elif line.lower().startswith("when to apply:"):
                when_to_apply = line.split(":", 1)[1].strip()

        return principle, when_to_apply

    def _get_adapter(self) -> Any | None:
        """Return a local inference adapter if available, else None.

        Returns:
            ``LocalInferenceAdapter`` instance or ``None``.
        """
        try:
            if not self._adapter_checked:
                self._adapter = get_local_inference_adapter()
                self._adapter_checked = True
            return self._adapter
        except Exception as exc:
            logger.warning("[StrategyDistiller] Adapter import failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


def store_distilled_strategies(strategies: list[dict[str, Any]]) -> int:
    """Store distilled strategies as episodes in episodic memory.

    Module-level convenience wrapper around
    ``StrategyDistiller().store_strategies()``.

    Args:
        strategies: List of strategy dicts as returned by
            ``StrategyDistiller.distill_strategies``.

    Returns:
        Number of strategies successfully stored.
    """
    return StrategyDistiller().store_strategies(strategies)
