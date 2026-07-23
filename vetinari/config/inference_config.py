"""Inference Configuration Manager — vetinari.config.inference_config.

Loads per-task inference profiles from external JSON config, applies
model-size adjustments and per-model overrides, and clamps values.

Usage
-----
    from vetinari.config.inference_config import get_inference_config

    cfg = get_inference_config()
    params = cfg.get_effective_params("coding", "qwen2.5-coder-7b")
    # -> {"temperature": 0.05, "top_p": 0.89, "top_k": 35, "max_tokens": 4096, ...}
"""

from __future__ import annotations

import json
import logging
import pathlib
import re
import threading
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml

from vetinari.config.model_config import get_task_default_model
from vetinari.constants import _PROJECT_ROOT
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass for a resolved profile
# ---------------------------------------------------------------------------


@dataclass
class InferenceProfile:
    """Resolved inference parameters for a task type."""

    temperature: float = 0.3
    top_p: float = 0.9
    top_k: int = 40
    max_tokens: int = 8192
    stop_sequences: list[str] = field(default_factory=list)
    prefer_json: bool = False

    def __repr__(self) -> str:
        return f"InferenceProfile(temperature={self.temperature!r}, max_tokens={self.max_tokens!r}, prefer_json={self.prefer_json!r})"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)


@dataclass(frozen=True, slots=True)
class ResolutionTraceRow:
    """One resolved inference parameter."""

    param_name: str
    value: Any
    source_layer: int
    source_ref: str

    def __repr__(self) -> str:
        return (
            "ResolutionTraceRow("
            f"param_name={self.param_name!r}, value={self.value!r}, "
            f"source_layer={self.source_layer!r}, source_ref={self.source_ref!r})"
        )


@dataclass(frozen=True, slots=True)
class ResolutionTrace:
    """Human-readable explanation of inference parameter resolution."""

    rows: tuple[ResolutionTraceRow, ...]

    def to_table(self) -> str:
        """Render a compact text table.

        Returns:
            Multiline table describing effective parameter sources.
        """
        lines = ["param value layer source"]
        lines.extend(f"{row.param_name} {row.value} {row.source_layer} {row.source_ref}" for row in self.rows)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Model size classification
# ---------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _classify_model_size(model_id: str) -> str:
    """Classify a model into a size tier based on its ID heuristics."""
    mid = model_id.lower()
    # Extract size numbers from model name (e.g., "qwen-7b", "llama-70b")

    matches = re.findall(r"(\d+)[bB]", mid)
    if matches:
        size_b = max(int(m) for m in matches)
        if size_b <= 10:
            return "small"
        if size_b <= 40:
            return "medium"
        if size_b <= 80:
            return "large"
        return "xlarge"
    # Fallback heuristics
    if any(k in mid for k in ("tiny", "mini", "small", "1b", "3b")):
        return "small"
    if any(k in mid for k in ("xl", "xxl", "ultra", "large")):
        return "xlarge"
    return "medium"  # safe default


# ---------------------------------------------------------------------------
# Config Manager
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_PATH = str(_PROJECT_ROOT / "config" / "task_inference_profiles.json")
_PACKAGED_CONFIG_PATH = str(pathlib.Path(__file__).resolve().parent / "runtime" / "task_inference_profiles.json")


def resolve_config_path(filename: str = "task_inference_profiles.json") -> pathlib.Path:
    """Resolve the production inference profile catalog path by filename.

    Returns:
        Path to the canonical inference profile catalog.

    Raises:
        ValueError: If ``filename`` is not the supported profile catalog name.
    """
    if pathlib.Path(filename).name != "task_inference_profiles.json":
        raise ValueError("only task_inference_profiles.json is an inference profile catalog")
    return _PROJECT_ROOT / "config" / "task_inference_profiles.json"


class InferenceConfigManager:
    """Manages per-task inference profiles loaded from external JSON config.

    Singleton — use ``get_inference_config()`` to get the shared instance.
    """

    _instance: InferenceConfigManager | None = None
    _class_lock = threading.Lock()

    def __new__(cls) -> InferenceConfigManager:
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    def _setup(self) -> None:
        self._lock = threading.RLock()
        self._profiles: dict[str, dict[str, Any]] = {}
        self._model_size_adjustments: dict[str, dict[str, Any]] = {}
        self._model_overrides: dict[str, dict[str, Any]] = {}
        self.is_loaded = False
        self._config_path: str | None = None
        self._load_config()

    def _clear_loaded_config(self) -> None:
        with self._lock:
            self._profiles = {}
            self._model_size_adjustments = {}
            self._model_overrides = {}
            self.is_loaded = False

    def _load_config_file(self, config_path: str) -> bool:
        try:
            with pathlib.Path(config_path).open(encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                logger.error(
                    "Inference profiles at %s has unexpected root type %s (expected dict)"
                    " — all profiles cleared, inference will use built-in fallbacks",
                    config_path,
                    type(data).__name__,
                )
                self._clear_loaded_config()
                return False

            profiles = data.get("profiles")
            if not isinstance(profiles, dict):
                logger.error(
                    "Inference profiles at %s missing dict 'profiles' section"
                    " — all profiles cleared, inference will use built-in fallbacks",
                    config_path,
                )
                self._clear_loaded_config()
                return False

            with self._lock:
                self._profiles = profiles
                self._model_size_adjustments = data.get("model_size_adjustments", {})
                self._model_overrides = data.get("model_overrides", {})
                self.is_loaded = True
                self._config_path = config_path

            logger.info("Loaded %d inference profiles from %s", len(self._profiles), config_path)
            return True
        except FileNotFoundError:
            logger.warning(
                "Inference profiles not found at %s — using built-in fallbacks",
                config_path,
            )
            self.is_loaded = False
            return False
        except json.JSONDecodeError as e:
            logger.error(
                "Inference profiles at %s is not valid JSON (line %d, col %d: %s)"
                " — all profiles cleared, inference will use built-in fallbacks",
                config_path,
                e.lineno,
                e.colno,
                e.msg,
            )
            self._clear_loaded_config()
            return False
        except Exception:
            logger.exception(
                "Unexpected error loading inference profiles from %s"
                " — all profiles cleared, inference will use built-in fallbacks",
                config_path,
            )
            self._clear_loaded_config()
            return False

    def _load_config(self, path: str | None = None) -> bool:
        """Load profiles from JSON config. Returns True on success."""
        config_path = path or self._config_path or _DEFAULT_CONFIG_PATH
        self._config_path = config_path

        if self._load_config_file(config_path):
            return True

        default_load = path is None and config_path == _DEFAULT_CONFIG_PATH
        if default_load and config_path != _PACKAGED_CONFIG_PATH:
            logger.warning(
                "Default inference config %s unavailable or invalid — trying packaged config %s",
                config_path,
                _PACKAGED_CONFIG_PATH,
            )
            return self._load_config_file(_PACKAGED_CONFIG_PATH)

        return False

    def update_profile_parameters(self, task_type: str, changes: dict[str, Any]) -> None:
        """Apply process-local profile changes through the manager boundary.

        Args:
            task_type: Profile key to update.
            changes: Non-empty mapping of profile values to merge.

        Raises:
            ValueError: If ``task_type`` or ``changes`` is empty or malformed.
        """
        if not isinstance(task_type, str) or not task_type.strip():
            raise ValueError("task_type must be non-empty")
        if not isinstance(changes, dict) or not changes:
            raise ValueError("changes must be a non-empty dict")
        with self._lock:
            profile_data = dict(self._profiles.get(task_type, {}))
            profile_data.update(changes)
            self._profiles[task_type] = profile_data

    def reload(self, path: str | None = None) -> bool:
        """Hot-reload config without restart."""
        return self._load_config(path)

    # ------------------------------------------------------------------
    # Profile lookup
    # ------------------------------------------------------------------

    def get_profile(self, task_type: str) -> InferenceProfile:
        """Get the base inference profile for a task type without model-size adjustments.

        Resolution order:
        1. JSON config profile for ``task_type`` (most specific, operator-tuned)
        2. Knowledge YAML parameter guide for ``task_type`` (data-driven fallback)
        3. JSON config ``general`` profile (safe default)

        Args:
            task_type: Task profile key (e.g. ``"coding"``, ``"reasoning"``, ``"general"``).

        Returns:
            InferenceProfile with temperature, top_p, top_k, max_tokens, and stop_sequences.
        """
        with self._lock:
            raw = self._profiles.get(task_type)

        if raw is not None:
            return InferenceProfile(
                temperature=raw.get("temperature", 0.3),
                top_p=raw.get("top_p", 0.9),
                top_k=raw.get("top_k", 40),
                max_tokens=raw.get("max_tokens", 2048),
                stop_sequences=raw.get("stop_sequences", []),
                prefer_json=raw.get("prefer_json", False),
            )

        # JSON config has no entry for this task type — try knowledge YAML
        knowledge_profile = self._get_knowledge_profile(task_type)
        if knowledge_profile is not None:
            return knowledge_profile

        # Final fallback: general profile from JSON config
        with self._lock:
            raw = self._profiles.get("general", {})

        return InferenceProfile(
            temperature=raw.get("temperature", 0.3),
            top_p=raw.get("top_p", 0.9),
            top_k=raw.get("top_k", 40),
            max_tokens=raw.get("max_tokens", 2048),
            stop_sequences=raw.get("stop_sequences", []),
            prefer_json=raw.get("prefer_json", False),
        )

    @staticmethod
    def _get_knowledge_profile(task_type: str) -> InferenceProfile | None:
        """Build an InferenceProfile from knowledge YAML parameter recommendations.

        Handles both preset format (``{"preset": "code", "temperature": 0.05, ...}``)
        and per-parameter format (``{"temperature": {"recommended": 0.05, ...}, ...}``).

        Args:
            task_type: Task type to look up in parameters.yaml.

        Returns:
            InferenceProfile derived from knowledge data, or None if no useful
            knowledge exists for this task type.
        """
        try:
            from vetinari.knowledge import get_parameter_guide
        except ImportError:
            logger.warning(
                "Knowledge module unavailable — skipping knowledge-based profile for %s",
                task_type,
            )
            return None

        guide = get_parameter_guide(task_type)
        if not guide:
            return None

        # Preset format: {"preset": "code", "temperature": 0.05, "top_p": 0.89, ...}
        if "preset" in guide:
            temperature = guide.get("temperature")
            top_p = guide.get("top_p")
            top_k = guide.get("top_k")
            if temperature is not None:
                return InferenceProfile(
                    temperature=float(temperature),
                    top_p=float(top_p) if top_p is not None else 0.9,
                    top_k=int(top_k) if top_k is not None else 40,
                    max_tokens=int(guide.get("max_tokens", 8192)),
                    stop_sequences=guide.get("stop_sequences", []),
                    prefer_json=bool(guide.get("prefer_json", False)),
                )

        # Per-parameter format: {"temperature": {"recommended": 0.05, ...}, ...}
        temperature = guide.get("temperature", {}).get("recommended")
        top_p = guide.get("top_p", {}).get("recommended")
        top_k = guide.get("top_k", {}).get("recommended")
        if temperature is not None:
            return InferenceProfile(
                temperature=float(temperature),
                top_p=float(top_p) if top_p is not None else 0.9,
                top_k=int(top_k) if top_k is not None else 40,
                max_tokens=int(guide.get("max_tokens", {}).get("recommended", 8192)),
                stop_sequences=[],
                prefer_json=False,
            )

        return None

    def get_effective_params(self, task_type: str, model_id: str = "") -> dict[str, Any]:
        """Resolve inference params for a (task_type, model_id) pair with all adjustments applied.

        Applies size-tier offsets (small/medium/large/xlarge) and per-model overrides
        on top of the base profile, then clamps each value to a valid range.

        Args:
            task_type: Task profile key (e.g. ``"coding"``, ``"reasoning"``).
            model_id: Model identifier used to derive the size tier and per-model overrides.
                When empty, returns the base profile without any model adjustments.

        Returns:
            Dictionary with keys: temperature, top_p, top_k, max_tokens,
            stop_sequences, prefer_json — all clamped to valid ranges.
        """
        profile = self.get_profile(task_type)

        if not model_id:
            # Auto-select the best available model for this task type so that
            # model-size adjustments (temperature offsets, top_k tweaks) can
            # still be applied even when the caller didn't specify a model.
            try:
                model_id = get_task_default_model(task_type)
            except Exception:
                logger.warning(
                    "get_task_default_model unavailable for %s — using base profile without model-specific tuning",
                    task_type,
                )
                return profile.to_dict()

        # Apply model-size adjustments
        size_tier = _classify_model_size(model_id)
        with self._lock:
            size_adj = self._model_size_adjustments.get(size_tier, {})

        temp_offset = size_adj.get("temperature_offset", 0.0)
        top_p_offset = size_adj.get("top_p_offset", 0.0)
        top_k_offset = size_adj.get("top_k_offset", 0)

        # Apply model-specific overrides
        with self._lock:
            model_ovr = self._model_overrides.get(model_id, {})

        temp_offset += model_ovr.get("temperature_offset", 0.0)
        top_p_offset += model_ovr.get("top_p_offset", 0.0)
        top_k_offset += model_ovr.get("top_k_offset", 0)

        # Apply offsets and clamp
        temperature = _clamp(profile.temperature + temp_offset, 0.0, 1.5)
        top_p = _clamp(profile.top_p + top_p_offset, 0.0, 1.0)
        top_k = int(_clamp(profile.top_k + top_k_offset, 1, 100))

        return {
            "temperature": round(temperature, 3),
            "top_p": round(top_p, 3),
            "top_k": top_k,
            "max_tokens": profile.max_tokens,
            "stop_sequences": profile.stop_sequences,
            "prefer_json": profile.prefer_json,
        }

    @staticmethod
    def _yaml_profile(profile_name: str) -> dict[str, Any]:
        path = _PROJECT_ROOT / "config" / "inference_profiles.yaml"
        if not path.exists():
            return {}
        try:
            with path.open(encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except Exception:
            logger.warning("Unable to load inference profile YAML %s", path, exc_info=True)
            return {}
        raw = data.get("profiles", {}).get(profile_name, {})
        return raw if isinstance(raw, dict) else {}

    def get_effective_catalog_params(
        self,
        profile_name: str,
        model_id: str,
        agent_type: str | None = None,
        agent_mode: str | None = None,
        call_site_overrides: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve parameters with explicit overrides above YAML profile defaults.

        Args:
            profile_name: Inference profile name.
            model_id: Model id used for size-based adjustments.
            agent_type: Optional agent type for traceability.
            agent_mode: Optional agent mode for traceability.
            call_site_overrides: Explicit non-None override values.

        Returns:
            Effective inference parameter mapping.
        """
        params = self.get_effective_params(profile_name, model_id)
        yaml_profile = self._yaml_profile(profile_name)
        for key in ("temperature", "top_p", "top_k", "max_tokens"):
            if key in yaml_profile:
                params[key] = yaml_profile[key]
        if call_site_overrides:
            params.update({k: v for k, v in call_site_overrides.items() if v is not None})
        return params

    def explain(self, agent: str, task: str, model: str) -> ResolutionTrace:
        """Explain which layer set each effective inference parameter.

        Args:
            agent: Agent name for the trace request.
            task: Task or profile name being resolved.
            model: Model id being resolved.

        Returns:
            Resolution trace rows for the effective parameters.
        """
        params = self.get_effective_catalog_params(task, model, agent_type=agent)
        yaml_profile = self._yaml_profile(task)
        rows = []
        for key in ("temperature", "top_p", "top_k", "max_tokens"):
            layer = 3 if key in yaml_profile else 6
            source = f"config/inference_profiles.yaml:{task}" if layer == 3 else "InferenceConfigManager fallback"
            rows.append(ResolutionTraceRow(key, params.get(key), layer, source))
        return ResolutionTrace(tuple(rows))

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_profiles(self) -> list[str]:
        """Return the names of all loaded task-type inference profiles.

        Returns:
            List of profile key strings (e.g. ``["general", "coding", "reasoning"]``).
        """
        with self._lock:
            return list(self._profiles.keys())

    def get_all_profiles(self) -> dict[str, dict[str, Any]]:
        """Return all loaded inference profiles as a copy of the raw config data.

        Returns:
            Mapping from task-type name to its raw parameter dictionary as loaded
            from the JSON config file.
        """
        with self._lock:
            return dict(self._profiles)

    def get_stats(self) -> dict[str, Any]:
        """Return diagnostic information about the current config manager state.

        Returns:
            Dictionary with keys: loaded (bool), config_path, profile_count,
            model_size_tiers (list of tier names), and model_overrides (list of model IDs
            with explicit overrides).
        """
        with self._lock:
            return {
                "loaded": self.is_loaded,
                "config_path": self._config_path,
                "profile_count": len(self._profiles),
                "model_size_tiers": list(self._model_size_adjustments.keys()),
                "model_overrides": list(self._model_overrides.keys()),
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------


def get_inference_config() -> InferenceConfigManager:
    """Return the singleton InferenceConfigManager, creating it if necessary.

    Returns:
        The shared InferenceConfigManager used for per-task inference profile resolution.
    """
    return InferenceConfigManager()


def reset_inference_config() -> None:
    """Reset inference config."""
    with InferenceConfigManager._class_lock:
        InferenceConfigManager._instance = None


def reload_inference_config() -> None:
    """Reload inference config by clearing the singleton cache."""
    reset_inference_config()
