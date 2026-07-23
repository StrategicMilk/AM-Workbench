"""Self-evolved benchmarks — reframe seed prompts into harder variations.

Applies four deterministic transformations to eval prompts:
paraphrase, add noise, reverse polarity, increase complexity.
Produces a 4x expanded corpus with provenance tracking.

Pipeline role: Quality Gate — expands static benchmarks with automatic diversity.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir
from vetinari.learning.atomic_writers import write_yaml_atomic

logger = logging.getLogger(__name__)


# -- Configuration --
def _default_output_dir() -> Path:
    """Return the default evolved corpus directory, resolved lazily via get_user_dir().

    Avoids binding the path at import time so tests can override VETINARI_USER_DIR.
    """
    return get_user_dir() / "testing" / "evolved_corpus"


# Synonym table for paraphrasing common programming terms
_SYNONYMS: dict[str, list[str]] = {
    "write": ["create", "implement", "develop", "build"],
    "function": ["method", "routine", "procedure", "subroutine"],
    "check": ["verify", "validate", "test", "examine"],
    "return": ["produce", "output", "yield", "give back"],
    "list": ["array", "collection", "sequence", "series"],
    "error": ["exception", "fault", "issue", "problem"],
    "fast": ["efficient", "quick", "performant", "optimized"],
    "simple": ["basic", "straightforward", "minimal", "concise"],
}

# Complexity constraints appended by increase_complexity
_COMPLEXITY_CONSTRAINTS: list[str] = [
    "Handle edge cases including empty input and None values.",
    "Optimize for both time and space complexity.",
    "Include type hints and a docstring.",
    "Support both synchronous and asynchronous usage.",
    "Add input validation with descriptive error messages.",
    "Make it thread-safe for concurrent access.",
    "Ensure it works on Python 3.10+ with no external dependencies.",
    "Include logging at appropriate levels.",
]

# Noise patterns for add_noise
_NOISE_TYPOS: dict[str, str] = {
    "the": "teh",
    "function": "fucntion",
    "return": "retrun",
    "class": "calss",
    "import": "imoprt",
    "python": "pyhton",
}


@dataclass(frozen=True, slots=True)
class EvolvedPrompt:
    """A seed prompt transformed by one reframing operation."""

    original: str
    transformed: str
    operation: str
    expected_outcome: str = "response addresses the transformed prompt without unsafe or off-topic behavior"
    rubric: dict[str, Any] = field(
        default_factory=lambda: {
            "must_address_prompt": True,
            "must_preserve_original_intent": True,
            "must_not_introduce_unsafe_content": True,
        }
    )
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        preview = self.original[:40] + "..." if len(self.original) > 40 else self.original
        return f"EvolvedPrompt(operation={self.operation!r}, original={preview!r})"


class BenchmarkEvolver:
    """Apply deterministic reframing operations to seed eval prompts.

    Four operations: paraphrase, add_noise, reverse_polarity, increase_complexity.
    Each produces a different version of the input prompt.

    Args:
        output_dir: Directory for storing evolved corpus YAML files.
        seed: Random seed for reproducible noise generation.
    """

    def __init__(
        self,
        output_dir: Path | None = None,
        seed: int = 42,
    ) -> None:
        self._output_dir = output_dir or _default_output_dir()
        self._rng = random.Random(seed)

    def paraphrase(self, prompt: str) -> str:
        """Reword a prompt using synonym substitution.

        Replaces known programming terms with synonyms to test
        model robustness to phrasing variation.

        Args:
            prompt: The original prompt text.

        Returns:
            Paraphrased version with synonym substitutions applied.
        """
        result = prompt
        for word, synonyms in _SYNONYMS.items():
            pattern = re.compile(rf"\b{word}\b", re.IGNORECASE)
            match = pattern.search(result)
            if match:
                replacement = self._rng.choice(synonyms)
                # Preserve original casing of first character
                if result[match.start()].isupper():
                    replacement = replacement.capitalize()
                result = pattern.sub(replacement, result, count=1)
        return result

    def add_noise(self, prompt: str) -> str:
        """Introduce typos, extra whitespace, and mixed casing.

        Tests model robustness to noisy input that mimics
        real-world user typing errors.

        Args:
            prompt: The original prompt text.

        Returns:
            Noisy version with deliberate imperfections.
        """
        words = prompt.split()
        noisy_words: list[str] = []
        for word in words:
            lower = word.lower().strip(".,!?;:")
            if lower in _NOISE_TYPOS and self._rng.random() < 0.4:
                # Apply typo
                noisy_words.append(word.replace(lower, _NOISE_TYPOS[lower]))
            elif self._rng.random() < 0.15:
                # Random case flip
                noisy_words.append(word.swapcase())
            elif self._rng.random() < 0.1:
                # Extra space
                noisy_words.append(f" {word} ")
            else:
                noisy_words.append(word)
        return " ".join(noisy_words)

    def reverse_polarity(self, prompt: str) -> str:
        """Negate the request to test opposite-case handling.

        Transforms "write X" into "what NOT to do when writing X"
        to test the model on negative/avoidance-style instructions.

        Args:
            prompt: The original prompt text.

        Returns:
            Negated version focusing on what NOT to do.
        """
        # Pattern: "Write/Create/Build X" -> "What should you NOT do when writing X?"
        action_match = re.match(
            r"^(Write|Create|Build|Implement|Design|Make)\s+(.+)",
            prompt,
            re.IGNORECASE,
        )
        if action_match:
            verb = action_match.group(1).lower()
            gerund = verb + "ing" if not verb.endswith("e") else verb[:-1] + "ing"
            remainder = action_match.group(2)
            return f"What are common mistakes to avoid when {gerund} {remainder}"

        # Pattern: "How to X" -> "What NOT to do when X"
        how_match = re.match(r"^How\s+to\s+(.+)", prompt, re.IGNORECASE)
        if how_match:
            return f"What should you NOT do when trying to {how_match.group(1)}"

        # Fallback: prepend negation
        return f"What is wrong with the following approach: {prompt}"

    def increase_complexity(self, prompt: str) -> str:
        """Add constraints to make the prompt harder.

        Appends additional requirements from a predefined list
        to test model handling of multi-constraint instructions.

        Args:
            prompt: The original prompt text.

        Returns:
            Extended version with additional complexity constraints.
        """
        num_constraints = self._rng.randint(1, 3)
        constraints = self._rng.sample(
            _COMPLEXITY_CONSTRAINTS,
            min(num_constraints, len(_COMPLEXITY_CONSTRAINTS)),
        )
        suffix = " ".join(constraints)
        # Ensure base prompt ends with period
        base = prompt.rstrip()
        if base and base[-1] not in ".!?":
            base += "."
        return f"{base} Additionally: {suffix}"

    def evolve_corpus(self, seeds: list[str]) -> list[EvolvedPrompt]:
        """Apply all 4 operations to each seed, returning a 4x expanded set.

        Args:
            seeds: List of seed prompt strings.

        Returns:
            List of EvolvedPrompt with provenance (4 per seed).
        """
        operations = [
            ("paraphrase", self.paraphrase),
            ("add_noise", self.add_noise),
            ("reverse_polarity", self.reverse_polarity),
            ("increase_complexity", self.increase_complexity),
        ]
        evolved: list[EvolvedPrompt] = []
        for seed in seeds:
            for op_name, op_fn in operations:
                transformed = op_fn(seed)
                evolved.append(
                    EvolvedPrompt(
                        original=seed,
                        transformed=transformed,
                        operation=op_name,
                    )
                )
        logger.info("Evolved %d seeds into %d benchmarks", len(seeds), len(evolved))
        return evolved

    def store_corpus(self, evolved: list[EvolvedPrompt]) -> Path:
        """Persist evolved benchmarks as a timestamped YAML file.

        Args:
            evolved: List of EvolvedPrompt to store.

        Returns:
            Path to the created YAML file.
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = self._output_dir / f"evolved_{ts}.yaml"

        data: dict[str, Any] = {
            "evolved_benchmarks": [
                {
                    "original": ep.original,
                    "transformed": ep.transformed,
                    "operation": ep.operation,
                    "expected_outcome": ep.expected_outcome,
                    "rubric": ep.rubric,
                    "timestamp": ep.timestamp,
                }
                for ep in evolved
            ],
            "total": len(evolved),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        write_yaml_atomic(out_path, data)
        logger.info("Evolved corpus stored at %s", out_path)
        return out_path
