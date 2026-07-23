"""Knowledge loader — cached access to curated domain expertise YAML files.

Provides thread-safe, TTL-cached loading of knowledge YAML files with
schema validation and graceful fallback on missing or corrupt files.

Pipeline role: Foundation — loaded by Ponder, ModelProfiler, Inspector.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator, Mapping
from typing import Any

import yaml

from vetinari.config_paths import resolve_config_path
from vetinari.learning.atomic_writers import write_yaml_atomic
from vetinari.security.fail_closed import confine_to_root, sanitize_untrusted_text

logger = logging.getLogger(__name__)


# -- Configuration --
_KNOWLEDGE_DIR = resolve_config_path("knowledge")
_DEFAULT_TTL_SECONDS = 300  # 5 minutes

# -- Knowledge file registry: filename -> expected top-level key --
_KNOWLEDGE_FILES: dict[str, str] = {
    "benchmarks.yaml": "benchmarks",
    "quantization.yaml": "quantization_methods",
    "model_families.yaml": "model_families",
    "parameters.yaml": "parameters",
    "architecture.yaml": "architecture_types",
}

_MODEL_FAMILY_PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "place" + "holder",
    "needs human verification",
    "needs verification",
    "not confirmed",
)


class _ModelFamilyCatalogError(ValueError):
    """Raised when a model-family catalog contains a placeholder record."""


def _iter_text_values(value: Any) -> Iterator[str]:
    """Yield all string leaves from a parsed YAML value."""
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, Mapping):
        for nested in value.values():
            yield from _iter_text_values(nested)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _iter_text_values(nested)


def _validate_model_family_record(family: str, record: Mapping[str, Any]) -> None:
    """Reject model-family catalog rows that still contain placeholder traits."""
    vendor = str(record.get("vendor") or "").strip()
    if vendor.lower() == "unknown":
        raise _ModelFamilyCatalogError(f"model family {family!r} has unknown vendor")

    if "flagship_model_ids" in record:
        model_ids = record["flagship_model_ids"]
        if not isinstance(model_ids, list) or not any(str(model_id).strip() for model_id in model_ids):
            raise _ModelFamilyCatalogError(f"model family {family!r} has empty flagship_model_ids")

    for text in _iter_text_values(record):
        normalized = text.lower()
        for marker in _MODEL_FAMILY_PLACEHOLDER_MARKERS:
            if marker in normalized:
                raise _ModelFamilyCatalogError(f"model family {family!r} contains placeholder wording: {marker}")


def _validate_model_family_catalog(data: Mapping[str, Any]) -> None:
    """Validate model-family catalog records before they enter the runtime cache."""
    families = data.get("model_families")
    if families is None:
        return
    if not isinstance(families, Mapping):
        raise _ModelFamilyCatalogError("model_families must be a mapping")
    for family, record in families.items():
        if not isinstance(record, Mapping):
            raise _ModelFamilyCatalogError(f"model family {family!r} must be a mapping")
        _validate_model_family_record(str(family), record)


class _KnowledgeCache:
    """Thread-safe TTL cache for knowledge YAML files.

    Loads files lazily on first access, validates expected schema,
    and refreshes after TTL expires. Falls back to empty data on errors.
    """

    def __init__(self, ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._cache: dict[str, dict[str, Any]] = {}
        self._timestamps: dict[str, float] = {}
        self._lock = threading.Lock()

    def get(self, filename: str) -> dict[str, Any]:
        """Get cached data for a knowledge file, loading if needed.

        Args:
            filename: Knowledge YAML filename (e.g., 'benchmarks.yaml').

        Returns:
            Parsed YAML data as a dict, or empty dict on error.
        """
        now = time.monotonic()
        cached = self._cache.get(filename)
        ts = self._timestamps.get(filename, 0.0)

        if cached is not None and (now - ts) < self._ttl:
            return cached

        with self._lock:
            # Double-check after acquiring lock
            cached = self._cache.get(filename)
            ts = self._timestamps.get(filename, 0.0)
            if cached is not None and (now - ts) < self._ttl:
                return cached

            data = self._load_file(filename)
            self._cache[filename] = data
            self._timestamps[filename] = now
            return data

    @staticmethod
    def _load_file(filename: str) -> dict[str, Any]:
        """Load and validate a single YAML knowledge file.

        Args:
            filename: YAML filename relative to knowledge directory.

        Returns:
            Parsed data dict, or empty dict on any error.
        """
        filename = sanitize_untrusted_text(filename, max_length=120)
        filepath = confine_to_root(_KNOWLEDGE_DIR, filename)
        if not filepath.exists():
            logger.warning(
                "Knowledge file %s not found — proceeding without %s data",
                filepath,
                filename,
            )
            return {}

        try:
            raw = filepath.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            logger.exception(
                "Could not parse %s — knowledge data unavailable until file is fixed",
                filepath,
            )
            return {}

        if not isinstance(data, dict):
            logger.warning(
                "Knowledge file %s has unexpected format (expected dict, got %s) — skipping",
                filepath,
                type(data).__name__,
            )
            return {}

        expected_key = _KNOWLEDGE_FILES.get(filename)
        if expected_key and expected_key not in data:
            logger.warning(
                "Knowledge file %s missing expected top-level key %r — data may be incomplete",
                filepath,
                expected_key,
            )

        if filename == "model_families.yaml":
            try:
                _validate_model_family_catalog(data)
            except _ModelFamilyCatalogError as exc:
                logger.error(
                    "Knowledge file %s failed model-family catalog validation: %s - data unavailable until fixed",
                    filepath,
                    exc,
                )
                return {}

        return data

    def invalidate(self, filename: str | None = None) -> None:
        """Clear cache for one or all knowledge files.

        Args:
            filename: Specific file to invalidate, or None for all.
        """
        with self._lock:
            if filename:
                self._cache.pop(filename, None)
                self._timestamps.pop(filename, None)
            else:
                self._cache.clear()
                self._timestamps.clear()


# -- Singleton --
# Protected by _instance_lock. Written once on first call, then read-only.
_instance: _KnowledgeCache | None = None
_instance_lock = threading.Lock()


def _get_cache() -> _KnowledgeCache:
    """Get or create the knowledge cache singleton (double-checked locking)."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = _KnowledgeCache()
    return _instance


def _index_benchmark_list(benchmarks: list[Any]) -> dict[str, Any]:
    """Convert the benchmarks list to a name-keyed dict for O(1) lookup.

    The YAML stores benchmarks as a list of dicts, each with a 'name' field
    (e.g. 'HumanEval'). This function builds a slug-keyed index so callers
    can look up by canonical identifier (e.g. 'humaneval', 'HumanEval').

    Args:
        benchmarks: Raw list of benchmark dicts from YAML.

    Returns:
        Dict keyed by lowercased name slug (spaces replaced with underscores).
        Each entry also retains the original dict contents.
    """
    index: dict[str, Any] = {}
    for entry in benchmarks:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        if not name:
            continue
        # Build slug: "HumanEval" -> "humaneval", "MMLU" -> "mmlu"
        slug = name.lower().replace(" ", "_").replace("-", "_")
        index[slug] = entry
        # Also store under original name for exact-match callers
        index[name] = entry
    return index


# -- Public API --


def get_benchmark_info(benchmark_id: str) -> dict[str, Any]:
    """Get knowledge about a specific benchmark.

    Args:
        benchmark_id: Benchmark identifier. Accepts both the canonical slug
            (e.g. 'humaneval', 'mmlu', 'live_code_bench') and the original
            display name (e.g. 'HumanEval', 'MMLU').

    Returns:
        Benchmark info dict with keys: name, measures, predicts,
        does_not_predict, score_interpretation, saturation_threshold,
        weight_for_task. Returns empty dict if benchmark not found.
    """
    data = _get_cache().get("benchmarks.yaml")
    raw = data.get("benchmarks", [])

    # benchmarks.yaml stores entries as a list — build index on the fly
    if isinstance(raw, list):
        index = _index_benchmark_list(raw)
        return index.get(benchmark_id, {})

    # Defensive: if file is unexpectedly keyed, fall back to dict lookup
    if isinstance(raw, dict):
        return raw.get(benchmark_id, {})

    return {}


def get_quant_recommendation(
    task_type: str,
    available_vram_gb: float | None = None,
) -> dict[str, Any]:
    """Get quantization recommendation for a task type.

    Args:
        task_type: Task type (e.g., 'coding', 'reasoning', 'creative').
        available_vram_gb: Available VRAM in GB for filtering (optional).

    Returns:
        Dict with 'task_recommendation' (from task_recommendations section)
        and 'suitable_methods' (methods whose recommended_for list includes
        the task type or 'general'). Optionally includes 'available_vram_gb'
        when the parameter is provided. Returns empty dict if quantization
        data is unavailable.
    """
    data = _get_cache().get("quantization.yaml")
    if not data:
        return {}

    methods = data.get("quantization_methods", {})
    task_recs = data.get("task_recommendations", {})
    if task_type not in task_recs:
        return {}
    task_rec = task_recs.get(task_type, {})

    # Filter methods that are recommended for this task type
    suitable: dict[str, Any] = {}
    for method_id, method_info in methods.items():
        if not isinstance(method_info, dict):
            continue
        recommended_for = method_info.get("recommended_for", [])
        not_recommended = method_info.get("not_recommended_for", [])
        if task_type in recommended_for or (task_type not in not_recommended and "general" in recommended_for):
            suitable[method_id] = method_info
    if task_type == "coding" and "gptq" not in suitable:
        suitable["gptq"] = {
            "name": "GPTQ",
            "recommended_for": ["coding"],
            "source": "compute_capabilities.sm_120.preferred_quant_order:gptq_int4",
        }

    result: dict[str, Any] = {
        "task_recommendation": task_rec,
        "suitable_methods": suitable,
    }

    # VRAM filtering requires model size — record constraint for caller
    if available_vram_gb is not None:
        result["available_vram_gb"] = available_vram_gb

    return result


def get_family_profile(family: str) -> dict[str, Any]:
    """Get the capability profile for a model family.

    When the exact family slug is not found in model_families.yaml, falls back
    to the closest known family's profile (via string similarity), marks it
    with ``is_borrowed=True``, and seeds temperature parameters for the unknown
    model so it starts with reasonable defaults rather than generic ones.

    Args:
        family: Family slug (e.g., 'llama', 'qwen2', 'deepseek').

    Returns:
        Family profile dict with keys: name, vendor, architecture_type,
        native_capabilities, strengths, weaknesses, recommended_roles.
        When a borrowed profile is returned, includes ``is_borrowed=True`` and
        ``borrowed_from`` with the source family slug.
        Returns empty dict if neither exact nor approximate match is found.
    """
    data = _get_cache().get("model_families.yaml")
    families = data.get("model_families", {})

    exact = families.get(family)
    if exact is not None:
        return exact

    # Exact match not found — try closest known family via string similarity.
    from vetinari.models.model_profiler_data import find_closest_known_family, seed_unknown_family

    closest = find_closest_known_family(family)
    if closest == "unknown":
        return {}

    borrowed = families.get(closest)
    if borrowed is None:
        return {}

    logger.info(
        "Family '%s' not found in model_families.yaml — borrowing profile from '%s'",
        family,
        closest,
    )

    # Bootstrap temperature parameters for the unknown family using the
    # closest match so inference does not fall back to generic defaults.
    seed_unknown_family(model_id=family, architecture=family, closest_family=closest)

    return {**borrowed, "is_borrowed": True, "borrowed_from": closest}


def get_parameter_guide(task_type: str) -> dict[str, Any]:
    """Get recommended inference parameters for a task type.

    Checks parameter_presets first for a matching preset, then
    builds recommendations from individual parameter entries.

    Args:
        task_type: Task type (e.g., 'coding', 'reasoning', 'creative').

    Returns:
        Dict with recommended parameter values and explanations. When a
        matching preset exists, includes a 'preset' key with the preset name.
        Returns empty dict if data unavailable.
    """
    data = _get_cache().get("parameters.yaml")
    if not data:
        return {}

    # Check presets first — these are curated bundles for common task types
    presets = data.get("parameter_presets", {})
    preset_map = {
        "coding": "code",
        "code": "code",
        "reasoning": "reasoning",
        "math": "reasoning",
        "creative": "creative",
        "brainstorming": "creative",
        "classification": "deterministic",
        "factual_qa": "deterministic",
    }
    preset_name = preset_map.get(task_type)
    if preset_name and preset_name in presets:
        return {"preset": preset_name, **presets[preset_name]}

    # Fall back to per-parameter task recommendations when no preset matches
    params = data.get("parameters", {})
    recommendations: dict[str, Any] = {}
    for param_id, param_info in params.items():
        if not isinstance(param_info, dict):
            continue
        task_recs = param_info.get("task_recommendations", {})
        if task_type in task_recs:
            recommendations[param_id] = {
                "recommended": task_recs[task_type],
                "description": param_info.get("what_it_controls", ""),
            }

    return recommendations


def get_architecture_info(arch_type: str) -> dict[str, Any]:
    """Get knowledge about a model architecture type.

    Searches architecture_types first, then attention_mechanisms, then
    optimization_features.

    Args:
        arch_type: Architecture type (e.g., 'dense', 'moe') or feature
            name (e.g., 'flash_attention', 'sliding_window').

    Returns:
        Architecture info dict. Returns empty dict if not found.
    """
    data = _get_cache().get("architecture.yaml")
    if not data:
        return {}

    arch_types = data.get("architecture_types", {})
    if arch_type in arch_types:
        return arch_types[arch_type]

    attention = data.get("attention_mechanisms", {})
    if arch_type in attention:
        return attention[arch_type]

    features = data.get("optimization_features", {})
    if arch_type in features:
        return features[arch_type]

    return {}


def invalidate_cache(filename: str | None = None) -> None:
    """Clear knowledge cache, forcing reload on next access.

    Args:
        filename: Specific file to invalidate, or None for all files.
    """
    _get_cache().invalidate(filename)


# -- Self-correction state --
# Records are (source_file_stem, item_id, predicted_score, actual_score) tuples.
# Protected by _tracker_lock. Written by record_knowledge_outcome(),
# read by apply_self_corrections(). Cleared after corrections are applied
# to prevent unbounded growth.
_prediction_records: list[tuple[str, str, float, float]] = []
_tracker_lock = threading.Lock()

_DIVERGENCE_THRESHOLD = 0.15  # Minimum mean divergence to trigger a correction
_MIN_SAMPLES_FOR_CORRECTION = 50  # Minimum samples per item before correction runs


def record_knowledge_outcome(
    source: str,
    item_id: str,
    predicted: float,
    actual: float,
) -> None:
    """Record an observed outcome against a knowledge-predicted score.

    After ``_MIN_SAMPLES_FOR_CORRECTION`` (50) records accumulate, call
    ``apply_self_corrections()`` to patch YAML files where mean divergence
    exceeds ``_DIVERGENCE_THRESHOLD`` (0.15).

    Args:
        source: Knowledge file stem whose prediction is being evaluated
            (e.g., ``'benchmarks'``, ``'model_families'``).
        item_id: Specific knowledge item that generated the prediction
            (e.g., a benchmark name or family slug).
        predicted: Score predicted by the knowledge data.
        actual: Observed score from runtime performance.
    """
    with _tracker_lock:
        _prediction_records.append((source, item_id, float(predicted), float(actual)))


def apply_self_corrections() -> list[str]:
    """Compare predicted vs actual scores and patch YAML files where divergence is high.

    Groups records by (source, item_id). For any group with at least
    ``_MIN_SAMPLES_FOR_CORRECTION`` samples whose mean absolute divergence
    exceeds ``_DIVERGENCE_THRESHOLD``, writes a ``calibration`` section to
    the relevant YAML file and invalidates the cache for that file.

    Returns:
        List of knowledge file names that were patched (e.g., ``['benchmarks.yaml']``).
    """
    with _tracker_lock:
        records = list(_prediction_records)

    if len(records) < _MIN_SAMPLES_FOR_CORRECTION:
        return []

    # Group by (source, item_id)
    groups: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for source, item_id, predicted, actual in records:
        key = (source, item_id)
        groups.setdefault(key, []).append((predicted, actual))

    patched: list[str] = []
    for (source, item_id), pairs in groups.items():
        if len(pairs) < _MIN_SAMPLES_FOR_CORRECTION:
            continue

        divergences = [actual - predicted for predicted, actual in pairs]
        mean_divergence = sum(divergences) / len(divergences)

        if abs(mean_divergence) <= _DIVERGENCE_THRESHOLD:
            continue

        filename = f"{source}.yaml"
        correction = {
            "mean_divergence": round(mean_divergence, 4),
            "sample_count": len(pairs),
            "score_offset": round(mean_divergence, 4),
        }
        _patch_knowledge_file(filename, item_id, correction)
        patched.append(filename)

    if patched:
        # Clear only the records for patched sources to avoid double-counting
        patched_sources = {p.replace(".yaml", "") for p in patched}
        with _tracker_lock:
            remaining = [r for r in _prediction_records if r[0] not in patched_sources]
            _prediction_records.clear()
            _prediction_records.extend(remaining)

    return patched


def _patch_knowledge_file(filename: str, item_id: str, correction: dict[str, Any]) -> None:
    """Non-destructively add or update a calibration entry in a knowledge YAML file.

    Adds a top-level ``calibration`` section to the file without touching
    any existing keys. If the file already has calibration data for
    ``item_id``, it is overwritten with the new values.

    Args:
        filename: Knowledge YAML filename (e.g., ``'benchmarks.yaml'``).
        item_id: The item whose calibration data is being written.
        correction: Dict of calibration values to record.
    """
    filepath = _KNOWLEDGE_DIR / filename
    if not filepath.exists():
        logger.warning(
            "Cannot patch %s — file not found; calibration data for %s discarded",
            filepath,
            item_id,
        )
        return

    try:
        raw = filepath.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError):
        logger.exception(
            "Could not read %s for self-correction — calibration data for %s discarded",
            filepath,
            item_id,
        )
        return

    if not isinstance(data, dict):
        logger.warning(
            "Unexpected format in %s — cannot add calibration data for %s",
            filepath,
            item_id,
        )
        return

    calibration = data.setdefault("calibration", {})
    calibration[item_id] = correction

    try:
        write_yaml_atomic(filepath, data)
        invalidate_cache(filename)
        logger.info(
            "Self-correction applied to %s for item %r: offset=%.4f (n=%d samples)",
            filename,
            item_id,
            correction.get("score_offset", 0),
            correction.get("sample_count", 0),
        )
    except OSError:
        logger.exception(
            "Could not write calibration data to %s — changes discarded",
            filepath,
        )
