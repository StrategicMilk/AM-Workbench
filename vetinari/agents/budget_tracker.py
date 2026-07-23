"""Agent Budget Tracker — thread-safe resource accounting for hierarchical agents.

Enforces token, iteration, cost, and delegation budgets per-agent so that
recursive Foreman chains cannot exhaust system resources.  Conservation law:
a child's delegation_budget must be <= parent's remaining delegation_budget - 1.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from vetinari.agents.contracts import AgentSpec


# Default budget limits applied when no explicit spec overrides are present
DEFAULT_TOKEN_BUDGET = 32_000  # tokens per agent invocation
DEFAULT_ITERATION_CAP = 10  # maximum retry/iteration loops
DEFAULT_COST_BUDGET_USD = 1.0  # USD
DEFAULT_DELEGATION_BUDGET = 5  # maximum recursive delegation depth


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """Point-in-time view of an agent's budget consumption.

    All numeric fields are non-negative.  ``is_exhausted`` is True when ANY
    limit has been reached — callers should check this before dispatching work.

    Args:
        tokens_used: Tokens consumed so far.
        token_budget: Maximum tokens allowed.
        iterations_used: Iteration/retry loops consumed.
        iteration_cap: Maximum iterations allowed.
        cost_used_usd: Dollars consumed so far.
        cost_budget_usd: Maximum dollars allowed.
        delegations_used: Delegation hops consumed.
        delegation_budget: Maximum delegation hops allowed.
        is_exhausted: True if any budget is at or above its limit.
    """

    tokens_used: int = 0
    token_budget: int = DEFAULT_TOKEN_BUDGET
    iterations_used: int = 0
    iteration_cap: int = DEFAULT_ITERATION_CAP
    cost_used_usd: float = 0.0
    cost_budget_usd: float = DEFAULT_COST_BUDGET_USD
    delegations_used: int = 0
    delegation_budget: int = DEFAULT_DELEGATION_BUDGET
    is_exhausted: bool = False

    def __repr__(self) -> str:
        return (
            f"BudgetSnapshot(tokens={self.tokens_used}/{self.token_budget}, "
            f"iterations={self.iterations_used}/{self.iteration_cap}, "
            f"cost=${self.cost_used_usd:.4f}/${self.cost_budget_usd:.2f}, "
            f"delegations={self.delegations_used}/{self.delegation_budget}, "
            f"exhausted={self.is_exhausted!r})"
        )


def _spec_budget_value(spec: AgentSpec, field_name: str, default: int | float) -> int | float:
    """Read an AgentSpec budget field while preserving explicit zero values."""
    value = getattr(spec, field_name, default)
    return default if value is None else value


class BudgetTracker:
    """Thread-safe budget tracker for a single agent invocation.

    Tracks token usage, iteration loops, cost, and delegation depth.
    All mutations are protected by an internal lock so the tracker is
    safe to call from multiple threads (e.g. parallel tool executions).

    Conservation law for delegation:
        ``child.delegation_budget <= parent.remaining_delegations - 1``
    This prevents unbounded recursive Foreman chains.

    Example::

        tracker = BudgetTracker.from_agent_spec(spec)
        tracker.record_tokens(1500)
        tracker.record_iteration()
        if tracker.is_exhausted():
            raise BudgetExhaustedError("token budget exceeded")
    """

    def __init__(
        self,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        iteration_cap: int = DEFAULT_ITERATION_CAP,
        cost_budget_usd: float = DEFAULT_COST_BUDGET_USD,
        delegation_budget: int = DEFAULT_DELEGATION_BUDGET,
        agent_name: str = "",
    ) -> None:
        """Initialise tracker with explicit budget limits.

        Args:
            token_budget: Maximum tokens this agent may consume.
            iteration_cap: Maximum retry/iteration loops allowed.
            cost_budget_usd: Maximum cost in USD.
            delegation_budget: Maximum delegation hops this agent may spawn.
            agent_name: Human-readable label used in log messages.
        """
        self._token_budget = token_budget
        self._iteration_cap = iteration_cap
        self._cost_budget_usd = cost_budget_usd
        self._delegation_budget = delegation_budget
        self._agent_name = agent_name

        self._tokens_used: int = 0
        self._iterations_used: int = 0
        self._cost_used_usd: float = 0.0
        self._delegations_used: int = 0

        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_agent_spec(cls, spec: AgentSpec) -> BudgetTracker:
        """Build a tracker pre-configured from an AgentSpec.

        Falls back to module-level defaults for any missing budget field,
        ensuring forward compatibility as new fields are added to AgentSpec.

        Args:
            spec: The agent specification carrying budget constraints.

        Returns:
            A BudgetTracker initialised from the spec's budget fields.
        """
        token_budget = _spec_budget_value(spec, "token_budget", DEFAULT_TOKEN_BUDGET)
        iteration_cap = _spec_budget_value(spec, "iteration_cap", DEFAULT_ITERATION_CAP)
        cost_budget_usd = _spec_budget_value(spec, "cost_budget_usd", DEFAULT_COST_BUDGET_USD)
        delegation_budget = _spec_budget_value(spec, "delegation_budget", DEFAULT_DELEGATION_BUDGET)
        return cls(
            token_budget=token_budget,
            iteration_cap=iteration_cap,
            cost_budget_usd=cost_budget_usd,
            delegation_budget=delegation_budget,
            agent_name=getattr(spec, "name", ""),
        )

    # ------------------------------------------------------------------
    # Mutation methods (all thread-safe)
    # ------------------------------------------------------------------

    def record_tokens(self, count: int) -> None:
        """Add *count* tokens to the running total.

        Args:
            count: Number of tokens to record.  Negative values are ignored.
        """
        if count <= 0:
            return
        with self._lock:
            self._tokens_used += count
        if self._tokens_used >= self._token_budget:
            logger.warning(
                "[BudgetTracker] %s token budget exhausted (%d/%d)",
                self._agent_name,
                self._tokens_used,
                self._token_budget,
            )

    def record_usage(self, tokens: int, cost_usd: float = 0.0) -> None:
        """Record token usage and optional USD cost for one model call.

        Args:
            tokens: Tokens value consumed by record_usage().
            cost_usd: Cost usd value consumed by record_usage().
        """
        self.record_tokens(tokens)
        self.record_cost(cost_usd)

    def record_iteration(self) -> None:
        """Increment the iteration counter by one."""
        with self._lock:
            self._iterations_used += 1
        if self._iterations_used >= self._iteration_cap:
            logger.warning(
                "[BudgetTracker] %s iteration cap reached (%d/%d)",
                self._agent_name,
                self._iterations_used,
                self._iteration_cap,
            )

    def record_cost(self, usd: float) -> None:
        """Add *usd* to the running cost total.

        Args:
            usd: Cost in US dollars to record.  Negative values are ignored.
        """
        if usd <= 0.0:
            return
        with self._lock:
            self._cost_used_usd += usd
        if self._cost_used_usd >= self._cost_budget_usd:
            logger.warning(
                "[BudgetTracker] %s cost budget exhausted ($%.4f/$%.2f)",
                self._agent_name,
                self._cost_used_usd,
                self._cost_budget_usd,
            )

    def record_delegation(self) -> None:
        """Increment the delegation counter by one."""
        with self._lock:
            self._delegations_used += 1
        if self._delegations_used >= self._delegation_budget:
            logger.warning(
                "[BudgetTracker] %s delegation budget exhausted (%d/%d)",
                self._agent_name,
                self._delegations_used,
                self._delegation_budget,
            )

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def is_exhausted(self) -> bool:
        """Return True if any budget dimension has reached its limit.

        Returns:
            True when tokens, iterations, cost, or delegations are at or
            above their configured limits.
        """
        with self._lock:
            return (
                self._tokens_used >= self._token_budget
                or self._iterations_used >= self._iteration_cap
                or self._cost_used_usd >= self._cost_budget_usd
                or self._delegations_used >= self._delegation_budget
            )

    def remaining_delegations(self) -> int:
        """Return how many more delegation hops this agent may make.

        Returns:
            Non-negative integer of remaining delegation budget.
        """
        with self._lock:
            return max(0, self._delegation_budget - self._delegations_used)

    def child_delegation_budget(self) -> int:
        """Return the delegation budget a child agent should receive.

        Conservation law: child_budget = remaining_delegations - 1.
        This guarantees the total delegation chain is bounded.

        Returns:
            Delegation budget for a child agent, minimum 0.
        """
        return max(0, self.remaining_delegations() - 1)

    def snapshot(self) -> BudgetSnapshot:
        """Return a frozen snapshot of current budget state.

        Returns:
            BudgetSnapshot capturing all current counters and limits.
        """
        with self._lock:
            exhausted = (
                self._tokens_used >= self._token_budget
                or self._iterations_used >= self._iteration_cap
                or self._cost_used_usd >= self._cost_budget_usd
                or self._delegations_used >= self._delegation_budget
            )
            return BudgetSnapshot(
                tokens_used=self._tokens_used,
                token_budget=self._token_budget,
                iterations_used=self._iterations_used,
                iteration_cap=self._iteration_cap,
                cost_used_usd=self._cost_used_usd,
                cost_budget_usd=self._cost_budget_usd,
                delegations_used=self._delegations_used,
                delegation_budget=self._delegation_budget,
                is_exhausted=exhausted,
            )

    def to_dict(self) -> dict[str, Any]:
        """Serialize the current budget state to a JSON-safe dictionary.

        Returns:
            Dictionary with all budget fields and their current values.
        """
        snap = self.snapshot()
        return {
            "tokens_used": snap.tokens_used,
            "token_budget": snap.token_budget,
            "iterations_used": snap.iterations_used,
            "iteration_cap": snap.iteration_cap,
            "cost_used_usd": snap.cost_used_usd,
            "cost_budget_usd": snap.cost_budget_usd,
            "delegations_used": snap.delegations_used,
            "delegation_budget": snap.delegation_budget,
            "is_exhausted": snap.is_exhausted,
        }
