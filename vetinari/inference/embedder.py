"""Compatibility facade for supervised AM Engine embeddings.

Load ordering: CpuEmbedder.load() MUST be called after InProcessCpuTier.load()
completes to avoid disk I/O contention during weight loading (BRAINSTORM
Decision 12).
"""

from __future__ import annotations

import logging
import sys
import threading
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import yaml

from vetinari.contracts import ConfigContractViolation
from vetinari.inference.request import EmbedderConfig
from vetinari.workbench.effective_config import capture_embedding_config_snapshot

logger = logging.getLogger(__name__)
engine_embedding_fallbacks_total = 0

# Side effects:
#   - _embedder_instance: CpuEmbedder | None is a module-level singleton.
#   - _embedder_lock: threading.Lock guards singleton init and reset.
_embedder_instance: CpuEmbedder | None = None
_embedder_lock = threading.Lock()


class CpuEmbedder:
    """Engine-first embedder with an opt-in legacy in-process fallback."""

    def __init__(self, config: EmbedderConfig | dict) -> None:
        self._config = config
        self._model_id = config.model_id if isinstance(config, EmbedderConfig) else config.get("embed_model")
        if not self._model_id:
            raise KeyError("CpuEmbedder config requires 'embed_model'")
        _validate_pinned_embedding_model(self._model_id)
        self._model: Any | None = None
        self._loaded = False
        self.last_effective_config_snapshot_id: str | None = None

    def load(self) -> None:
        """Prepare the embedder and, when installed, its compatibility fallback.

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if "sentence_transformers" not in sys.modules and find_spec("sentence_transformers") is None:
            self._model = None
        else:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_id)
        self._loaded = True

    def embed(self, text: str) -> list[float]:
        """Embed one string as a float vector.

        Returns:
            list[float] value produced by embed().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not self._loaded:
            raise RuntimeError("CpuEmbedder.load() must be called before embed()")
        snapshot = capture_embedding_config_snapshot(self._config, accepted=True)
        self.last_effective_config_snapshot_id = snapshot.snapshot_id
        try:
            from vetinari.engine.client import EmbeddingsRequest, get_engine_client

            response = get_engine_client().embeddings(EmbeddingsRequest((text,), model_id=self._model_id))
            return _to_float_vector(response.vectors[0])
        except Exception as exc:
            _record_engine_fallback()
            logger.warning(
                "AM Engine single embedding unavailable; using resident compatibility model",
                extra={"fallback_type": "resident_sentence_transformer", "exc_class": type(exc).__name__},
            )
            if self._model is None:
                raise RuntimeError("AM Engine embedding unavailable and no compatibility fallback is loaded") from exc
            return _to_float_vector(self._model.encode(text))

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed strings in input order.

        Returns:
            list[list[float]] value produced by embed_batch().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not self._loaded:
            raise RuntimeError("CpuEmbedder.load() must be called before embed_batch()")
        snapshot = capture_embedding_config_snapshot(self._config, accepted=True)
        self.last_effective_config_snapshot_id = snapshot.snapshot_id
        try:
            from vetinari.engine.client import EmbeddingsRequest, get_engine_client

            response = get_engine_client().embeddings(EmbeddingsRequest(tuple(texts), model_id=self._model_id))
            return [_to_float_vector(vector) for vector in response.vectors]
        except Exception as exc:
            _record_engine_fallback()
            logger.warning(
                "AM Engine batch embedding unavailable; using resident compatibility model",
                extra={"fallback_type": "resident_sentence_transformer", "exc_class": type(exc).__name__},
            )
            if self._model is None:
                raise RuntimeError("AM Engine embedding unavailable and no compatibility fallback is loaded") from exc
            return [_to_float_vector(vector) for vector in self._model.encode(texts)]


def _record_engine_fallback() -> None:
    global engine_embedding_fallbacks_total
    engine_embedding_fallbacks_total += 1


def _to_float_vector(value: Any) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    return [float(item) for item in value]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _pinned_embedding_model_ids() -> set[str]:
    root = _project_root()
    pinned: set[str] = set()
    cpu_tier = root / "config" / "cpu_tier.yaml"
    if cpu_tier.exists():
        data = yaml.safe_load(cpu_tier.read_text(encoding="utf-8")) or {}
        model_id = data.get("embed_model") if isinstance(data, dict) else None
        if isinstance(model_id, str) and model_id:
            pinned.add(model_id)
        for model_id in data.get("pinned_embedding_models", []) if isinstance(data, dict) else []:
            if isinstance(model_id, str) and model_id:
                pinned.add(model_id)
    backend_pins = root / "config" / "backend_pins.yaml"
    if backend_pins.exists():
        data = yaml.safe_load(backend_pins.read_text(encoding="utf-8")) or {}
        backends = data.get("backends", {}) if isinstance(data, dict) else {}
        if isinstance(backends, dict):
            for entry in backends.values():
                if not isinstance(entry, dict):
                    continue
                review = entry.get("release_license_review")
                if isinstance(review, dict) and isinstance(review.get("model_id"), str):
                    pinned.add(review["model_id"])
                for model_id in entry.get("upstream_image_required_for", []) or []:
                    if isinstance(model_id, str):
                        pinned.add(model_id)
    return pinned


def _validate_pinned_embedding_model(model_id: str) -> None:
    pinned = _pinned_embedding_model_ids()
    if model_id not in pinned:
        raise ConfigContractViolation(
            path=_project_root() / "config" / "cpu_tier.yaml",
            reason=f"embedding model {model_id!r} is not declared in the pinned model registry",
        )


def get_cpu_embedder(config: EmbedderConfig | dict) -> CpuEmbedder:
    """Return the process singleton CPU embedder.

    Returns:
        Resolved cpu embedder value.

    Raises:
        ConfigContractViolation: If the singleton is reused with incompatible config.
    """
    global _embedder_instance
    with _embedder_lock:
        if _embedder_instance is not None:
            if _embedder_instance._config != config:
                raise ConfigContractViolation(
                    path=Path("<embedder-singleton>"),
                    reason=(
                        "get_cpu_embedder called with differing config after "
                        "singleton creation; call reset_cpu_embedder() first"
                    ),
                )
            return _embedder_instance
        _embedder_instance = CpuEmbedder(config)
        return _embedder_instance


def reset_cpu_embedder() -> None:
    """Reset the process embedder singleton (test/reconfiguration use)."""
    global _embedder_instance
    with _embedder_lock:
        _embedder_instance = None


__all__ = ["CpuEmbedder", "get_cpu_embedder", "reset_cpu_embedder"]
