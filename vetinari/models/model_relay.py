"""Model Relay — config-based catalog with policy-driven selection.

Provides :class:`ModelRelay` for managing a YAML-based model catalog
and selecting models by task type using configurable routing policies.
Also defines :class:`ModelEntry` and :class:`RelayModelSelection` dataclasses.

Previously consolidated into ``dynamic_model_router.py``, now extracted
into its own module for separation of concerns.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vetinari.learning.atomic_writers import write_yaml_atomic
from vetinari.models.dynamic_model_router import ModelStatus
from vetinari.models.model_router_types import RouterTypePolicy
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


# Re-export RouterTypePolicy as RoutingPolicy for consistency with test patches
RoutingPolicy = RouterTypePolicy


# =====================================================================
# ModelEntry (used by web_ui catalog endpoints)
# =====================================================================


@dataclass
class ModelEntry:
    """Catalog entry for a model."""

    model_id: str
    provider: str
    display_name: str
    capabilities: list[str] = field(default_factory=list)
    context_window: int = 4096
    latency_hint: str = "medium"
    privacy_level: str = "local"
    memory_requirements_gb: float = 0.0
    cost_per_1k_tokens: float = 0.0
    status: str = ModelStatus.AVAILABLE.value
    endpoint: str = ""
    current_load: float = 0.0

    def __repr__(self) -> str:
        return (
            f"ModelEntry(model_id={self.model_id!r}, provider={self.provider!r}, "
            f"status={self.status!r}, latency_hint={self.latency_hint!r})"
        )

    def to_dict(self) -> dict:
        """Serialize to dictionary.

        Returns:
            Dict representation of this entry.
        """
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ModelEntry:
        """Deserialize from dictionary.

        Args:
            data: Dictionary with model entry fields.

        Returns:
            A new ModelEntry instance.
        """
        return cls(
            model_id=data.get("model_id", ""),
            provider=data.get("provider", "local"),
            display_name=data.get("display_name", data.get("model_id", "")),
            capabilities=data.get("capabilities", []),
            context_window=data.get("context_window", 4096),
            latency_hint=data.get("latency_hint", "medium"),
            privacy_level=data.get("privacy_level", "local"),
            memory_requirements_gb=data.get("memory_requirements_gb", 0.0),
            cost_per_1k_tokens=data.get("cost_per_1k_tokens", 0.0),
            status=data.get("status", ModelStatus.AVAILABLE.value),
            endpoint=data.get("endpoint", ""),
            current_load=data.get("current_load", 0.0),
        )


# =====================================================================
# RelayModelSelection — lightweight selection result
# =====================================================================


@dataclass(frozen=True, slots=True)
class RelayModelSelection:
    """Lightweight selection result used by the relay / web_ui catalog API."""

    model_id: str
    provider: str
    endpoint: str
    reasoning: str
    confidence: float
    latency_estimate: str

    def __repr__(self) -> str:
        return (
            f"RelayModelSelection(model_id={self.model_id!r}, provider={self.provider!r}, "
            f"confidence={self.confidence!r})"
        )

    def to_dict(self) -> dict:
        """Serialize to dictionary.

        Returns:
            Dict representation of this selection.
        """
        return dataclass_to_dict(self)


# =====================================================================
# ModelRelay
# =====================================================================


class ModelRelay:
    """Config-based model catalog with policy-driven selection.

    Manages a YAML-based model catalog and selects models by task type
    using configurable routing policies (privacy, latency, cost weights).
    """

    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def get_instance(cls, config_path: str | None = None) -> ModelRelay:
        """Return the singleton ModelRelay instance.

        Args:
            config_path: Optional path to models config YAML.

        Returns:
            The ModelRelay singleton.
        """
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    # Same lock-around-create invariant used by vetinari.events.
                    cls._instance = cls(config_path)
        return cls._instance

    def __init__(self, config_path: str | None = None):
        if config_path is None:
            env_path = os.environ.get("VETINARI_MODELS_CONFIG", "")
            if env_path:
                config_path = Path(env_path)
            else:
                pkg_root = Path(__file__).parent.parent
                config_path = pkg_root / "config" / "models.yaml"

        self.config_path = Path(config_path)
        self.models: dict[str, ModelEntry] = {}
        self.policy = RouterTypePolicy()
        self.config_load_error = ""
        self._load_config()

    # ----- config I/O -----

    def _load_config(self) -> None:
        """Load model catalog from YAML config file."""
        if self.config_path.exists():
            try:
                with open(self.config_path, encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                if not isinstance(data, dict):
                    raise ValueError("model config must be a mapping")
                model_rows = data.get("models", [])
                if not isinstance(model_rows, list):
                    raise ValueError("model config models must be a list")
                for model_data in model_rows:
                    if not isinstance(model_data, dict):
                        raise ValueError("model config model rows must be mappings")
                    model = ModelEntry.from_dict(model_data)
                    self.models[model.model_id] = model
                if "policy" in data:
                    self.policy = RouterTypePolicy.from_dict(data["policy"])
            except Exception as e:
                logger.error("Error loading model config: %s", e)
                self.models.clear()
                self.config_load_error = str(e)
                return

        if not self.models and not self.config_path.exists():
            self._load_default_models()

    # This hardcoded catalog does not synchronize with ModelRegistry; keep it
    # as a fallback of last resort when no operator-managed config is present.
    def _load_default_models(self) -> None:
        """Populate catalog with built-in default models."""
        logger.warning(
            "Using hardcoded fallback model catalog (%s); supply a model relay config file to avoid "
            "catalog drift from ModelRegistry.",
            "qwen2.5-coder-7b, qwen2.5-72b, llama-3.3-70b, gpt-4o",
        )
        default_models = [
            ModelEntry(
                model_id="qwen2.5-coder-7b",
                provider="local",
                display_name="Qwen 2.5 Coder 7B",
                capabilities=["coding", "fast"],
                context_window=32768,
                latency_hint="fast",
                privacy_level="local",
                memory_requirements_gb=8,
                endpoint="",
            ),
            ModelEntry(
                model_id="qwen2.5-72b",
                provider="local",
                display_name="Qwen 2.5 72B",
                capabilities=["reasoning", "coding"],
                context_window=32768,
                latency_hint="medium",
                privacy_level="local",
                memory_requirements_gb=48,
                endpoint="",
            ),
            ModelEntry(
                model_id="llama-3.3-70b",
                provider="local",
                display_name="Llama 3.3 70B",
                capabilities=["reasoning", "coding"],
                context_window=32768,
                latency_hint="medium",
                privacy_level="local",
                memory_requirements_gb=48,
                endpoint="",
            ),
            ModelEntry(
                model_id="gpt-4o",
                provider="openai",
                display_name="GPT-4o",
                capabilities=["reasoning", "vision", "coding"],
                context_window=128000,
                latency_hint="medium",
                privacy_level="public",
                cost_per_1k_tokens=0.005,
                endpoint="https://api.openai.com/v1/chat/completions",
            ),
        ]
        for model in default_models:
            self.models[model.model_id] = model

    def reload_catalog(self) -> None:
        """Reload catalog from disk."""
        self.models.clear()
        self._load_config()

    def _save_config(self) -> None:
        """Persist current catalog and policy to YAML."""
        data = {
            "models": [m.to_dict() for m in self.models.values()],
            "policy": self.policy.to_dict(),
        }
        write_yaml_atomic(self.config_path, data)

    # ----- queries -----

    def get_available_models(self) -> list[ModelEntry]:
        """Return all models with status AVAILABLE."""
        return [m for m in self.models.values() if m.status == ModelStatus.AVAILABLE.value]

    def get_model(self, model_id: str) -> ModelEntry | None:
        """Return a model by ID, or None if not found."""
        return self.models.get(model_id)

    def get_all_models(self) -> list[ModelEntry]:
        """Return all models in the catalog."""
        return list(self.models.values())

    def get_policy(self) -> RouterTypePolicy:
        """Return the current routing policy."""
        return self.policy

    def set_policy(self, policy: RouterTypePolicy) -> None:
        """Set a new routing policy and persist.

        Args:
            policy: The new routing policy.
        """
        self.policy = policy
        self._save_config()

    # ----- selection -----

    def pick_model_for_task(self, task_type: str | None = None, context: dict | None = None) -> RelayModelSelection:
        """Select the best model for a given task type.

        Args:
            task_type: The task type (e.g. "coding", "reasoning", "vision").
            context: Optional context dict for selection hints.

        Returns:
            A RelayModelSelection with the chosen model details.
        """
        available = self.get_available_models()

        if not available:
            return RelayModelSelection(
                model_id="",
                provider="",
                endpoint="",
                reasoning="No available models",
                confidence=0.0,
                latency_estimate="unknown",
            )

        # Build capability requirements from context first (any task type),
        # then fall back to well-known task_type → capability mappings.
        # This ensures context-provided constraints are always honoured, not
        # just the three hard-coded task types that existed previously.
        required_caps: list[str] = []
        if context:
            required_caps = list(context.get("required_capabilities", []))
        if not required_caps and task_type:
            # Map common task type names to the matching capability tag
            _task_cap_map = {
                "coding": "coding",
                "reasoning": "reasoning",
                "vision": "vision",
            }
            cap = _task_cap_map.get(task_type)
            if cap:
                required_caps = [cap]

        candidates = available
        if required_caps:
            candidates = [m for m in available if any(cap in m.capabilities for cap in required_caps)]
        if not candidates:
            candidates = available
        if self.policy.max_cost_per_1k_tokens > 0:
            candidates = [
                m
                for m in candidates
                if m.cost_per_1k_tokens <= 0 or m.cost_per_1k_tokens <= self.policy.max_cost_per_1k_tokens
            ]
        if not candidates:
            return RelayModelSelection(
                model_id="",
                provider="",
                endpoint="",
                reasoning="No model within configured cost cap",
                confidence=0.0,
                latency_estimate="unknown",
            )
        local_candidates = [m for m in candidates if m.provider == "local" or m.privacy_level == "local"]
        local_first_applied = bool(self.policy.local_first and local_candidates)
        if local_first_applied:
            candidates = local_candidates

        scored = []
        for model in candidates:
            score = self._score_model(model)
            scored.append((model, score))
        scored.sort(key=lambda x: x[1], reverse=True)

        best = scored[0][0] if scored else None
        if not best:
            return RelayModelSelection(
                model_id="",
                provider="",
                endpoint="",
                reasoning="No suitable model found",
                confidence=0.0,
                latency_estimate="unknown",
            )

        return RelayModelSelection(
            model_id=best.model_id,
            provider=best.provider,
            endpoint=best.endpoint,
            reasoning=self._get_selection_reason(
                best,
                task_type,
                local_first_applied=local_first_applied,
                local_available=bool(local_candidates),
            ),
            confidence=0.9 if best.privacy_level == "local" else 0.7,
            latency_estimate=best.latency_hint,
        )

    def _score_model(self, model: ModelEntry) -> float:
        """Score a model based on policy weights."""
        privacy_scores = {"local": 1.0, "private": 0.7, "public": 0.3}
        latency_scores = {"fast": 1.0, "medium": 0.6, "slow": 0.3}

        privacy = privacy_scores.get(model.privacy_level, 0.5)
        latency = latency_scores.get(model.latency_hint, 0.5)

        cost = 1.0
        if model.cost_per_1k_tokens > 0:
            cost = max(0, 1.0 - (model.cost_per_1k_tokens * 100))

        return (
            privacy * self.policy.privacy_weight + latency * self.policy.latency_weight + cost * self.policy.cost_weight
        )

    def _get_selection_reason(
        self,
        model: ModelEntry,
        task_type: str | None = None,
        *,
        local_first_applied: bool = False,
        local_available: bool = False,
    ) -> str:
        """Generate human-readable selection reasoning."""
        reasons = []
        if model.privacy_level == "local":
            reasons.append("local model selected")
        if self.policy.local_first and local_first_applied:
            reasons.append("local_first policy applied")
        elif self.policy.local_first and not local_available:
            reasons.append("local_first policy could not apply: no local candidate")
        if task_type:
            reasons.append(f"supports {task_type}")
        return ", ".join(reasons) if reasons else "best available model"

    # ----- mutations -----

    def add_model(self, model: ModelEntry) -> None:
        """Add a model to the catalog and persist.

        Args:
            model: The ModelEntry to add.
        """
        self.models[model.model_id] = model
        self._save_config()

    def remove_model(self, model_id: str) -> None:
        """Remove a model from the catalog and persist.

        Args:
            model_id: The model identifier to remove.
        """
        if model_id in self.models:
            del self.models[model_id]
            self._save_config()


# =====================================================================
# Singleton accessors
# =====================================================================


def get_model_relay() -> ModelRelay:
    """Lazily return the singleton ModelRelay with double-checked locking."""
    return ModelRelay.get_instance()


class _LazyModelRelay:
    """Proxy that resolves the ModelRelay singleton on first attribute access."""

    def __getattr__(self, name):
        """Delegate attribute access to the singleton.

        Args:
            name: Attribute name to look up.

        Returns:
            The attribute from the ModelRelay singleton.
        """
        return getattr(ModelRelay.get_instance(), name)

    def __repr__(self):
        """Return repr of the underlying ModelRelay."""
        return repr(ModelRelay.get_instance())


model_relay = _LazyModelRelay()
