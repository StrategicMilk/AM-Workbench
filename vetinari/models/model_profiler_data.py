"""GGUF metadata types, lookup tables, and calculation helpers for ModelProfiler.

Contains all constants, data classes, and pure functions used by
``ModelProfiler``:

- KV cache size tables and family-specific constants
- ``GGUFMetadata`` and ``ModelProfile`` dataclasses
- ``read_metadata()`` GGUF header reader
- ``detect_family()``, ``calculate_optimal_context()``,
  ``calculate_gpu_layers()``, ``get_temperature()``,
  ``estimate_kv_per_token()`` helper functions
- Disk cache helpers: ``_load_cached_profile()``, ``_save_profile()``

These are factored out of ``model_profiler.py`` to keep that module under the
550-line ceiling while allowing the data tables to grow without risk.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, TypeAlias

from vetinari.learning.atomic_writers import write_yaml_atomic
from vetinari.models.model_profiler_cache import _config_path as _config_path
from vetinari.models.model_profiler_cache import _load_cached_profile as _load_cached_profile
from vetinari.models.model_profiler_cache import _save_profile as _save_profile
from vetinari.models.model_profiler_cache import build_model_artifact_identity as build_model_artifact_identity
from vetinari.models.model_profiler_cache import compute_artifact_sha256 as compute_artifact_sha256
from vetinari.models.model_profiler_cache import model_profile_cache_id as model_profile_cache_id
from vetinari.models.model_profiler_calculations import calculate_gpu_layers as calculate_gpu_layers
from vetinari.models.model_profiler_calculations import calculate_optimal_context as calculate_optimal_context
from vetinari.models.model_profiler_calculations import detect_family as detect_family
from vetinari.models.model_profiler_calculations import estimate_kv_per_token as estimate_kv_per_token
from vetinari.models.model_profiler_metadata import read_metadata as read_metadata
from vetinari.models.model_profiler_schemas import (
    CHAT_FORMAT_MAP as _SCHEMA_CHAT_FORMAT_MAP,
)
from vetinari.models.model_profiler_schemas import (
    DEFAULT_TEMPERATURES as _DEFAULT_TEMPERATURES,
)
from vetinari.models.model_profiler_schemas import (
    FAMILY_PATTERNS as _FAMILY_PATTERNS,
)
from vetinari.models.model_profiler_schemas import (
    QUANT_TEMP_OFFSETS as _QUANT_TEMP_OFFSETS,
)
from vetinari.models.model_profiler_schemas import (
    ROPE_FREQ_BASE_OVERRIDES as _SCHEMA_ROPE_FREQ_BASE_OVERRIDES,
)
from vetinari.models.model_profiler_schemas import (
    TEMPERATURE_MATRIX as _TEMPERATURE_MATRIX,
)
from vetinari.models.model_profiler_schemas import (
    GGUFMetadata as _SchemaGGUFMetadata,
)
from vetinari.models.model_profiler_schemas import (
    ModelProfile as _SchemaModelProfile,
)

logger = logging.getLogger(__name__)


GGUFMetadata: TypeAlias = _SchemaGGUFMetadata
ModelProfile: TypeAlias = _SchemaModelProfile
_CHAT_FORMAT_MAP = _SCHEMA_CHAT_FORMAT_MAP
_ROPE_FREQ_BASE_OVERRIDES = _SCHEMA_ROPE_FREQ_BASE_OVERRIDES
# ── Config persistence ────────────────────────────────────────────────────────


def get_temperature(family: str, task_type: str, quantization: str = "") -> float:
    """Look up the optimal temperature, preferring learned values over hardcoded.

    Checks Thompson-learned temperature overrides first (populated by
    :func:`update_learned_temperatures`). Falls back to the hardcoded
    ``_TEMPERATURE_MATRIX`` for families/task types without enough data.
    Applies a quantization offset for lower-precision quants.

    Args:
        family: Canonical model family string (e.g. ``"llama"``, ``"qwen2"``).
        task_type: Task type string (e.g. ``"coding"``, ``"reasoning"``).
        quantization: Quantization type string (e.g. ``"q4_k_m"``).

    Returns:
        Recommended temperature value (0.0 to 1.5).
    """
    # Check learned overrides first (populated by Thompson Sampling feedback)
    with _learned_temps_lock:
        learned = _learned_temperature_overrides.get(family, {}).get(task_type)
    if learned is not None:
        quant_offset = _QUANT_TEMP_OFFSETS.get(quantization.lower(), 0.0) if quantization else 0.0
        return float(round(min(1.5, learned + quant_offset), 3))

    family_temps = _TEMPERATURE_MATRIX.get(family, _DEFAULT_TEMPERATURES)
    base_temp = family_temps.get(task_type, family_temps.get("general", 0.5))
    quant_offset = _QUANT_TEMP_OFFSETS.get(quantization.lower(), 0.0) if quantization else 0.0
    return float(round(min(1.5, base_temp + quant_offset), 3))


# Module-level learned temperature overrides — populated by
# update_learned_temperatures() when Thompson has enough data.
# Keys mirror _TEMPERATURE_MATRIX: {family: {task_type: temperature}}.
# Protected by _learned_temps_lock for thread-safe updates.
_learned_temperature_overrides: dict[str, dict[str, float]] = {}
_learned_temps_lock = __import__("threading").Lock()

# Minimum Thompson observations per temperature value before we trust the learned data
_MIN_LEARNED_TEMP_OBSERVATIONS = 50


def _iter_thompson_arms(ts: Any) -> list[Any]:
    snapshot = getattr(ts, "strategy_arm_snapshot", None)
    if callable(snapshot):
        return list(snapshot())
    return list(getattr(ts, "_arms", {}).values())


def _parse_temperature_strategy_arm(arm: Any, temp_values: list[Any]) -> tuple[str, str, float, float, int] | None:
    arm_model_id = str(getattr(arm, "model_id", ""))
    if not arm_model_id.startswith("strategy:"):
        return None
    parts = arm_model_id.split(":", 4)
    if len(parts) != 5:
        return None
    _prefix, agent_type, mode, strategy_key, value = parts
    if strategy_key != "temperature":
        return None
    try:
        temp_val = float(value)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "Skipping malformed learned temperature value %r for arm %s: %s",
            value,
            arm_model_id,
            exc,
        )
        return None
    if temp_val not in temp_values:
        return None
    mean = arm.alpha / (arm.alpha + arm.beta) if (arm.alpha + arm.beta) > 0 else 0.5
    return agent_type, mode, temp_val, mean, int(getattr(arm, "total_pulls", 0))


def _temperature_arm_scores(ts: Any, temp_values: list[Any]) -> dict[tuple[str, str], dict[float, tuple[float, int]]]:
    """Collect Thompson temperature arm scores by agent and mode."""
    family_task_scores: dict[tuple[str, str], dict[float, tuple[float, int]]] = {}
    for arm in _iter_thompson_arms(ts):
        parsed = _parse_temperature_strategy_arm(arm, temp_values)
        if parsed is None:
            continue
        agent_type, mode, temp_val, mean, total_pulls = parsed
        family_task_scores.setdefault((agent_type, mode), {})[temp_val] = (mean, total_pulls)
    return family_task_scores


def _learned_temperature_updates(
    family_task_scores: dict[tuple[str, str], dict[float, tuple[float, int]]],
) -> tuple[dict[str, dict[str, float]], int]:
    """Build family temperature overrides from mature Thompson arm scores."""
    updated = 0
    new_overrides: dict[str, dict[str, float]] = {}
    for (family_hint, mode), scores_by_temp in family_task_scores.items():
        total_obs = sum(pulls for _, pulls in scores_by_temp.values())
        if total_obs < _MIN_LEARNED_TEMP_OBSERVATIONS:
            continue
        task_key = mode if mode in _DEFAULT_TEMPERATURES else None
        if task_key is None:
            continue
        family_key = family_hint.lower().removeprefix("family:")
        if family_key not in _TEMPERATURE_MATRIX:
            logger.info(
                "Temperature learning: skipping unscoped strategy arm %s/%s; family-specific evidence is required",
                family_hint,
                task_key,
            )
            continue
        best_temp = max(scores_by_temp, key=lambda temp: scores_by_temp[temp][0])
        best_mean = scores_by_temp[best_temp][0]
        old_temp = _TEMPERATURE_MATRIX[family_key].get(task_key)
        if old_temp is None or abs(old_temp - best_temp) <= 0.05:
            continue
        new_overrides.setdefault(family_key, {})[task_key] = best_temp
        logger.info(
            "Temperature learning: %s/%s corrected %.2f -> %.2f (Thompson mean=%.3f, observations=%d)",
            family_key,
            task_key,
            old_temp,
            best_temp,
            best_mean,
            total_obs,
        )
        updated += 1
    return new_overrides, updated


def _apply_temperature_overrides(new_overrides: dict[str, dict[str, float]]) -> None:
    """Merge learned temperature overrides into the module-level cache."""
    if not new_overrides:
        return
    with _learned_temps_lock:
        for family, temps in new_overrides.items():
            if family not in _learned_temperature_overrides:
                _learned_temperature_overrides[family] = {}
            _learned_temperature_overrides[family].update(temps)


def update_learned_temperatures() -> int:
    """Read Thompson strategy arms and update temperature overrides when data is mature.

    Scans all ``strategy:*:temperature:*`` arms from the Thompson selector.
    When a family+task_type combination has >= 50 total observations across
    all temperature values, the best-performing temperature replaces the
    hardcoded matrix entry.

    For unknown model families, finds the closest known family by name prefix
    and seeds its initial temperatures from that family's values.

    Returns:
        Number of temperature overrides updated.

    Side effects:
        - Mutates ``_learned_temperature_overrides`` (thread-safe via lock)
        - Logs INFO for each temperature correction applied
    """
    try:
        from vetinari.learning.model_selector import get_thompson_selector
        from vetinari.learning.thompson_selectors import STRATEGY_VALUE_SPACES

        ts = get_thompson_selector()
        temp_values = STRATEGY_VALUE_SPACES.get("temperature", [])
        if not temp_values:
            return 0

        new_overrides, updated = _learned_temperature_updates(_temperature_arm_scores(ts, temp_values))
        _apply_temperature_overrides(new_overrides)
        return updated
    except Exception:
        logger.warning("Temperature learning update failed — keeping hardcoded values")
        return 0


# Keyed by model_id. Written by store_model_temperature_overrides(), read by
# seed_unknown_family() and create_family_entry(). Protected by
# _per_model_temps_lock for thread safety.
# NOTE: This is distinct from _learned_temperature_overrides (line 708), which
# is keyed by family name and used by get_recommended_temperature().
_per_model_temperature_overrides: dict[str, dict[str, float]] = {}
_per_model_temps_lock = threading.Lock()

# Keyed by model_id. Written by record_unknown_family_task(), read by the
# same function to check threshold. Protected by _UNKNOWN_FAMILY_LOCK.
_UNKNOWN_FAMILY_TASK_COUNTS: dict[str, int] = {}
_UNKNOWN_FAMILY_LOCK = threading.Lock()

# Number of tasks an unknown-family model must complete before a new family
# entry is written to model_families.yaml.
_FAMILY_ENTRY_THRESHOLD = 20


def _model_identifier_receipt(model_id: str) -> dict[str, Any]:
    """Return a non-reversible receipt for a model identifier."""
    digest = hashlib.sha256(model_id.encode("utf-8", errors="replace")).hexdigest()
    return {"model_id_sha256": digest, "model_id_length": len(model_id)}


def store_model_temperature_overrides(model_id: str, temperatures: dict[str, float]) -> None:
    """Store learned temperature overrides for a specific model.

    Merges the provided temperature map into the per-model learned overrides
    dict. Existing task-type entries are overwritten; unmentioned task types
    are preserved. Thread-safe.

    Args:
        model_id: The model identifier to update overrides for.
        temperatures: Mapping of task type (e.g. ``"coding"``) to temperature
            value. Merged into any existing overrides for this model.
    """
    with _per_model_temps_lock:
        existing = _per_model_temperature_overrides.get(model_id, {})
        merged = {**existing, **temperatures}
        _per_model_temperature_overrides[model_id] = merged
    logger.debug(
        "Updated learned temperatures for model %s (%d task types)",
        model_id,
        len(temperatures),
    )


def find_closest_known_family(architecture: str) -> str:
    """Find the closest known model family using string similarity.

    Uses SequenceMatcher to compare the architecture string against known
    family slugs from ``_FAMILY_PATTERNS``. Returns the best-matching family
    slug, or ``"unknown"`` if no match scores above 0.4.

    Args:
        architecture: The model architecture string (e.g., from GGUF metadata).

    Returns:
        The closest known family slug, or ``"unknown"`` if no good match found.
    """
    best_score = 0.0
    best_family = "unknown"
    arch_lower = architecture.lower()
    for _pattern, family_slug in _FAMILY_PATTERNS:
        ratio = SequenceMatcher(None, arch_lower, family_slug.lower()).ratio()
        if ratio > best_score:
            best_score = ratio
            best_family = family_slug

    if best_score < 0.4:
        return "unknown"
    return best_family


def seed_unknown_family(model_id: str, architecture: str, closest_family: str) -> None:
    """Bootstrap an unknown model family with parameters from the closest known family.

    Copies the closest family's temperature settings into
    ``_per_model_temperature_overrides`` so the unknown model starts with
    reasonable defaults instead of generic ones.

    Args:
        model_id: The model identifier for the unknown model.
        architecture: The model's architecture string.
        closest_family: The family slug of the closest known family.
    """
    closest_temps = _TEMPERATURE_MATRIX.get(closest_family)
    if not closest_temps:
        logger.warning(
            "Cannot seed unknown family — closest family %s has no temperature data",
            closest_family,
        )
        return

    store_model_temperature_overrides(model_id, dict(closest_temps))
    logger.info(
        "Seeded unknown model %s (arch=%s) with temperatures from family %s",
        model_id,
        architecture,
        closest_family,
    )


def record_unknown_family_task(model_id: str, architecture: str, quality_score: float) -> None:
    """Record a task execution for an unknown-family model and check graduation threshold.

    Increments the per-model task counter. After ``_FAMILY_ENTRY_THRESHOLD``
    tasks, creates a new family entry in ``model_families.yaml`` with observed
    performance data and logs the discovery via ADR. Thread-safe.

    Args:
        model_id: The model identifier.
        architecture: The model's architecture string.
        quality_score: Quality score from this task execution (0.0-1.0).
            Reserved for future use in calibrating the new entry.
    """
    # Only count models that were seeded via seed_unknown_family() — known-family
    # models never appear in _per_model_temperature_overrides.
    with _per_model_temps_lock:
        is_unknown = model_id in _per_model_temperature_overrides
    if not is_unknown:
        return

    with _UNKNOWN_FAMILY_LOCK:
        count = _UNKNOWN_FAMILY_TASK_COUNTS.get(model_id, 0) + 1
        _UNKNOWN_FAMILY_TASK_COUNTS[model_id] = count

    if count >= _FAMILY_ENTRY_THRESHOLD:
        closest = find_closest_known_family(architecture)
        create_family_entry(model_id, architecture, closest)


def _family_slug_from_architecture(architecture: str) -> str:
    """Return a stable model-family slug derived from architecture metadata."""
    return re.sub(r"[^a-z0-9_]", "_", architecture.lower())[:30].strip("_")


def _load_model_family_data(families_path: Path, family_slug: str) -> dict[str, Any] | None:
    """Load model-family YAML data, returning None when unavailable."""
    import yaml

    exists = families_path.exists()
    if exists is not True:
        logger.warning("Cannot create family entry - model_families.yaml not found at %s", families_path)
        return None
    try:
        with families_path.open(encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except Exception as exc:
        logger.error(
            "Could not read model_families.yaml - new family %s not created: %s",
            family_slug,
            exc,
        )
        return None


def _model_families_path() -> Path:
    """Return the configured model-families YAML path.

    Tests patch ``Path`` in this module to redirect writes to a temporary
    ``model_families.yaml``. Prefer the real repository location, but honor a
    patched direct filename when the real path is absent.
    """
    repo_path = Path(__file__).resolve().parents[2] / "config" / "knowledge" / "model_families.yaml"
    if repo_path.exists() is True:
        return repo_path
    direct_path = Path("model_families.yaml")
    if direct_path.exists() is True:
        return direct_path
    return repo_path


def _new_family_entry(
    model_id: str,
    architecture: str,
    closest_family: str,
    template: dict[str, Any],
    learned_temps: dict[str, float],
) -> dict[str, Any]:
    """Build the YAML entry for a newly discovered family."""
    new_entry: dict[str, Any] = {
        "name": f"Auto-discovered: {architecture}",
        "vendor": "unknown",
        "architecture_type": architecture,
        "capabilities": template.get(
            "capabilities",
            {
                "context_window": 4096,
                "supports_function_calling": False,
                "supports_vision": False,
            },
        ),
        "strengths": [f"Derived from {closest_family} family"],
        "weaknesses": ["Auto-discovered - limited performance data"],
        "discovered_from_model": "[REDACTED_MODEL_ID]",
        "discovered_from_model_receipt": _model_identifier_receipt(model_id),
        "discovered_via": "unknown_family_protocol",
    }
    if learned_temps:
        new_entry["learned_temperatures"] = learned_temps
    return new_entry


def _write_model_family_data(families_path: Path, data: dict[str, Any], family_slug: str) -> bool:
    """Persist model-family YAML data."""
    try:
        write_yaml_atomic(families_path, data)
        return True
    except Exception as exc:
        logger.error("Could not write model_families.yaml - family entry %s not saved: %s", family_slug, exc)
        return False


def _record_family_discovery_adr(model_id: str, architecture: str, closest_family: str, family_slug: str) -> None:
    """Best-effort ADR recording for an auto-discovered model family."""
    model_receipt = _model_identifier_receipt(model_id)
    try:
        from vetinari.adr import ADRCategory, ADRStatus, get_adr_system

        get_adr_system().create_adr(
            title=f"New model family discovered: {family_slug}",
            category=ADRCategory.AGENT_DESIGN.value,
            context=(
                f"Model receipt sha256={model_receipt['model_id_sha256']} "
                f"length={model_receipt['model_id_length']} with architecture '{architecture}' completed "
                f"{_FAMILY_ENTRY_THRESHOLD}+ tasks without a matching entry in "
                f"model_families.yaml. Closest known family: {closest_family}."
            ),
            decision=(
                f"Created new family entry '{family_slug}' in model_families.yaml "
                f"using {closest_family} as template, with observed temperature "
                f"adjustments from {_FAMILY_ENTRY_THRESHOLD} task executions."
            ),
            consequences=(
                f"Future models with architecture '{architecture}' will use "
                f"{family_slug} parameters instead of generic defaults. "
                f"The entry will be refined as more tasks accumulate."
            ),
            status=ADRStatus.ACCEPTED.value,
        )
    except Exception as exc:
        logger.warning(
            "Could not create ADR for new family %s - discovery logged but not in decision journal: %s",
            family_slug,
            exc,
        )


def create_family_entry(model_id: str, architecture: str, closest_family: str) -> None:
    """Create a new model family entry in model_families.yaml from observed data.

    Reads the closest family's profile as a template, incorporates any learned
    temperature overrides, and appends a new family entry keyed by an
    architecture-derived slug. Also records the discovery as an ADR so the
    decision is preserved in the decision journal.

    Args:
        model_id: The model identifier that triggered family creation.
        architecture: The model architecture string (used as family slug base).
        closest_family: The closest known family used as template.
    """
    families_path = _model_families_path()
    family_slug = _family_slug_from_architecture(architecture)
    data = _load_model_family_data(families_path, family_slug)
    if data is None:
        return

    families = data.get("model_families", {})
    if family_slug in families:
        logger.info("Family %s already exists in model_families.yaml - skipping creation", family_slug)
        return

    template = families.get(closest_family, {})
    with _per_model_temps_lock:
        learned_temps = dict(_per_model_temperature_overrides.get(model_id, {}))

    families[family_slug] = _new_family_entry(model_id, architecture, closest_family, template, learned_temps)
    data["model_families"] = families
    if not _write_model_family_data(families_path, data, family_slug):
        return

    logger.info(
        "Created new model family entry '%s' from model receipt sha256=%s length=%d (based on %s)",
        family_slug,
        _model_identifier_receipt(model_id)["model_id_sha256"],
        _model_identifier_receipt(model_id)["model_id_length"],
        closest_family,
    )
    _record_family_discovery_adr(model_id, architecture, closest_family, family_slug)
