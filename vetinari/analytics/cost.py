"""Cost Attribution — vetinari.analytics.cost  (Phase 5).

Tracks token and compute costs per agent, task, provider and model.

Pricing is configurable per provider/model pair ($/1k input-tokens,
$/1k output-tokens, $/request).  A built-in default table covers common
OpenAI and local-model scenarios so the module is useful out-of-the-box.

Usage
-----
    from vetinari.analytics.cost import get_cost_tracker, CostEntry, ModelPricing

    tracker = get_cost_tracker()

    # Override pricing for a model
    tracker.set_pricing("openai", "gpt-4",
                        ModelPricing(input_per_1k=0.03, output_per_1k=0.06))

    # Record a call
    tracker.record(CostEntry(
        agent="builder",
        task_id="task-001",
        provider="openai",
        model="gpt-4",
        input_tokens=500,
        output_tokens=200,
    ))

    report = tracker.get_report()
    logger.debug(report.total_cost_usd)
    logger.debug(report.by_agent)
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

from vetinari.analytics.cost_models import _DEFAULT_PRICING, CostEntry, CostReport, ModelPricing, require_model_pricing
from vetinari.analytics.cost_storage import (
    annotate_correlated_span,
    build_cost_persistence_config,
    entry_with_correlation,
    load_persisted_cost_entries,
    persist_budget_alert,
    persist_cost_entry,
)

logger = logging.getLogger(__name__)

_COST_HISTORY_MAX_ENTRIES = 1000  # In-memory report window; durable JSONL keeps restart history.


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class CostTracker:
    """Thread-safe cost attribution tracker.  Singleton — use ``get_cost_tracker()``."""

    _instance: CostTracker | None = None
    _class_lock = threading.Lock()

    def __new__(cls) -> CostTracker:
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    def _setup(self) -> None:
        self._lock = threading.RLock()
        self._persistence = build_cost_persistence_config()
        self._entries_path = self._persistence.entries_path
        self._budget_limit_usd = self._persistence.budget_limit_usd
        self._entries: deque[CostEntry] = deque(maxlen=_COST_HISTORY_MAX_ENTRIES)
        self._pricing: dict[str, ModelPricing] = dict(_DEFAULT_PRICING)
        load_persisted_cost_entries(self._entries, self._entries_path, self._persistence.backup_count)

    # ------------------------------------------------------------------
    # Pricing
    # ------------------------------------------------------------------

    def set_pricing(self, provider: str, model: str, pricing: ModelPricing) -> None:
        """Set pricing.

        Args:
            provider: The provider.
            model: The model.
            pricing: The pricing.
        """
        with self._lock:
            self._pricing[f"{provider}:{model}"] = pricing

    def get_pricing(self, provider: str, model: str) -> ModelPricing:
        """Get pricing.

        Args:
            provider: The provider.
            model: The model.

        Returns:
            The ModelPricing result.
        """
        with self._lock:
            key = f"{provider}:{model}"
            if key in self._pricing:
                return require_model_pricing(key, self._pricing)
            # Try wildcard for provider
            wildcard = f"{provider}:*"
            if wildcard in self._pricing:
                return require_model_pricing(wildcard, self._pricing)
            return ModelPricing()  # free / unknown

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, entry: CostEntry) -> CostEntry:
        """Record a cost entry.  ``entry.cost_usd`` is calculated automatically.

        using the configured pricing if it is not already set (> 0).

        Returns:
            The CostEntry result.
        """
        with self._lock:
            entry = entry_with_correlation(entry)
            if entry.cost_usd is None:
                # Only recalculate when caller did not provide an explicit cost.
                # A caller-supplied cost_usd=0.0 is preserved as-is.
                pricing = self.get_pricing(entry.provider, entry.model)
                entry = CostEntry(
                    provider=entry.provider,
                    model=entry.model,
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                    agent=entry.agent,
                    task_id=entry.task_id,
                    project_id=entry.project_id,
                    trace_id=entry.trace_id,
                    span_id=entry.span_id,
                    timestamp=entry.timestamp,
                    cost_usd=pricing.compute(entry.input_tokens, entry.output_tokens),
                    latency_ms=entry.latency_ms,
                )
            projected_total = self._current_total_cost_locked() + (entry.cost_usd or 0.0)
            persist_cost_entry(entry, self._persistence)
            self._entries.append(entry)
            annotate_correlated_span(entry)
            if projected_total > self._budget_limit_usd:
                persist_budget_alert(entry, projected_total, self._persistence)
                logger.warning(
                    "Cost budget exceeded: projected_total_usd=%.6f budget_limit_usd=%.6f provider=%s model=%s",
                    projected_total,
                    self._budget_limit_usd,
                    entry.provider,
                    entry.model,
                )
            logger.debug(
                "Cost recorded: %s/%s  in=%d out=%d  $%.6f  agent=%s",
                entry.provider,
                entry.model,
                entry.input_tokens,
                entry.output_tokens,
                entry.cost_usd,
                entry.agent,
            )
            return entry

    def _current_total_cost_locked(self) -> float:
        """Return the current in-memory cost total while the caller holds the lock."""
        return sum(e.cost_usd or 0.0 for e in self._entries)

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def get_report(
        self,
        agent: str | None = None,
        task_id: str | None = None,
        project_id: str | None = None,
        since: float | None = None,
    ) -> CostReport:
        """Build an aggregated cost report, optionally filtered.

        Args:
            agent:   Only include entries from this agent.
            task_id: Only include entries for this task.
            project_id: Only include entries for this project.
            since:   Only include entries with timestamp >= since (unix epoch).

        Returns:
            CostReport with total spend, token counts, and breakdowns by
            agent, provider, model (``provider:model`` key), and task.
            All monetary values are in USD.
        """
        with self._lock:
            entries = list(self._entries)

        if agent:
            entries = [e for e in entries if e.agent == agent]
        if task_id:
            entries = [e for e in entries if e.task_id == task_id]
        if project_id:
            entries = [e for e in entries if e.project_id == project_id]
        if since is not None:
            entries = [e for e in entries if e.timestamp >= since]

        total_cost = sum(e.cost_usd or 0.0 for e in entries)
        total_tokens = sum(e.input_tokens + e.output_tokens for e in entries)

        by_agent: dict[str, float] = {}
        by_provider: dict[str, float] = {}
        by_model: dict[str, float] = {}
        by_task: dict[str, float] = {}
        by_project: dict[str, float] = {}

        for e in entries:
            key_a = e.agent or "unknown"
            key_p = e.provider or "unknown"
            key_m = f"{e.provider}:{e.model}"
            key_t = e.task_id or "unknown"
            key_project = e.project_id or "unknown"
            cost = e.cost_usd or 0.0

            by_agent[key_a] = by_agent.get(key_a, 0.0) + cost
            by_provider[key_p] = by_provider.get(key_p, 0.0) + cost
            by_model[key_m] = by_model.get(key_m, 0.0) + cost
            by_task[key_t] = by_task.get(key_t, 0.0) + cost
            by_project[key_project] = by_project.get(key_project, 0.0) + cost

        return CostReport(
            total_cost_usd=total_cost,
            total_tokens=total_tokens,
            total_requests=len(entries),
            by_agent=by_agent,
            by_provider=by_provider,
            by_model=by_model,
            by_task=by_task,
            by_project=by_project,
            entries=len(entries),
        )

    def get_top_agents(self, n: int = 5) -> list[dict[str, Any]]:
        """Rank agents by accumulated metered cost.

        Returns:
            List of dicts sorted by descending cost, each with ``agent``
            (agent name) and ``cost_usd`` (total spend) keys.
        """
        report = self.get_report()
        ranked = sorted(report.by_agent.items(), key=lambda x: x[1], reverse=True)
        return [{"agent": k, "cost_usd": v} for k, v in ranked[:n]]

    def get_top_models(self, n: int = 5) -> list[dict[str, Any]]:
        """Rank provider/model combinations by accumulated metered cost.

        Returns:
            List of dicts sorted by descending cost, each with ``model``
            (``provider:model`` key) and ``cost_usd`` (total spend) keys.
        """
        report = self.get_report()
        ranked = sorted(report.by_model.items(), key=lambda x: x[1], reverse=True)
        return [{"model": k, "cost_usd": v} for k, v in ranked[:n]]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Summarise the current tracker state without computing a full report.

        Returns:
            Dictionary with ``total_entries`` (number of recorded cost entries)
            ``configured_models`` (number of pricing rules loaded), and
            budget and persistence fields so callers can distinguish healthy
            accounting from cap crossings without treating unknown state as
            free usage.
        """
        with self._lock:
            total_cost = self._current_total_cost_locked()
            return {
                "total_entries": len(self._entries),
                "configured_models": len(self._pricing),
                "budget_limit_usd": self._budget_limit_usd,
                "budget_exceeded": total_cost > self._budget_limit_usd,
                "persistence_path": str(self._entries_path),
            }

    def get_summary(self) -> dict[str, Any]:
        """Return aggregated cost summary for the token-stats endpoint.

        Returns:
            Dictionary with ``total_cost_usd`` and ``by_model`` keys
            summarising all recorded cost entries.
        """
        with self._lock:
            total_cost = sum(e.cost_usd or 0.0 for e in self._entries)
            by_model: dict[str, dict[str, float]] = {}
            for entry in self._entries:
                key = f"{entry.provider}:{entry.model}" if entry.provider else entry.model
                if key not in by_model:
                    by_model[key] = {"cost_usd": 0.0, "tokens": 0}
                by_model[key]["cost_usd"] += entry.cost_usd or 0.0
                by_model[key]["tokens"] += entry.input_tokens + entry.output_tokens
            return {
                "total_cost_usd": round(total_cost, 6),
                "by_model": by_model,
            }

    def clear(self) -> None:
        """Clear for the current context."""
        with self._lock:
            self._entries.clear()


# ---------------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------------


def get_cost_tracker() -> CostTracker:
    """Return the singleton CostTracker instance, creating it if necessary.

    Returns:
        The shared CostTracker singleton used for all cost attribution.
    """
    return CostTracker()


def reset_cost_tracker() -> None:
    """Reset cost tracker."""
    with CostTracker._class_lock:
        CostTracker._instance = None
