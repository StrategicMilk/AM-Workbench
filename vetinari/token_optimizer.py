"""Vetinari Token Optimizer.

Comprehensive token usage optimization system including:

1. **Token Budget Enforcement** — per-task and per-plan limits
2. **Context Summarisation** — compress long contexts before they overflow
3. **Dynamic max_tokens** — task-type-aware output limits
4. **Local LLM Preprocessing** — use cheap local llama-cpp-python models to
   compress context before sending to expensive cloud models (reduces cloud tokens 30-60%)
5. **Structured output enforcement** — JSON mode where supported
6. **Context deduplication** — avoid sending the same context repeatedly
7. **Task-specific model profiles** — optimal temperature/tokens per task type

Usage:
    from vetinari.token_optimizer import get_token_optimizer, TokenBudget

    optimizer = get_token_optimizer()

    # Check budget before inference
    budget = TokenBudget(plan_id="plan_123", max_tokens=50000)
    compressed = optimizer.prepare_prompt(
        prompt=long_prompt,
        context=big_context,
        task_type="coding",
        budget=budget,
    )
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from vetinari.context.window_manager import count_tokens
from vetinari.token_compression import (
    _COMPRESS_THRESHOLD_CHARS as _TOKEN_COMPRESSION_THRESHOLD_CHARS,
)
from vetinari.token_compression import (
    LocalPreprocessor,
)

logger = logging.getLogger(__name__)


_COMPRESS_THRESHOLD_CHARS = _TOKEN_COMPRESSION_THRESHOLD_CHARS

# ---------------------------------------------------------------------------
# Task-type profiles: (max_tokens, temperature, prefer_json)
# ---------------------------------------------------------------------------
TASK_PROFILES: dict[str, tuple[int, float, bool]] = {
    "planning": (3000, 0.2, True),
    "planner": (3000, 0.2, True),
    "coding": (4096, 0.1, False),
    "code_gen": (4096, 0.1, False),
    "builder": (4096, 0.1, False),
    "research": (2048, 0.4, True),
    "researcher": (2048, 0.4, True),
    "analysis": (2048, 0.3, True),
    "oracle": (2048, 0.3, True),
    "documentation": (3000, 0.3, False),
    "documentation_agent": (3000, 0.3, False),
    "security": (2048, 0.2, True),
    "security_auditor": (2048, 0.2, True),
    "testing": (3000, 0.1, True),
    "test_automation": (3000, 0.1, True),
    "ui_design": (3000, 0.4, True),
    "ui_planner": (3000, 0.4, True),
    "data_engineering": (2048, 0.2, True),
    "data_engineer": (2048, 0.2, True),
    "classification": (256, 0.0, True),
    "extraction": (512, 0.0, True),
    "summarisation": (1024, 0.2, False),
    "summarization": (1024, 0.2, False),
    "synthesis": (2048, 0.3, False),
    "synthesizer": (2048, 0.3, False),
    "evaluation": (1024, 0.1, True),
    "evaluator": (1024, 0.1, True),
    "exploration": (1500, 0.3, True),
    "explorer": (1500, 0.3, True),
    "general": (2048, 0.3, False),
}


@dataclass
class TokenBudget:
    """Per-plan token budget with enforcement."""

    plan_id: str
    max_tokens: int = 100_000  # Total token ceiling for the whole plan
    max_tokens_per_task: int = 8_000  # Per-task ceiling
    tokens_used: int = 0
    task_token_counts: dict[str, int] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"TokenBudget(plan_id={self.plan_id!r}, tokens_used={self.tokens_used!r}, max_tokens={self.max_tokens!r})"
        )

    def record(self, task_id: str, tokens: int) -> None:
        """Record.

        Args:
            task_id: The task id.
            tokens: The tokens.
        """
        self.tokens_used += tokens
        self.task_token_counts[task_id] = self.task_token_counts.get(task_id, 0) + tokens

    @property
    def remaining(self) -> int:
        """Return the number of tokens still available under this budget."""
        return max(0, self.max_tokens - self.tokens_used)

    @property
    def is_exhausted(self) -> bool:
        """Return True if the token budget has been fully consumed."""
        return self.tokens_used >= self.max_tokens

    def check_task(self, task_id: str, estimated_tokens: int) -> bool:
        """Return True if this task can proceed within budget.

        Args:
            task_id: The task id.
            estimated_tokens: The estimated tokens.

        Returns:
            True if successful, False otherwise.
        """
        if self.is_exhausted:
            return False
        task_used = self.task_token_counts.get(task_id, 0)
        return not task_used + estimated_tokens > self.max_tokens_per_task


# LocalPreprocessor is defined in vetinari.token_compression and re-exported here.
# The class is imported at the top of this module.
class TokenOptimizer:
    """Central token optimization orchestrator.

    Integrates:
    - TokenBudget enforcement
    - Task-specific model profiles (max_tokens, temperature)
    - Context summarisation for long inputs
    - Local LLM preprocessing for cloud calls
    - Context deduplication
    """

    def __init__(self):
        self._budgets: dict[str, TokenBudget] = {}
        self._preprocessor = LocalPreprocessor()
        # Bounded dedup cache — prevents unbounded memory growth
        self._context_cache: OrderedDict[str, str] = OrderedDict()
        self._max_context_cache: int = 1024

    # ------------------------------------------------------------------
    # Budget management
    # ------------------------------------------------------------------

    def create_budget(
        self,
        plan_id: str,
        max_tokens: int = 100_000,
        max_tokens_per_task: int = 8_000,
    ) -> TokenBudget:
        """Create and register a token budget for a plan.

        Args:
            plan_id: The plan id.
            max_tokens: The max tokens.
            max_tokens_per_task: The max tokens per task.

        Returns:
            The TokenBudget result.
        """
        budget = TokenBudget(
            plan_id=plan_id,
            max_tokens=max_tokens,
            max_tokens_per_task=max_tokens_per_task,
        )
        self._budgets[plan_id] = budget
        return budget

    def get_budget(self, plan_id: str) -> TokenBudget | None:
        """Retrieve the token budget for a plan, if one has been registered.

        Args:
            plan_id: Identifier of the plan whose budget to look up.

        Returns:
            The TokenBudget for the plan, or None if no budget has been created.
        """
        return self._budgets.get(plan_id)

    def record_usage(self, plan_id: str, task_id: str, tokens: int) -> None:
        """Record token usage after a completed inference.

        Args:
            plan_id: The plan id.
            task_id: The task id.
            tokens: The tokens.
        """
        budget = self._budgets.get(plan_id)
        if budget:
            budget.record(task_id, tokens)

    # ------------------------------------------------------------------
    # Task profile resolution
    # ------------------------------------------------------------------

    def get_task_profile(self, task_type: str) -> tuple[int, float, bool]:
        """Return inference parameters appropriate for a task type.

        Args:
            task_type: Task type string (e.g. "coding", "research", "planning").

        Returns:
            3-tuple of (max_tokens, temperature, prefer_json). Falls back to
            the "general" profile when the task type is not recognised.
        """
        key = task_type.lower().replace(" ", "_").replace("-", "_")
        return TASK_PROFILES.get(key, TASK_PROFILES["general"])

    # ------------------------------------------------------------------
    # Prompt preparation
    # ------------------------------------------------------------------

    def prepare_prompt(
        self,
        prompt: str,
        context: str = "",
        task_type: str = "general",
        task_description: str = "",
        is_cloud_model: bool = False,
        context_length: int = 8192,
        plan_id: str | None = None,
        task_id: str | None = None,
        budget: TokenBudget | None = None,
    ) -> dict[str, Any]:
        """Prepare an optimized prompt for inference.

        Args:
            prompt: Prompt value consumed by prepare_prompt().
            context: Context value consumed by prepare_prompt().
            task_type: Task type value consumed by prepare_prompt().
            task_description: Task description value consumed by prepare_prompt().
            is_cloud_model: Is cloud model value consumed by prepare_prompt().
            context_length: Context length value consumed by prepare_prompt().
            plan_id: Plan id value consumed by prepare_prompt().
            task_id: Task id value consumed by prepare_prompt().
            budget: Budget value consumed by prepare_prompt().

        Returns:
            Value produced for the caller.
        """
        max_tokens, temperature, prefer_json = self.get_task_profile(task_type)
        meta: dict[str, Any] = {
            "task_type": task_type,
            "task_profile": {"max_tokens": max_tokens, "temperature": temperature},
            "is_cloud_model": is_cloud_model,
        }
        context_tokens = count_tokens(context) if context else 0
        if context and context_tokens >= int(context_length * 0.7):
            prompt, context, compress_meta = self._preprocessor.preprocess_for_cloud(prompt, context, task_description)
            meta.update(compress_meta)
        estimated_input_tokens = count_tokens(f"{prompt}\n{context}" if context else prompt)
        estimated_total = estimated_input_tokens + max_tokens
        active_budget = budget or (self._budgets.get(plan_id) if plan_id else None)
        budget_ok = self._apply_budget_meta(active_budget, task_id, estimated_total, meta)
        context_hash = hashlib.md5(context.encode(), usedforsecurity=False).hexdigest() if context else ""
        if context_hash and context_hash in self._context_cache:
            meta["context_deduplicated"] = True
            self._context_cache.move_to_end(context_hash)
            context = f"[Context unchanged from previous task — key points: {context[:200]}...]"
        elif context_hash:
            if len(self._context_cache) >= self._max_context_cache:
                self._context_cache.popitem(last=False)
            self._context_cache[context_hash] = context
        if context:
            final_prompt = f"Context:\n{context}\n\n{prompt}"
        else:
            final_prompt = prompt
        return {
            "prompt": final_prompt,
            "context": context,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "prefer_json": prefer_json,
            "metadata": meta,
            "budget_ok": budget_ok,
        }

    @staticmethod
    def _apply_budget_meta(
        active_budget: TokenBudget | None,
        task_id: str | None,
        estimated_total: int,
        meta: dict[str, Any],
    ) -> bool:
        if not active_budget:
            return True
        budget_ok = active_budget.check_task(task_id or "unknown", estimated_total)
        meta["budget_remaining"] = active_budget.remaining
        meta["budget_ok"] = budget_ok
        if not budget_ok:
            logger.warning(
                "[TokenOptimizer] Task %s would exceed budget (estimated %s tokens, remaining %s)",
                task_id,
                estimated_total,
                active_budget.remaining,
            )
        return budget_ok

    def summarise_results(self, results: list[dict[str, Any]], max_chars: int = 2000) -> str:
        """Summarise a list of task results for inclusion in subsequent prompts.

        Prevents context explosion when many tasks have completed.

        Args:
            results: List of task result dicts or strings from previous
                execution steps.
            max_chars: Maximum character length of the combined summary string.

        Returns:
            A compact bullet-point summary of all results, truncated to
            max_chars if needed. Returns an empty string when results is empty.
        """
        if not results:
            return ""

        summaries = []
        for r in results:
            if isinstance(r, dict):
                desc = r.get("description", r.get("task_id", "task"))[:60]
                out = r.get("output", r.get("result", ""))
                if isinstance(out, dict):
                    # Take only top-level keys and short values
                    out_str = "; ".join(f"{k}: {str(v)[:80]}" for k, v in list(out.items())[:5] if v)
                else:
                    out_str = str(out)[:200]
                summaries.append(f"- {desc}: {out_str}")
            elif isinstance(r, str):
                summaries.append(f"- {r[:200]}")

        combined = "\n".join(summaries)
        if len(combined) > max_chars:
            combined = combined[:max_chars] + "\n[... additional results truncated ...]"
        return combined


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_token_optimizer: TokenOptimizer | None = None
_token_optimizer_lock = threading.Lock()


def get_token_optimizer() -> TokenOptimizer:
    """Get or create the global TokenOptimizer singleton.

    Returns:
        The singleton TokenOptimizer instance shared across all subsystems.
    """
    global _token_optimizer
    if _token_optimizer is None:
        with _token_optimizer_lock:
            if _token_optimizer is None:
                _token_optimizer = TokenOptimizer()
    return _token_optimizer
