"""Internal MetaAdapter prototype matching and persistence helpers."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from vetinari.boundary_guards import require_score_in_range
from vetinari.constants import VETINARI_STATE_DIR
from vetinari.learning.atomic_writers import write_json_atomic
from vetinari.utils.math_helpers import cosine_similarity
from vetinari.utils.serialization import dataclass_to_dict

if TYPE_CHECKING:
    from .meta_adapter import StrategyBundle, TaskPrototype

logger = logging.getLogger(__name__)
_META_ADAPTER_EMA_ALPHA = 0.2
_MAX_PROTOTYPES = 200
_MAX_STRATEGY_VALUES = 32
_PROTOTYPE_MATCH_THRESHOLD = 0.6


class _MetaAdapterInternals:
    """Prototype matching and state persistence methods for MetaAdapter."""

    def _find_best_prototype(
        self,
        query_embedding: list[float],
    ) -> tuple[TaskPrototype, float] | None:
        """Find the most similar prototype above threshold.

        Args:
            query_embedding: Embedding vector for the query.

        Returns:
            Tuple of (prototype, similarity) or None if no match above threshold.
        """
        with self._lock:
            best_score = 0.0
            best_proto: TaskPrototype | None = None

            for proto in self._prototypes.values():
                if not proto.embedding:
                    continue
                score = cosine_similarity(query_embedding, proto.embedding)
                if score > best_score:
                    best_score = score
                    best_proto = proto

            if best_proto is not None and best_score >= _PROTOTYPE_MATCH_THRESHOLD:
                return (best_proto, best_score)
            return None

    @staticmethod
    def _bundle_from_prototype(prototype: TaskPrototype) -> StrategyBundle:
        """Build a StrategyBundle from a prototype's learned strategies.

        Args:
            prototype: The matched TaskPrototype.

        Returns:
            StrategyBundle with best-performing values from the prototype.
        """
        temp = prototype.get_best_strategy("temperature")
        ctx = prototype.get_best_strategy("context_window")
        gran = prototype.get_best_strategy("decomposition_granularity")
        tmpl = prototype.get_best_strategy("prompt_template_variant")

        from .meta_adapter import StrategyBundle

        return StrategyBundle(
            temperature=float(temp) if temp is not None else 0.3,
            context_window=int(ctx) if ctx is not None else 4096,
            decomposition_granularity=str(gran) if gran is not None else "medium",
            prompt_template_variant=str(tmpl) if tmpl is not None else "standard",
        )

    @staticmethod
    def _bundle_from_thompson(agent_type: str, mode: str) -> StrategyBundle:
        """Build a StrategyBundle using Thompson Sampling selection.

        Args:
            agent_type: Agent type string.
            mode: Agent mode string.

        Returns:
            StrategyBundle with Thompson-selected values.
        """
        try:
            from vetinari.learning.model_selector import get_thompson_selector

            selector = get_thompson_selector()
            temp = selector.select_strategy(agent_type, mode, "temperature")
            ctx = selector.select_strategy(agent_type, mode, "context_window_size")
            gran = selector.select_strategy(agent_type, mode, "decomposition_granularity")
            tmpl = selector.select_strategy(agent_type, mode, "prompt_template_variant")

            from .meta_adapter import StrategyBundle

            return StrategyBundle(
                temperature=float(temp),
                context_window=int(ctx),
                decomposition_granularity=str(gran),
                prompt_template_variant=str(tmpl),
                source="thompson",
            )
        except Exception:
            logger.warning("Thompson Sampling unavailable, using defaults")
            from .meta_adapter import StrategyBundle

            return StrategyBundle(source="default")

    def _update_prototype(
        self,
        prototype: TaskPrototype,
        strategy: StrategyBundle,
        quality: float,
        mode: str,
    ) -> None:
        """Update a prototype with a new observation.

        Args:
            prototype: The prototype to update.
            strategy: The strategy that was used.
            quality: Observed quality score.
            mode: Agent mode that was used.
        """
        if not hasattr(self, "_state_lock"):
            self._state_lock = threading.Lock()
        with self._state_lock:
            # Running average for quality
            n = prototype.sample_count
            prototype.avg_quality = (prototype.avg_quality * n + quality) / (n + 1)
            prototype.sample_count = n + 1
            prototype.last_updated = datetime.now(timezone.utc).isoformat()

            if mode:
                prototype.preferred_mode = mode

            self._record_strategy_in_prototype(prototype, strategy, quality)

    @staticmethod
    def _record_strategy_in_prototype(
        prototype: TaskPrototype,
        strategy: StrategyBundle,
        quality: float,
    ) -> None:
        """Record strategy parameter effectiveness in a prototype.

        Args:
            prototype: The prototype to update.
            strategy: The strategy bundle used.
            quality: Quality score achieved.
        """
        quality = require_score_in_range(
            quality,
            "meta_adapter.strategy_quality",
            field_name="quality",
        )
        mappings = {
            "temperature": str(strategy.temperature),
            "context_window": str(strategy.context_window),
            "decomposition_granularity": strategy.decomposition_granularity,
            "prompt_template_variant": strategy.prompt_template_variant,
        }
        for key, value in mappings.items():
            if key not in prototype.successful_strategies:
                prototype.successful_strategies[key] = {}
            strategy_data = prototype.successful_strategies[key]
            if value not in strategy_data and len(strategy_data) >= _MAX_STRATEGY_VALUES:
                del strategy_data[next(iter(strategy_data))]
            # Exponential moving average for strategy quality tracking
            if value in strategy_data:
                old_avg = strategy_data[value]
                strategy_data[value] = (1 - _META_ADAPTER_EMA_ALPHA) * old_avg + _META_ADAPTER_EMA_ALPHA * quality
            else:
                strategy_data[value] = quality

    def _evict_least_used(self) -> None:
        """Remove the prototype with the lowest sample count."""
        if not self._prototypes:
            return
        worst_id = min(
            self._prototypes,
            key=lambda pid: self._prototypes[pid].sample_count,
        )
        del self._prototypes[worst_id]
        logger.debug("MetaAdapter: evicted prototype %s", worst_id)

    def _next_prototype_id(self) -> str:
        """Generate the next sequential prototype ID.

        Uses a monotonically-incrementing counter so that evictions (which
        decrease ``len(self._prototypes)``) cannot cause ID reuse.

        Returns:
            String like 'proto_001', 'proto_002', etc.
        """
        self._next_proto_counter += 1
        return f"proto_{self._next_proto_counter:03d}"

    @staticmethod
    def _get_embedder():
        """Get an embedding function (reuses episode memory's embedder).

        Returns:
            A callable that takes a string and returns a list of floats.
        """
        from vetinari.learning.episode_memory import _simple_embedding

        return _simple_embedding

    # â”€â”€ Persistence â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @staticmethod
    def _default_state_path() -> Path:
        """Resolve default path for meta-adapter state.

        Returns:
            Path to meta_adapter_state.json.
        """
        state_dir_env = os.environ.get("VETINARI_STATE_DIR", "")
        if state_dir_env:
            state_dir = Path(state_dir_env)
        else:
            state_dir = VETINARI_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir / "meta_adapter_state.json"

    def _load_state(self) -> None:
        """Load prototype state from JSON file."""
        try:
            if self._state_path.exists():
                with Path(self._state_path).open(encoding="utf-8") as f:
                    data = json.load(f)
                for pid, proto_data in data.items():
                    from .meta_adapter import TaskPrototype

                    self._prototypes[pid] = TaskPrototype(**proto_data)
                # Seed the monotonic counter from the highest ID seen so that
                # new IDs never collide with existing ones after a reload.
                for pid in data:
                    if pid.startswith("proto_"):
                        try:
                            n = int(pid[len("proto_") :])
                        except ValueError:
                            n = None
                        if n is not None and n > self._next_proto_counter:
                            self._next_proto_counter = n
                logger.debug(
                    "MetaAdapter: loaded %d prototypes from %s",
                    len(self._prototypes),
                    self._state_path,
                )
        except Exception:
            logger.warning(
                "MetaAdapter: could not load state from %s",
                self._state_path,
                exc_info=True,
            )

    def _save_state(self) -> None:
        """Persist prototype state to JSON file."""
        try:
            data = {pid: dataclass_to_dict(proto) for pid, proto in self._prototypes.items()}
            write_json_atomic(Path(self._state_path), data, indent=2)
        except Exception:
            logger.warning(
                "MetaAdapter: could not save state to %s",
                self._state_path,
                exc_info=True,
            )


# â”€â”€ Module-level singleton â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
