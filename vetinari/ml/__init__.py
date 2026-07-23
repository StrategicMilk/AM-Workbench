"""Lightweight ML Model Infrastructure — vetinari.ml.

Small, fast ML models for classification, regression, and embedding tasks
that replace expensive LLM calls. NOT for LLM inference — just for
lightweight classifiers, regressors, and embedders.

Submodules:
  - quality_prescreener: Three-tier quality pre-screening
  - classifiers: Goal, defect, and ambiguity classifiers (US-212)
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from vetinari.exceptions import ConfigurationError, ModelNotFoundError
from vetinari.ml.classifiers import (
    AmbiguityDetector,
    AmbiguityResult,
    DefectClassification,
    DefectClassifier,
    GoalClassification,
    GoalClassifier,
)
from vetinari.ml.task_classifier import TaskClassifier

logger = logging.getLogger(__name__)
_TRUSTED_ARTIFACT_ROOT = (Path(__file__).resolve().parent / "models").resolve()


def _module_is_available(module_name: str) -> bool:
    """Return True when an ML backend dependency is discoverable without importing it."""
    if module_name in sys.modules:
        return sys.modules[module_name] is not None
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ModuleNotFoundError, ValueError):
        logger.warning("Exception handled by  module is available fallback", exc_info=True)
        return False


@dataclass(frozen=True, slots=True)
class MLModelInfo:
    """Metadata about a loaded ML model.

    Args:
        name: Model identifier.
        model_type: Type of model (sklearn, onnx, sentence_transformer).
        loaded: Whether the model is currently loaded.
    """

    name: str = ""
    model_type: str = ""
    loaded: bool = False


ModelInfo = MLModelInfo


@dataclass(frozen=True, slots=True)
class _EngineEmbeddingModel:
    """Small ``encode`` adapter over the canonical AM Engine client."""

    model_id: str

    def encode(self, inputs: Any) -> Any:
        """Encode input through the supervised AM Engine.

        Returns:
            One vector for string input, otherwise vectors in input order.
        """
        from vetinari.engine.client import EmbeddingsRequest, get_engine_client

        single = isinstance(inputs, str)
        items = (inputs,) if single else tuple(str(item) for item in inputs)
        response = get_engine_client().embeddings(EmbeddingsRequest(items, model_id=self.model_id))
        vectors = [[float(value) for value in vector] for vector in response.vectors]
        return vectors[0] if single else vectors


class MLModelRegistry:
    """Registry for lightweight ML models (classifiers, regressors, embedders).

    Manages loading, caching, and inference for small ML models used to
    replace LLM calls for non-generative tasks.
    """

    def __init__(self) -> None:
        self._models: dict[str, Any] = {}
        self._model_info: dict[str, ModelInfo] = {}
        self._lock = threading.Lock()

    def load(self, name: str, model_path: Path) -> Any:
        """Load a small ML model or register an engine-backed embedding model.

        Args:
            name: Identifier for the model.
            model_path: Path to the model file or directory.

        Returns:
            The loaded model object.

        Raises:
            ModelNotFoundError: If the model path doesn't exist.
            ValueError: If the model format is not supported.
        """
        with self._lock:
            if name in self._models:
                return self._models[name]

            if not model_path.exists():
                raise ModelNotFoundError(f"Model not found: {model_path}")

            model = None
            model_type = "unknown"

            if model_path.suffix == ".onnx":
                trusted_model_path = _resolve_trusted_model_path(model_path)
                _verify_file_sha256(trusted_model_path)
                if not _module_is_available("onnxruntime"):
                    raise ConfigurationError("onnxruntime not installed — cannot load ONNX model")
                import onnxruntime as ort

                model = ort.InferenceSession(str(trusted_model_path))
                model_type = "onnx"

            elif model_path.suffix == ".joblib":
                if not _module_is_available("joblib"):
                    raise ConfigurationError("joblib not installed — cannot load sklearn model")
                import joblib

                trusted_model_path = _resolve_trusted_model_path(model_path)
                _verify_file_sha256(trusted_model_path)
                model = joblib.load(trusted_model_path)
                model_type = "sklearn"

            elif model_path.is_dir():
                trusted_model_path = _resolve_trusted_model_path(model_path)
                _verify_directory_manifest(trusted_model_path)
                model = _EngineEmbeddingModel(name)
                model_type = "engine_embedding"
            else:
                raise ConfigurationError(f"Unsupported model format: {model_path.suffix}")

            self._models[name] = model
            self._model_info[name] = ModelInfo(name=name, model_type=model_type, loaded=True)
            logger.info("Loaded ML model %s (%s) from %s", name, model_type, model_path)
            return model

    def predict(self, name: str, inputs: Any) -> Any:
        """Run inference on a loaded model.

        Args:
            name: Model identifier.
            inputs: Model-specific input data.

        Returns:
            Model predictions.

        Raises:
            KeyError: If the model is not loaded.
        """
        with self._lock:
            model = self._models.get(name)

        if model is None:
            raise KeyError(f"Model '{name}' not loaded. Call load() first.")

        if hasattr(model, "predict"):
            return model.predict(inputs)
        if hasattr(model, "encode"):
            return model.encode(inputs)
        raise TypeError(f"Model '{name}' has no predict() or encode() method")

    def is_loaded(self, name: str) -> bool:
        """Check if a model is loaded.

        Args:
            name: Model identifier.

        Returns:
            True if the model is loaded.
        """
        with self._lock:
            return name in self._models

    def unload(self, name: str) -> None:
        """Unload a model to free memory.

        Args:
            name: Model identifier.
        """
        with self._lock:
            self._models.pop(name, None)
            info = self._model_info.get(name)
            if info:
                self._model_info[name] = replace(info, loaded=False)

    def get_status(self) -> dict[str, Any]:
        """Return registry status.

        Returns:
            Dictionary with loaded model information.
        """
        with self._lock:
            return {
                "loaded_models": list(self._models.keys()),
                "model_count": len(self._models),
                "models": {
                    name: {"type": info.model_type, "loaded": info.loaded} for name, info in self._model_info.items()
                },
            }


def _resolve_trusted_model_path(model_path: Path) -> Path:
    """Resolve a model path and require it to live under the packaged ML model root."""
    resolved = model_path.resolve()
    try:
        resolved.relative_to(_TRUSTED_ARTIFACT_ROOT)
    except ValueError as exc:
        raise ConfigurationError(f"Joblib model path is outside trusted model root: {resolved}") from exc
    return resolved


def _verify_file_sha256(path: Path) -> None:
    """Verify a model artifact against its adjacent sha256 metadata file."""
    digest_path = path.with_suffix(path.suffix + ".sha256")
    if not digest_path.exists():
        raise ConfigurationError(f"Missing sha256 metadata for model artifact: {path}")
    expected = digest_path.read_text(encoding="utf-8").split()[0].lower()
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ConfigurationError(f"Model artifact digest mismatch: {path}")


def _verify_directory_manifest(path: Path) -> None:
    """Verify a directory-backed model against a package-local sha256 manifest."""
    manifest = path / "manifest.sha256"
    if not manifest.exists():
        raise ConfigurationError(f"Missing sha256 manifest for model directory: {path}")
    for raw_line in manifest.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        expected, separator, relative = line.partition("  ")
        if not expected or not separator or not relative:
            raise ConfigurationError(f"Malformed sha256 manifest line for {path}: {line!r}")
        artifact = (path / relative).resolve()
        try:
            artifact.relative_to(path)
        except ValueError as exc:
            raise ConfigurationError(f"Model manifest references path outside trusted root: {relative}") from exc
        if not artifact.is_file():
            raise ConfigurationError(f"Model manifest references missing artifact: {relative}")
        actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
        if actual != expected:
            raise ConfigurationError(f"Model artifact digest mismatch: {artifact}")


# Singleton
_ml_registry: MLModelRegistry | None = None
_ml_lock = threading.Lock()


def get_ml_registry() -> MLModelRegistry:
    """Return the singleton MLModelRegistry instance.

    Returns:
        The shared MLModelRegistry instance.
    """
    global _ml_registry
    if _ml_registry is None:
        with _ml_lock:
            if _ml_registry is None:
                _ml_registry = MLModelRegistry()
    return _ml_registry


def reset_ml_registry() -> None:
    """Reset singleton for testing."""
    global _ml_registry
    with _ml_lock:
        _ml_registry = None


__all__ = [
    "AmbiguityDetector",
    "AmbiguityResult",
    "DefectClassification",
    "DefectClassifier",
    "GoalClassification",
    "GoalClassifier",
    "MLModelRegistry",
    "ModelInfo",
    "TaskClassifier",
    "get_ml_registry",
    "reset_ml_registry",
]
