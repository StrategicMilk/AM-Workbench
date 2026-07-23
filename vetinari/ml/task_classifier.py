"""TF-IDF + logistic regression task classifier — replaces LLM goal classification.

Classifies user task descriptions into GoalCategory values. When scikit-learn
is unavailable or fewer than MIN_TRAINING_EXAMPLES samples have been collected,
falls back to keyword matching (same logic as GoalClassifier in classifiers.py).

This is the primary classifier: LLM is only consulted as fallback when
model confidence < CONFIDENCE_THRESHOLD.

Pipeline role: Called by classify_goal_via_llm() in llm_helpers.py before
any LLM call is made.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from vetinari.exceptions import SecurityError
from vetinari.paths import resolve_state_path
from vetinari.privacy import require_privacy_envelope, wrap_for_persistence

logger = logging.getLogger(__name__)


# Minimum labeled examples required before the trained model is used instead of keywords
MIN_TRAINING_EXAMPLES = 100

# Confidence below this threshold triggers LLM fallback
CONFIDENCE_THRESHOLD = 0.6

# Saved model location under the canonical Vetinari state root.
_MODEL_DIR = resolve_state_path("models")
_MODEL_PATH = _MODEL_DIR / "task_classifier.pkl"
_DEFAULT_MODEL_PATH = _MODEL_PATH
_MODEL_HASH_REGISTRY: dict[str, str] = {}
_ENABLE_PARALLEL_PYTEST_DISK_MODEL_ENV = "VETINARI_ENABLE_TASK_CLASSIFIER_XDIST_DISK_MODEL"
_PYTEST_PARALLEL_WORKER_ENVS = ("PYTEST_XDIST_WORKER", "VETINARI_PYTEST_SHARD_INDEX")
_TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


class TaskClassifier:
    """Classifies task descriptions into GoalCategory values.

    Uses TF-IDF + LogisticRegression when at least MIN_TRAINING_EXAMPLES
    labeled examples exist and scikit-learn is available.  Falls back to
    the keyword-matching GoalClassifier otherwise.

    Thread-safe: the internal sklearn model is protected by a lock and
    loaded/trained lazily on the first classify() call.

    Example::

        clf = TaskClassifier()
        category, confidence = clf.classify("implement a binary search tree")
        # -> ("code", 0.87)
    """

    def __init__(self) -> None:
        # sklearn objects; None until lazy-loaded
        self._model: Any = None
        self._vectorizer: Any = None
        self._training_examples: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        self._sklearn_available: bool | None = None  # None = not yet probed
        self._keyword_classifier: Any = None  # cached GoalClassifier for keyword fallback
        self._model_proof: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self, text: str) -> tuple[str, float]:
        """Classify a task description into a GoalCategory.

        Checks whether the trained model should be used (enough examples and
        sklearn available).  Falls back to keyword matching otherwise.

        Args:
            text: The task description to classify.

        Returns:
            Tuple of (category_string, confidence_score_0_to_1).
        """
        if not text or not text.strip():
            return "general", 0.3

        if self._should_use_trained_model():
            result = self._classify_with_model(text)
            if result is not None:
                return result

        return self._classify_with_keywords(text)

    def add_example(self, text: str, label: str) -> None:
        """Add a labeled training example.

        When the total reaches MIN_TRAINING_EXAMPLES, the next classify()
        call will train and cache the sklearn model automatically.

        Args:
            text: The task description text.
            label: The GoalCategory value string (e.g. "code", "research").
        """
        with self._lock:
            self._training_examples.append((text, label))
        logger.debug(
            "TaskClassifier: added example (label=%r, total=%d)",
            label,
            len(self._training_examples),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _should_use_trained_model(self) -> bool:
        """Return True when sklearn is available and a usable model is ready.

        A model is considered usable when either:
        - It is already loaded in memory (self._model is not None), or
        - Enough in-memory training examples exist to train a new one.

        A persisted model file on disk is NOT sufficient on its own because the
        file may be corrupt, incompatible, or left over from a previous run that
        never trained successfully.  The caller will attempt to load from disk
        inside ``_classify_with_model``; this method only gates the fast-path
        decision to bypass the keyword fallback entirely.
        """
        if self._sklearn_available is None:
            self._sklearn_available = find_spec("sklearn") is not None
            if not self._sklearn_available:
                self._sklearn_available = False
                logger.debug("TaskClassifier: scikit-learn not installed — using keyword fallback")

        if not self._sklearn_available:
            return False

        # Only bypass keyword fallback when a model is already in memory or
        # we have enough examples to train one.  A stale disk file alone is not
        # enough — the load attempt may fail, and if training also fails (too
        # few examples) we would return nothing useful.
        return (
            self._model is not None
            or len(self._training_examples) >= MIN_TRAINING_EXAMPLES
            or (
                _implicit_persisted_model_load_allowed() and _MODEL_PATH.exists() and _digest_path(_MODEL_PATH).exists()
            )
        )

    def _classify_with_model(self, text: str) -> tuple[str, float] | None:
        """Classify using the trained sklearn model, training it if necessary.

        Args:
            text: The task description to classify.

        Returns:
            Tuple of (category, confidence), or None if training/inference fails.
        """
        with self._lock:
            # Try loading from disk first; if that fails, train from scratch
            if self._model is None and not self._try_load_from_disk() and not self._train_model():
                return None
            if not self._model_proof_allows_probability():
                return None

        try:
            features = self._vectorizer.transform([text])
            probas = self._model.predict_proba(features)[0]
            best_idx = int(probas.argmax())
            confidence = float(probas[best_idx])
            category = self._model.classes_[best_idx]
            logger.debug(
                "TaskClassifier: ML result category=%r confidence=%.3f",
                category,
                confidence,
            )
            return str(category), confidence
        except Exception as exc:
            logger.warning(
                "TaskClassifier: ML inference failed (%s) — falling back to keywords",
                exc,
            )
            return None

    def _train_model(self) -> bool:
        """Train TF-IDF + LogisticRegression on current examples.

        Must be called inside self._lock.

        Returns:
            True if training succeeded, False otherwise.
        """
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.linear_model import LogisticRegression

            texts = [ex[0] for ex in self._training_examples]
            labels = [ex[1] for ex in self._training_examples]

            vectorizer = TfidfVectorizer(ngram_range=(1, 2), max_features=10_000)
            features = vectorizer.fit_transform(texts)

            model = LogisticRegression(max_iter=1000, C=1.0)
            model.fit(features, labels)

            self._vectorizer = vectorizer
            self._model = model
            self._model_proof = self._build_model_proof(
                features=features,
                labels=labels,
                sample_count=len(texts),
                class_count=len(set(labels)),
            )

            logger.info(
                "TaskClassifier: trained on %d examples (%d classes)",
                len(texts),
                len(set(labels)),
            )
            self._save_to_disk()
            return True
        except Exception as exc:
            logger.warning("TaskClassifier: training failed: %s", exc)
            return False

    def _try_load_from_disk(self) -> bool:
        """Load previously saved model from disk.

        Must be called inside self._lock.

        Returns:
            True if a saved model was loaded successfully.
        """
        if not _implicit_persisted_model_load_allowed():
            logger.debug("TaskClassifier: ambient persisted model loading disabled in parallel pytest worker")
            return False
        if not _MODEL_PATH.exists():
            return False
        try:
            import joblib

            _verify_file_sha256(_MODEL_PATH)
            record = joblib.load(_MODEL_PATH)
            payload = require_privacy_envelope(record)["payload"]
            if not isinstance(payload, dict):
                raise SecurityError("task_classifier model payload is not an object")
            proof = self._load_model_proof(payload)
            self._model = payload["model"]
            self._vectorizer = payload["vectorizer"]
            self._model_proof = proof
            logger.info("TaskClassifier: loaded saved model from %s", _MODEL_PATH)
            return True
        except Exception as exc:
            logger.warning("TaskClassifier: could not load saved model: %s", exc)
            return False

    def _save_to_disk(self) -> None:
        """Persist the trained model to disk for reuse across process restarts.

        Must be called inside self._lock.
        """
        try:
            import joblib

            _MODEL_DIR.mkdir(parents=True, exist_ok=True)
            proof = self._model_proof or {
                "evaluation": {"status": "unavailable", "reason": "training_proof_not_recorded"},
                "calibration": {"status": "unavailable", "reason": "training_proof_not_recorded"},
            }
            payload = wrap_for_persistence(
                {
                    "model": self._model,
                    "vectorizer": self._vectorizer,
                    "evaluation": proof["evaluation"],
                    "calibration": proof["calibration"],
                },
                privacy_class="subject_data",
                subject_id="task-classifier-local-vocabulary",
                retention_days=30,
                source="ml.task_classifier",
                redaction_applied=False,
            )
            joblib.dump(payload, _MODEL_PATH)
            _write_file_sha256(_MODEL_PATH)
            logger.info("TaskClassifier: saved model to %s", _MODEL_PATH)
        except Exception as exc:
            logger.warning("TaskClassifier: could not save model: %s", exc)

    def _build_model_proof(
        self,
        *,
        features: Any,
        labels: list[str],
        sample_count: int,
        class_count: int,
    ) -> dict[str, Any]:
        """Build local proof metadata for persisted probability outputs."""
        train_accuracy = float(self._model.score(features, labels))
        return {
            "evaluation": {
                "method": "training-set-resubstitution",
                "accuracy": train_accuracy,
                "sample_count": sample_count,
                "class_count": class_count,
            },
            "calibration": {
                "method": "logistic_regression_predict_proba",
                "confidence_threshold": CONFIDENCE_THRESHOLD,
                "sample_count": sample_count,
                "class_count": class_count,
            },
        }

    def _load_model_proof(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Load persisted proof metadata without authorizing probability use."""
        evaluation = payload.get("evaluation")
        calibration = payload.get("calibration")
        if not isinstance(evaluation, dict) or not isinstance(calibration, dict):
            raise SecurityError("task_classifier model missing evaluation/calibration proof")
        return {"evaluation": evaluation, "calibration": calibration}

    def _model_proof_allows_probability(self) -> bool:
        """Return whether the restored model proof is strong enough for ML inference."""
        proof = self._model_proof
        if not isinstance(proof, dict):
            return False
        evaluation = proof.get("evaluation")
        calibration = proof.get("calibration")
        if not isinstance(evaluation, dict) or not isinstance(calibration, dict):
            return False
        if not isinstance(evaluation.get("sample_count"), int) or evaluation["sample_count"] < MIN_TRAINING_EXAMPLES:
            logger.warning("TaskClassifier: saved model proof has insufficient sample count; using keyword fallback")
            return False
        if calibration.get("method") != "logistic_regression_predict_proba":
            logger.warning(
                "TaskClassifier: saved model proof has unsupported calibration method; using keyword fallback"
            )
            return False
        return True

    def _classify_with_keywords(self, text: str) -> tuple[str, float]:
        """Fall back to keyword-based GoalClassifier.

        The GoalClassifier instance is created once and reused — it holds no
        per-call state so caching it is safe and avoids repeated object allocation.

        Args:
            text: The task description to classify.

        Returns:
            Tuple of (category, confidence) from the keyword classifier.
        """
        if self._keyword_classifier is None:
            from vetinari.ml.classifiers import GoalClassifier

            self._keyword_classifier = GoalClassifier()
        result = self._keyword_classifier.classify(text)
        return result.category, result.confidence


def _digest_path(path: Path) -> Path:
    """Return the adjacent sha256 metadata path for a model artifact."""
    return path.with_suffix(path.suffix + ".sha256")


def _implicit_persisted_model_load_allowed() -> bool:
    """Return whether default persisted model loading is safe on this process.

    Parallel pytest workers on Windows can concurrently import
    sklearn/pandas/pyarrow native extensions when a default joblib model is
    implicitly loaded during generic goal-routing tests.  This applies both to
    pytest-xdist and Vetinari's native shard runner.  Explicit model-load tests
    monkeypatch ``_MODEL_PATH`` to a temp path and still exercise
    ``_try_load_from_disk``; only the ambient default-model fast path is
    disabled unless opted in.
    """
    if _MODEL_PATH != _DEFAULT_MODEL_PATH:
        return True
    if not any(os.environ.get(name) for name in _PYTEST_PARALLEL_WORKER_ENVS):
        return True
    return os.environ.get(_ENABLE_PARALLEL_PYTEST_DISK_MODEL_ENV, "").strip().lower() in _TRUE_ENV_VALUES


def _write_file_sha256(path: Path) -> None:
    """Write sha256 metadata after persisting a local model artifact."""
    _digest_path(path).write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n", encoding="utf-8")


def _verify_file_sha256(path: Path) -> None:
    """Verify a local model artifact before deserializing it with joblib."""
    expected = _MODEL_HASH_REGISTRY.get(path.as_posix())
    if expected is None:
        digest_path = _digest_path(path)
        try:
            expected = digest_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise SecurityError(
                f"task_classifier model file has no package-internal digest registry entry: {path}"
            ) from exc
        if not expected:
            raise SecurityError(f"task_classifier model file has empty digest metadata: {digest_path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise SecurityError(f"task_classifier model file integrity check failed: {path}")
