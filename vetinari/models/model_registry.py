# SPDX-FileCopyrightText: 2024-2026 Vetinari Contributors
# SPDX-License-Identifier: Apache-2.0
"""Vetinari Unified Model Registry.

================================
Single source of truth for all model state in the system.

All three routing paths (DynamicModelRouter, ModelRelay, PonderEngine) read
from this registry instead of maintaining separate model lists.  The registry:

1. Discovers local GGUF models via the llama-cpp-python adapter
2. Enriches auto-discovered models by inferring capabilities from their name
3. Merges with ``config/models.yaml`` for models that have explicit metadata
4. Tracks runtime state: is_loaded, last_seen, inferred context window, VRAM
5. Handles arbitrary/custom model names (e.g.
   ``qwen3-vl-32b-gemini-heretic-uncensored-thinking``)

Usage::

    from vetinari.models.model_registry import get_model_registry

    registry = get_model_registry()
    registry.refresh()                           # scan local models dir
    models = registry.get_available_models()     # all loaded + configured
    info   = registry.get_model_info("my-model") # dict with capabilities etc.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vetinari.models.model_profiler_data import build_model_artifact_identity, read_metadata
from vetinari.security.fail_closed import PathTraversalError, confine_to_root
from vetinari.security.redaction import redact_text

logger = logging.getLogger(__name__)
_MAX_LOCAL_MODEL_SCAN_FILES = 5000
_MAX_LOCAL_MODEL_SCAN_DEPTH = 8


# ---------------------------------------------------------------------------
# Model capability inference helpers
# ---------------------------------------------------------------------------

# Ordered: more specific patterns first
_CAPABILITY_PATTERNS: list[tuple] = [
    # Vision-language
    (r"\bvl\b|vision.?language|multimodal|visual", ["vision", "coding", "reasoning"]),
    # Uncensored / heretic variants
    (r"uncensored|heretic|unfiltered|abliterated", ["uncensored", "reasoning"]),
    # Thinking / extended CoT
    (r"thinking|cot|chain.?of.?thought", ["reasoning", "analysis"]),
    # Coding specialists
    (r"coder|codestral|deepseek.?coder|starcoder|code.?llama|codegen", ["coding", "fast"]),
    # Math / science
    (r"math|science|stem|minerva", ["reasoning", "analysis"]),
    # Instruction-following / chat
    (r"instruct|chat|assistant", ["coding", "reasoning"]),
    # General catch-alls by family
    (r"qwen\d|qwen3", ["reasoning", "coding"]),
    (r"llama.?3", ["reasoning", "coding"]),
    (r"mistral|mixtral", ["reasoning", "coding"]),
    (r"phi.?\d", ["reasoning", "fast"]),
    (r"gemma", ["reasoning"]),
    (r"yi.?\d", ["reasoning"]),
]

_CONTEXT_PATTERNS: list[tuple] = [
    (r"llama.?3", 131072),  # Llama 3.x default
    (r"qwen3", 32768),
    (r"qwen2\.5", 32768),
    (r"gemma.?2", 8192),
    (r"phi.?3", 4096),
    (r"mistral", 32768),
    (r"mixtral", 32768),
    (r"yi", 4096),
]


def _infer_capabilities(model_id: str) -> list[str]:
    """Infer capability tags from a model ID string."""
    lower = model_id.lower()
    caps: set = set()
    for pattern, tags in _CAPABILITY_PATTERNS:
        if re.search(pattern, lower):
            caps.update(tags)
    # Every model can do basic instruction following / general tasks
    caps.add("general")
    return sorted(caps)


def _infer_context_window(model_id: str) -> int:
    """Infer context window size from model family patterns."""
    lower = model_id.lower()
    for pattern, ctx in _CONTEXT_PATTERNS:
        if re.search(pattern, lower):
            return ctx
    # Conservative default for unknown models
    return 8192


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ModelRegistryEntry:
    """Runtime record for a single model."""

    model_id: str
    display_name: str
    provider: str  # "local" | "openai" | "claude" | …
    capabilities: list[str] = field(default_factory=list)
    context_window: int = 8192
    memory_requirements_gb: int = 8
    quantization: str = "unknown"
    latency_hint: str = "medium"  # "fast" | "medium" | "slow"
    privacy_level: str = "local"  # "local" | "public"
    cost_per_1k_tokens: float = 0.0
    requires_cpu_offload: bool = False
    preferred_for: list[str] = field(default_factory=list)
    is_loaded: bool = False  # currently present in local models directory
    last_seen: float = 0.0  # epoch timestamp
    endpoint: str = ""
    source: str = "discovered"  # "config" | "discovered" | "merged"
    artifact_path: str = ""
    artifact_sha256: str = ""
    artifact_size_bytes: int = 0
    artifact_mtime_ns: int = 0
    gguf_metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"ModelRegistryEntry(model_id={self.model_id!r}, provider={self.provider!r}, "
            f"is_loaded={self.is_loaded!r}, latency_hint={self.latency_hint!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize this ModelRegistryEntry to a plain dictionary.

        Returns:
            JSON-ready registry metadata with compatibility aliases and
            public-safe endpoint and artifact locations.
        """
        endpoint = _public_model_location(self.endpoint, provider=self.provider)
        artifact_path = _public_model_location(self.artifact_path, provider=self.provider)
        return {
            "id": self.model_id,
            "model_id": self.model_id,
            "name": self.display_name,
            "provider": self.provider,
            "capabilities": self.capabilities,
            "context_window": self.context_window,
            "context_len": self.context_window,  # compat alias
            "context_length": self.context_window,  # compat alias
            "memory_requirements_gb": self.memory_requirements_gb,
            "quantization": self.quantization,
            "latency_hint": self.latency_hint,
            "privacy_level": self.privacy_level,
            "cost_per_1k_tokens": self.cost_per_1k_tokens,
            "requires_cpu_offload": self.requires_cpu_offload,
            "preferred_for": self.preferred_for,
            "is_loaded": self.is_loaded,
            "last_seen": self.last_seen,
            "endpoint": endpoint,
            "source": self.source,
            "artifact_path": artifact_path,
            "artifact_path_redacted": bool(self.artifact_path and artifact_path != self.artifact_path),
            "artifact_sha256": self.artifact_sha256,
            "artifact_size_bytes": self.artifact_size_bytes,
            "artifact_mtime_ns": self.artifact_mtime_ns,
            "gguf_metadata": self.gguf_metadata,
            "tags": self.capabilities,  # alias for ponder.py compatibility
        }


ModelInfo = ModelRegistryEntry


def _public_model_location(value: str, *, provider: str) -> str:
    """Return a public-safe model location for route/UI dictionaries."""
    if not value:
        return ""
    if provider == "local":
        return "[REDACTED_PATH]"
    return redact_text(value)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ModelRegistry:
    """Unified model registry with local llama-cpp-python discovery + static config merging."""

    _instance: ModelRegistry | None = None
    _lock = threading.Lock()

    # How often to auto-refresh by scanning the local models directory (seconds)
    REFRESH_INTERVAL = 30

    def __init__(self):
        self._models: dict[str, ModelRegistryEntry] = {}
        self._last_refresh: float = 0.0
        self._refresh_lock = threading.Lock()
        self._models_dir = os.environ.get("VETINARI_MODELS_DIR", "")
        self._config_path = Path(__file__).parent.parent / "config" / "models.yaml"

        # Load static config immediately (no network call)
        self._load_static_config()

    # ------------------------------------------------------------------
    # Singleton
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> ModelRegistry:
        """Get instance.

        Returns:
            The shared ModelRegistry singleton, creating it on first call.
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Config loading
    # ------------------------------------------------------------------

    def _load_static_config(self) -> None:
        """Load models from config/models.yaml without network I/O."""
        if not self._config_path.exists():
            logger.debug("[ModelRegistry] config/models.yaml not found; skipping static load")
            return
        try:
            from vetinari.utils import load_yaml

            cfg = load_yaml(str(self._config_path))
        except Exception as e:
            logger.warning("[ModelRegistry] Failed to load models.yaml: %s", e)
            return

        all_model_configs = cfg.get("models", []) + cfg.get("cloud_models", [])
        for m in all_model_configs:
            mid = m.get("model_id", "")
            if not mid:
                continue
            info = ModelRegistryEntry(
                model_id=mid,
                display_name=m.get("display_name", mid),
                provider=m.get("provider", "local"),
                capabilities=m.get("capabilities", _infer_capabilities(mid)),
                context_window=m.get("context_window", _infer_context_window(mid)),
                memory_requirements_gb=m.get("memory_requirements_gb", 8),
                quantization=m.get("quantization", "unknown"),
                latency_hint=m.get("latency_hint", "medium"),
                privacy_level=m.get("privacy_level", "local"),
                cost_per_1k_tokens=m.get("cost_per_1k_tokens", 0.0),
                requires_cpu_offload=m.get("requires_cpu_offload", False),
                preferred_for=m.get("preferred_for", []),
                endpoint=m.get("endpoint", ""),
                source="config",
                artifact_path=m.get("artifact_path", ""),
                artifact_sha256=m.get("artifact_sha256", ""),
                artifact_size_bytes=m.get("artifact_size_bytes", 0),
                artifact_mtime_ns=m.get("artifact_mtime_ns", 0),
                gguf_metadata=m.get("gguf_metadata", {}),
            )
            self._models[mid] = info

        logger.debug("[ModelRegistry] Loaded %s models from config", len(self._models))

    # ------------------------------------------------------------------
    # Local model discovery
    # ------------------------------------------------------------------

    def refresh(self, force: bool = False) -> None:
        """Scan the local models directory for GGUF files and merge results."""
        now = time.time()
        if not force and (now - self._last_refresh) < self.REFRESH_INTERVAL:
            return

        with self._refresh_lock:
            # Double-check after acquiring lock
            if not force and (now - self._last_refresh) < self.REFRESH_INTERVAL:
                return
            self._models_dir = os.environ.get("VETINARI_MODELS_DIR", self._models_dir)
            self._do_refresh()
            self._last_refresh = time.time()

    def _do_refresh(self) -> None:
        """Internal: scan the local models directory and update registry."""
        models_dir = Path(self._models_dir) if self._models_dir else None

        # Mark all previously-discovered local models as unloaded
        for info in self._models.values():
            if info.provider == "local" and info.source == "discovered":
                info.is_loaded = False

        if models_dir is None or not models_dir.is_dir():
            logger.debug("[ModelRegistry] VETINARI_MODELS_DIR not set or not a directory; skipping local scan")
            return

        discovered_ids: set[str] = set()
        scanned = 0
        root = models_dir.resolve()
        for dirpath, dirnames, filenames in os.walk(root):
            current = Path(dirpath)
            try:
                depth = len(current.relative_to(root).parts)
            except ValueError:
                logger.warning("[ModelRegistry] Skipping model scan path outside root: %s", current)
                continue
            if depth >= _MAX_LOCAL_MODEL_SCAN_DEPTH:
                dirnames[:] = []
            else:
                dirnames[:] = [name for name in dirnames if not name.startswith(".")]
            for filename in filenames:
                scanned += 1
                if scanned > _MAX_LOCAL_MODEL_SCAN_FILES:
                    logger.warning(
                        "[ModelRegistry] Local model scan stopped after %d files under %s",
                        _MAX_LOCAL_MODEL_SCAN_FILES,
                        root,
                    )
                    break
                gguf_path = current / filename
                if gguf_path.suffix.lower() != ".gguf":
                    continue
                try:
                    gguf_path = confine_to_root(root, gguf_path.relative_to(root))
                except PathTraversalError as exc:
                    logger.warning("[ModelRegistry] Skipping model path outside root: %s", exc)
                    continue
                # Use the stem (filename without .gguf) as the model ID
                mid = gguf_path.stem
                if not mid:
                    continue
                artifact_identity = self._artifact_identity(gguf_path)
                if not artifact_identity.get("artifact_sha256"):
                    logger.warning(
                        "[ModelRegistry] Skipping unreadable GGUF artifact without provenance: %s", gguf_path
                    )
                    continue
                metadata_dict, actual_quantization = self._gguf_header_evidence(gguf_path)
                discovered_ids.add(mid)
                if mid in self._models:
                    existing = self._models[mid]
                    existing.is_loaded = True
                    existing.last_seen = time.time()
                    existing.endpoint = str(gguf_path)
                    existing.artifact_path = artifact_identity["artifact_path"]
                    existing.artifact_sha256 = artifact_identity["artifact_sha256"]
                    existing.artifact_size_bytes = artifact_identity["artifact_size_bytes"]
                    existing.artifact_mtime_ns = artifact_identity["artifact_mtime_ns"]
                    existing.gguf_metadata = metadata_dict
                    if actual_quantization:
                        existing.quantization = actual_quantization
                    if existing.source == "discovered":
                        existing.capabilities = _infer_capabilities(mid)
                        existing.context_window = _infer_context_window(mid)
                else:
                    from vetinari.utils import estimate_model_memory_gb

                    self._models[mid] = ModelRegistryEntry(
                        model_id=mid,
                        display_name=mid,
                        provider="local",
                        capabilities=_infer_capabilities(mid),
                        context_window=_infer_context_window(mid),
                        memory_requirements_gb=estimate_model_memory_gb(mid),
                        quantization=actual_quantization or self._infer_quantization(mid),
                        latency_hint=self._infer_latency(mid),
                        privacy_level="local",
                        cost_per_1k_tokens=0.0,
                        is_loaded=True,
                        last_seen=time.time(),
                        endpoint=str(gguf_path),
                        source="discovered",
                        artifact_path=artifact_identity["artifact_path"],
                        artifact_sha256=artifact_identity["artifact_sha256"],
                        artifact_size_bytes=artifact_identity["artifact_size_bytes"],
                        artifact_mtime_ns=artifact_identity["artifact_mtime_ns"],
                        gguf_metadata=metadata_dict,
                    )
            if scanned > _MAX_LOCAL_MODEL_SCAN_FILES:
                break

        logger.debug(
            "[ModelRegistry] Refreshed: %d discovered in %s, %d total in registry",
            len(discovered_ids),
            models_dir,
            len(self._models),
        )

    # ------------------------------------------------------------------
    # Capability / latency inference helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _artifact_identity(gguf_path: Path) -> dict[str, Any]:
        try:
            return build_model_artifact_identity(gguf_path)
        except OSError as exc:
            logger.warning("[ModelRegistry] Failed to fingerprint %s: %s", gguf_path, exc)
            return {
                "artifact_path": str(gguf_path.resolve()),
                "artifact_sha256": "",
                "artifact_size_bytes": 0,
                "artifact_mtime_ns": 0,
            }

    @staticmethod
    def _gguf_header_evidence(gguf_path: Path) -> tuple[dict[str, Any], str]:
        try:
            metadata = read_metadata(gguf_path)
        except Exception as exc:
            logger.warning("[ModelRegistry] Failed to read GGUF metadata for %s: %s", gguf_path, exc)
            return {}, ""
        metadata_dict = {
            "architecture": metadata.architecture,
            "block_count": metadata.block_count,
            "head_count": metadata.head_count,
            "head_count_kv": metadata.head_count_kv,
            "context_length": metadata.context_length,
            "expert_count": metadata.expert_count,
            "expert_used_count": metadata.expert_used_count,
            "file_type": metadata.file_type,
            "embedding_length": metadata.embedding_length,
            "vocab_size": metadata.vocab_size,
            "quantization": metadata.quantization,
            "file_size_gb": metadata.file_size_gb,
        }
        return metadata_dict, metadata.quantization

    @staticmethod
    def _infer_quantization(model_id: str) -> str:
        lower = model_id.lower()
        for q in ["q8", "q6", "q5_k_m", "q5_k_s", "q5", "q4_k_m", "q4_k_s", "q4", "q3", "f16", "f32"]:
            if q in lower:
                return q
        return "q4_k_m"  # reasonable default for most GGUF models

    @staticmethod
    def _infer_latency(model_id: str) -> str:
        lower = model_id.lower()
        # Large models are slow regardless of hardware
        if re.search(r"70b|72b|65b", lower):
            return "slow"
        if re.search(r"30b|32b|33b|34b", lower):
            return "medium"
        if re.search(r"7b|8b|14b|13b", lower):
            return "fast"
        if re.search(r"1b|2b|3b|4b", lower):
            return "fast"
        return "medium"

    @staticmethod
    def _build_auth_headers() -> dict[str, str]:
        """Return empty auth headers — local inference requires no authentication.

        Returns:
            Empty dict.
        """
        return {}

    # ------------------------------------------------------------------
    # Public query API
    # ------------------------------------------------------------------

    def get_available_models(
        self,
        provider: str | None = None,
        loaded_only: bool = False,
        capability: str | None = None,
    ) -> list[ModelRegistryEntry]:
        """Return models matching optional filters.

        Args:
            provider:    Filter to a specific provider (``"local"``, ``"openai"``, …)
            loaded_only: Only return models currently present in the local models directory
            capability:  Only return models with this capability tag

        Returns:
            List of results.
        """
        self.refresh()
        results = []
        for info in self._models.values():
            if provider and info.provider != provider:
                continue
            if loaded_only and not info.is_loaded:
                continue
            if capability and capability not in info.capabilities:
                continue
            results.append(info)
        return results

    def get_loaded_local_models(self) -> list[ModelRegistryEntry]:
        """Return all models currently present in the local models directory."""
        return self.get_available_models(provider="local", loaded_only=True)

    def get_model_info(self, model_id: str, *, refresh: bool = True) -> ModelRegistryEntry | None:
        """Return info for a specific model ID, refreshing if needed.

        Args:
            model_id: The model identifier to look up.
            refresh: When True (default) trigger a registry refresh first so
                newly-added local GGUFs are picked up. Set False for advisory
                lookups on the hot path where the cost of a refresh (full
                directory scan + SHA-256 of every GGUF) would dominate the
                request.

        Returns:
            The ModelInfo for the given model_id, or None if the model is not
            registered (including after a fresh registry refresh).
        """
        if refresh:
            self.refresh()
        return self._models.get(model_id)

    def get_all_as_dicts(self) -> list[dict[str, Any]]:
        """Return all models as plain dicts (compatible with legacy routing code).

        Returns:
            A list of dicts, one per registered model, produced by
            ``ModelInfo.to_dict()``. Triggers a refresh so the list reflects
            the current state of the models directory and config.
        """
        self.refresh()
        return [m.to_dict() for m in self._models.values()]

    def get_loaded_as_dicts(self) -> list[dict[str, Any]]:
        """Return currently-present local models as plain dicts."""
        return [m.to_dict() for m in self.get_loaded_local_models()]

    def list_loaded_models(self) -> list[dict[str, Any]]:
        """Compat shim for ponder.py which calls adapter.list_loaded_models()."""
        return self.get_loaded_as_dicts()

    def register_model(self, info: ModelRegistryEntry) -> None:
        """Manually register or override a model entry."""
        self._models[info.model_id] = info

    def get_registry_stats(self) -> dict[str, Any]:
        """Return a summary of registry state.

        Returns:
            A dict with keys: ``total`` (all registered models), ``loaded``
            (models currently marked as loaded), ``local`` and ``cloud``
            (counts by provider type), ``loaded_ids`` (list of loaded model
            IDs), and ``last_refresh`` (timestamp of the most recent scan).
        """
        all_models = list(self._models.values())
        loaded = [m for m in all_models if m.is_loaded]
        return {
            "total": len(all_models),
            "loaded": len(loaded),
            "local": len([m for m in all_models if m.provider == "local"]),
            "cloud": len([m for m in all_models if m.provider != "local"]),
            "loaded_ids": [m.model_id for m in loaded],
            "last_refresh": self._last_refresh,
        }


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------

_registry: ModelRegistry | None = None
_registry_lock = threading.Lock()


def get_model_registry() -> ModelRegistry:
    """Return the global ModelRegistry singleton (created lazily).

    Returns:
        The module-level ModelRegistry singleton. The instance is shared
        across all callers and created on first access under a lock.
    """
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ModelRegistry.get_instance()
    return _registry
