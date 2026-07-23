"""Per-agent training — each agent can specialise through targeted training.

Tracks which agents need training most urgently, provides dataset configuration
recommendations per agent type, and records training history to a persistent
JSONL log.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vetinari.boundary_guards import require_nonempty
from vetinari.constants import get_user_dir
from vetinari.training.continual_learning import ReplayBuffer
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# Function to get the path to the persistent training history log
def _get_history_path() -> Path:
    """Return the persistent training history log path, using get_user_dir() for testability."""
    return get_user_dir() / "agent_training_history.jsonl"


# Default ordered priority list used when live metrics are unavailable
_DEFAULT_PRIORITY_ORDER = [
    AgentType.WORKER.value,
    AgentType.INSPECTOR.value,
    AgentType.FOREMAN.value,
]

# Weights for priority score calculation
_WEIGHT_REJECTION_RATE = 0.40
_WEIGHT_QUALITY_DECLINE = 0.30
_WEIGHT_DAYS_SINCE_TRAINING = 0.30

# Days since last training to normalise the staleness component (cap at 90 days)
_STALENESS_CAP_DAYS = 90
_HISTORY_RETENTION_DAYS = 365
_HISTORY_MAX_RECORDS = 10000


class AgentTrainer:
    """Manages per-agent training prioritisation, configuration, and history.

    Uses late imports for optional vetinari dependencies so the class remains
    importable even when the full runtime is not initialised.
    """

    def __init__(self) -> None:
        """Initialise the AgentTrainer with lazy-loaded dependencies."""
        self._feedback_loop: Any = None
        self._kaizen: Any = None
        self._replay_buffer = ReplayBuffer(max_size=1000)

    # ── Public API ──────────────────────────────────────────────────────────

    def get_training_priority(self) -> list[tuple[str, float]]:
        """Rank agents by training need.

        Priority is a weighted combination of:
        - rejection rate (40 %): how often the agent's output is rejected
        - quality decline rate (30 %): recent drop in quality scores
        - days since last training (30 %): staleness of the current model

        Falls back to a default ordering when the live metrics modules are
        unavailable.

        Returns:
            List of ``(agent_type_name, priority_score)`` tuples sorted in
            descending order by priority score.
        """
        try:
            metrics = self._collect_agent_metrics()
        except Exception as exc:
            logger.warning(
                "Could not collect live agent metrics; using default ordering: %s",
                exc,
            )
            return [(name, float(len(_DEFAULT_PRIORITY_ORDER) - i)) for i, name in enumerate(_DEFAULT_PRIORITY_ORDER)]

        scored: list[tuple[str, float]] = []
        for agent_name, m in metrics.items():
            rejection_component = _WEIGHT_REJECTION_RATE * float(m.get("rejection_rate", 0.0))
            quality_component = _WEIGHT_QUALITY_DECLINE * float(m.get("quality_decline_rate", 0.0))

            days_stale = float(m.get("days_since_training", 0.0))
            # Normalise to [0, 1] capped at _STALENESS_CAP_DAYS
            normalised_staleness = min(days_stale, _STALENESS_CAP_DAYS) / _STALENESS_CAP_DAYS
            staleness_component = _WEIGHT_DAYS_SINCE_TRAINING * normalised_staleness

            score = rejection_component + quality_component + staleness_component
            scored.append((agent_name, round(score, 4)))

        scored.sort(key=lambda x: x[1], reverse=True)
        logger.debug("Training priority computed for %d agents", len(scored))
        return scored

    def get_agent_dataset_config(self, agent_type: str) -> dict[str, Any]:
        """Return recommended dataset configuration for a given agent type.

        Args:
            agent_type: The agent type name (e.g. ``"WORKER"``, ``"FOREMAN"``).

        Returns:
            Dictionary with keys:
            - ``task_types``: list of task type strings to include
            - ``min_score``: minimum quality score threshold
            - ``max_examples``: maximum training example count
            - ``preferred_method``: ``"sft"`` or ``"dpo"``
        """
        configs: dict[str, dict[str, Any]] = {
            AgentType.FOREMAN.value: {
                "task_types": ["planning", "decomposition", "scheduling"],
                "min_score": 0.82,
                "max_examples": 2500,
                "preferred_method": "sft",
            },
            AgentType.WORKER.value: {
                "task_types": [
                    "coding",
                    "implementation",
                    "refactor",
                    "bug_fix",
                    "research",
                    "summarisation",
                    "information_retrieval",
                    "architecture",
                    "documentation",
                    "monitoring",
                    "synthesis",
                ],
                "min_score": 0.85,
                "max_examples": 8000,
                "preferred_method": "sft",
            },
            AgentType.INSPECTOR.value: {
                "task_types": ["code_review", "test_generation", "quality_check", "analysis", "testing"],
                "min_score": 0.80,
                "max_examples": 3000,
                "preferred_method": "dpo",
            },
        }

        config = configs.get(agent_type.upper())
        if config is None:
            logger.warning(
                "No dataset config found for agent type '%s'; returning generic defaults",
                agent_type,
            )
            return {
                "task_types": [],
                "min_score": 0.80,
                "max_examples": 2000,
                "preferred_method": "sft",
            }

        return dict(config)

    def prepare_training_data_with_replay(
        self,
        current_examples: list[dict[str, Any]],
        replay_sample_size: int = 50,
    ) -> list[dict[str, Any]]:
        """Combine current training examples with replay buffer samples.

        Mixes in past examples from the replay buffer to reduce catastrophic
        forgetting when fine-tuning on a new task. After combining, stores the
        current examples into the replay buffer for future rounds.

        Args:
            current_examples: Training examples from the current round.
            replay_sample_size: Maximum number of past examples to sample from
                the replay buffer. Clamped to the current buffer size.

        Returns:
            Combined list of current examples and replay samples, ready for
            the training pipeline.
        """
        # When current_examples is empty (replay-only rehearsal) we still want
        # up to replay_sample_size past examples; clamping to len(current_examples)
        # would give zero samples in that case (defect 6 fix).
        effective_replay_size = (
            replay_sample_size if not current_examples else min(replay_sample_size, len(current_examples))
        )
        replay_samples = self._replay_buffer.get_replay_batch(effective_replay_size)
        training_examples = current_examples + replay_samples

        # Store current examples for future replay rounds
        if current_examples:
            self._replay_buffer.add(current_examples)

        logger.info(
            "prepare_training_data_with_replay: current=%d, replay=%d, total=%d",
            len(current_examples),
            len(replay_samples),
            len(training_examples),
        )
        return training_examples

    def record_training(
        self,
        agent_type: str,
        model_path: str,
        metrics: dict[str, Any],
    ) -> None:
        """Record a completed training event to the persistent history log.

        Appends a single JSON line to ``~/.vetinari/agent_training_history.jsonl``
        containing the agent type, model path, timestamp, and arbitrary metrics.

        Args:
            agent_type: The agent type that was trained.
            model_path: Path or identifier for the resulting model.
            metrics: Dictionary of training metrics (e.g. eval scores, epoch count).
        """
        history_path = _get_history_path()
        history_path.parent.mkdir(parents=True, exist_ok=True)

        record: dict[str, Any] = {
            "agent_type": agent_type,
            "model_path": model_path,
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "metrics": metrics,
        }

        try:
            _prune_history_file(history_path)
            with history_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
            logger.info(
                "Training record appended for agent '%s' -> %s",
                agent_type,
                model_path,
            )
        except OSError as exc:
            logger.error(
                "Failed to write training record for '%s': %s",
                agent_type,
                exc,
            )

    def get_last_trained(self, agent_type: str) -> str | None:
        """Return the ISO timestamp of the most recent training run for an agent.

        Args:
            agent_type: The agent type to look up.

        Returns:
            ISO-format timestamp string, or None if no training has been recorded
            for this agent type.
        """
        records = self._load_history()
        target = agent_type.upper()
        latest_ts: str | None = None

        for record in records:
            if record.get("agent_type", "").upper() == target:
                ts = record.get("timestamp")
                if ts and (latest_ts is None or ts > latest_ts):
                    latest_ts = ts

        return latest_ts

    def get_stats(self) -> dict[str, Any]:
        """Return training history statistics per agent type.

        Returns:
            Dictionary mapping each known agent type to a sub-dict with:
            - ``total_runs``: number of recorded training runs
            - ``last_trained``: ISO timestamp of most recent run, or None
            - ``latest_model``: model path from most recent run, or None
        """
        records = self._load_history()

        # Initialise stats for all known agent types
        stats: dict[str, Any] = {
            name: {"total_runs": 0, "last_trained": None, "latest_model": None} for name in _DEFAULT_PRIORITY_ORDER
        }

        for record in records:
            agent = record.get("agent_type", "").upper()
            if agent not in stats:
                stats[agent] = {"total_runs": 0, "last_trained": None, "latest_model": None}

            entry = stats[agent]
            entry["total_runs"] += 1

            ts = record.get("timestamp")
            if ts and (entry["last_trained"] is None or ts > entry["last_trained"]):
                entry["last_trained"] = ts
                entry["latest_model"] = record.get("model_path")

        return stats

    # ── Private helpers ──────────────────────────────────────────────────────

    def _load_history(self) -> list[dict[str, Any]]:
        """Load and parse all records from the training history log.

        Returns:
            List of parsed record dictionaries. Malformed lines are skipped.
        """
        history_path = _get_history_path()
        if not history_path.exists():
            return []

        records: list[dict[str, Any]] = []
        try:
            with history_path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError as exc:
                        logger.warning("Skipping malformed training history line: %s", exc)
        except OSError as exc:
            logger.warning("Could not read training history from %s: %s", history_path, exc)

        return records

    def _base_agent_metrics_from_history(self) -> dict[str, dict[str, float]]:
        now = datetime.now(tz=timezone.utc)
        metrics: dict[str, dict[str, float]] = {}
        for agent_name in _DEFAULT_PRIORITY_ORDER:
            last_ts = self.get_last_trained(agent_name)
            days_stale = float("inf")
            try:
                raw_ts = require_nonempty(last_ts, field_name="last_trained_at")
                last_dt = datetime.fromisoformat(raw_ts)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                days_stale = (now - last_dt).total_seconds() / 86400.0
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "agent_trainer: corrupt last_trained_at=%r; treating as never-trained (days_stale=inf)",
                    last_ts,
                    exc_info=exc,
                )
            metrics[agent_name] = {
                "rejection_rate": 0.0,
                "quality_decline_rate": 0.0,
                "days_since_training": days_stale,
            }
        return metrics

    def _enrich_feedback_metrics(self, metrics: dict[str, dict[str, float]]) -> None:
        try:
            from vetinari.learning.feedback_loop import get_feedback_loop

            feedback = get_feedback_loop()
            try:
                from vetinari.memory import get_memory_store

                memory = get_memory_store()
                for agent_name in list(metrics.keys()):
                    self._apply_agent_performance(metrics, agent_name, memory)
            except ImportError:
                logger.debug("agent performance metrics module not available")
            _ = feedback
        except ImportError:
            logger.debug("feedback_loop not available; skipping live rejection rate enrichment")

    @staticmethod
    def _apply_agent_performance(metrics: dict[str, dict[str, float]], agent_name: str, memory: Any) -> None:
        try:
            perf = memory.get_agent_performance(agent_name)
            if perf:
                metrics[agent_name]["rejection_rate"] = float(perf.get("rejection_rate", 0.0))
                metrics[agent_name]["quality_decline_rate"] = float(perf.get("quality_decline_rate", 0.0))
        except Exception as agent_exc:
            logger.warning("Could not fetch performance metrics for agent '%s': %s", agent_name, agent_exc)

    @staticmethod
    def _enrich_kaizen_metrics(metrics: dict[str, dict[str, float]]) -> None:
        try:
            from vetinari.kaizen.aggregator import ImprovementAggregator

            report = ImprovementAggregator().generate_weekly_report()
            for rec in getattr(report, "recommendations", []):
                agent = str(rec.get("agent", "")).upper() if isinstance(rec, dict) else ""
                if agent in metrics:
                    decline = float(rec.get("quality_decline_rate", 0.0)) if isinstance(rec, dict) else 0.0
                    metrics[agent]["quality_decline_rate"] = max(metrics[agent]["quality_decline_rate"], decline)
        except (ImportError, Exception) as exc:
            logger.warning("kaizen aggregator not available or failed: %s", exc)

    def _collect_agent_metrics(self) -> dict[str, dict[str, float]]:
        """Collect live rejection rate and quality decline metrics per agent.

        Queries the feedback loop and kaizen aggregator when available, then
        merges with days-since-training from local history.

        Returns:
            Mapping of agent_type_name to a dict with keys:
            ``rejection_rate``, ``quality_decline_rate``, ``days_since_training``.

        Raises:
            ImportError: If neither feedback_loop nor kaizen modules are importable.
        """
        metrics = self._base_agent_metrics_from_history()
        self._enrich_feedback_metrics(metrics)
        self._enrich_kaizen_metrics(metrics)
        return metrics

    def record_retraining_signal(self, event: Any) -> None:
        """Handle a RetrainingRecommended event from the EventBus.

        Logs the signal and records it in training history so that the
        training priority scoring can account for external retraining pressure.

        Args:
            event: A ``RetrainingRecommended`` event from the EventBus.
        """
        metric = getattr(event, "metric", "unknown")
        predicted_quality = getattr(event, "predicted_quality", 0.0)
        days_until_breach = getattr(event, "days_until_breach", -1)
        forecast_method = getattr(event, "forecast_method_used", "unknown")

        logger.warning(
            "[AgentTrainer] Retraining signal received — metric=%s, "
            "predicted_quality=%.3f, days_until_breach=%d, method=%s",
            metric,
            predicted_quality,
            days_until_breach,
            forecast_method,
        )

        # Record the signal in training history so priority scoring picks it up
        self.record_training(
            agent_type="SYSTEM",
            model_path="retraining_signal",
            metrics={
                "signal_type": "RetrainingRecommended",
                "metric": metric,
                "predicted_quality": predicted_quality,
                "days_until_breach": days_until_breach,
                "forecast_method": forecast_method,
            },
        )


def _prune_history_file(
    history_path: Path,
    *,
    retention_days: int = _HISTORY_RETENTION_DAYS,
    max_records: int = _HISTORY_MAX_RECORDS,
) -> int:
    """Bound the persistent agent-training history JSONL file."""
    if not history_path.exists():
        return 0
    cutoff_ts = datetime.now(timezone.utc).timestamp() - max(retention_days, 0) * 86400
    kept: list[str] = []
    removed = 0
    for line in history_path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
            timestamp = datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
        except (json.JSONDecodeError, ValueError, TypeError):
            logger.warning("Dropping malformed agent-training history row", exc_info=True)
            removed += 1
            continue
        if timestamp.timestamp() < cutoff_ts:
            removed += 1
            continue
        kept.append(json.dumps(row) + "\n")
    if max_records > 0 and len(kept) > max_records:
        removed += len(kept) - max_records
        kept = kept[-max_records:]
    history_path.write_text("".join(kept), encoding="utf-8")
    return removed


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------

_agent_trainer: AgentTrainer | None = None
_agent_trainer_lock = threading.Lock()


def get_agent_trainer() -> AgentTrainer:
    """Return the process-wide AgentTrainer singleton (thread-safe).

    Returns:
        The shared AgentTrainer instance.
    """
    global _agent_trainer
    if _agent_trainer is None:
        with _agent_trainer_lock:
            if _agent_trainer is None:
                _agent_trainer = AgentTrainer()
    return _agent_trainer
