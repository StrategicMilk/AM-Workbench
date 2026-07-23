"""Lightweight Isolation Forest for multivariate anomaly detection.

Two detectors watch for unusual metric COMBINATIONS that single-metric
detectors miss:
  - Primary detector (8 dims): latency, error_rate, token_usage, quality_score,
    context_window_util, inspector_rejection_rate, output_length_trend, retry_rate
  - Hardware detector (4 dims): gpu_util_pct, vram_util_pct, model_load_unload_freq,
    cache_hit_rate

Each detector builds isolation trees from observed data and scores new
observations.  Anomaly score > threshold triggers alerting.

The algorithm works by randomly partitioning the feature space: anomalies
are isolated quickly (short average path length through the trees) while
normal points require many splits to isolate.  Score 2^(-mean_path/c(n))
is normalized to [0, 1] where values close to 1 indicate anomalies.
"""

from __future__ import annotations

import logging
import math
import random
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import NamedTuple, cast

from vetinari.utils.bounded_collections import BoundedList

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Euler-Mascheroni constant used in the harmonic number approximation
_EULER_MASCHERONI: float = 0.5772156649015328

# Default anomaly score threshold — observations above this are flagged
_DEFAULT_THRESHOLD: float = 0.6


# ---------------------------------------------------------------------------
# Tree internals
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IsolationTreeNode:
    """A node in an isolation tree.

    Leaf nodes (feature_idx == -1) record the sample count at that leaf
    so the path-length correction factor can be applied.  Internal nodes
    record which feature to split on and the split value.
    """

    feature_idx: int = -1  # Split feature index (-1 = leaf)
    split_value: float = 0.0  # Value that separates left/right subtrees
    left: IsolationTreeNode | None = None  # Left subtree (value < split_value)
    right: IsolationTreeNode | None = None  # Right subtree (value >= split_value)
    size: int = 0  # Number of samples at this node (for leaves only)

    def __repr__(self) -> str:
        if self.feature_idx == -1:
            return f"IsolationTreeNode(leaf, size={self.size})"
        return f"IsolationTreeNode(feature={self.feature_idx}, split={self.split_value:.4g})"


def _c(n: int) -> float:
    """Average path length for an unsuccessful BST search over n samples.

    This harmonic-number correction is subtracted from the observed path
    length so that scores are normalized across different subsample sizes.

    Args:
        n: Number of data points.

    Returns:
        Expected average path length c(n).
    """
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    # H(n-1) approximated as ln(n-1) + Euler-Mascheroni constant
    return 2.0 * (math.log(n - 1) + _EULER_MASCHERONI) - 2.0 * (n - 1) / n


def _build_tree(
    data: list[list[float]],
    depth: int,
    height_limit: int,
    rng: random.Random,
) -> IsolationTreeNode:
    """Recursively build an isolation tree node.

    Splitting stops early (returns a leaf) either when the height limit is
    reached or when fewer than 2 distinct samples remain — isolating a single
    point requires no further splits.

    Args:
        data: Rows of feature values for the current subtree.
        depth: Current depth in the tree (0 = root).
        height_limit: Maximum depth before forcing a leaf.
        rng: Seeded random instance for reproducibility.

    Returns:
        Root node of the subtree.
    """
    n = len(data)
    if n <= 1 or depth >= height_limit:
        return IsolationTreeNode(feature_idx=-1, size=n)

    n_features = len(data[0])
    # Pick a random feature and a random split point within its observed range
    feat = rng.randrange(n_features)
    col = [row[feat] for row in data]
    lo, hi = min(col), max(col)

    if lo == hi:
        # All values identical on this feature — can't split, force leaf
        return IsolationTreeNode(feature_idx=-1, size=n)

    split = rng.uniform(lo, hi)
    left_data = [row for row in data if row[feat] < split]
    right_data = [row for row in data if row[feat] >= split]

    # Guard against degenerate splits that don't partition the data
    if not left_data or not right_data:
        return IsolationTreeNode(feature_idx=-1, size=n)

    return IsolationTreeNode(
        feature_idx=feat,
        split_value=split,
        left=_build_tree(left_data, depth + 1, height_limit, rng),
        right=_build_tree(right_data, depth + 1, height_limit, rng),
        size=n,
    )


def _path_length(node: IsolationTreeNode, point: list[float], depth: int) -> float:
    """Compute the path length for a point through one isolation tree.

    At a leaf the correction factor c(size) is added to account for the
    expected additional splits that would have occurred with more data.

    Args:
        node: Current tree node.
        point: Feature vector to score.
        depth: Accumulated depth so far.

    Returns:
        Estimated path length including the leaf correction term.
    """
    if node.feature_idx == -1:
        # Leaf node: add the correction for samples remaining here
        return depth + _c(node.size)

    if point[node.feature_idx] < node.split_value:
        child = node.left
    else:
        child = node.right

    if child is None:
        return depth + _c(node.size)

    return _path_length(child, point, depth + 1)


# ---------------------------------------------------------------------------
# Isolation Forest
# ---------------------------------------------------------------------------


class IsolationForest:
    """Ensemble of isolation trees for anomaly scoring.

    Anomaly score is normalized to [0, 1] where scores close to 1
    indicate anomalies.  The harmonic number correction factor c(n) is
    applied so scores are comparable across different subsample sizes.

    Thread-safe: fit and score acquire the instance lock.

    Args:
        n_trees: Number of isolation trees to build.
        subsample_size: Number of data points sampled per tree.
        contamination: Expected fraction of anomalies; used to set the
            internal decision threshold.
        seed: Optional integer seed for reproducibility.
    """

    def __init__(
        self,
        n_trees: int = 100,
        subsample_size: int = 256,
        contamination: float = 0.05,
        seed: int | None = None,
    ) -> None:
        self._n_trees = n_trees
        self._subsample_size = subsample_size
        self._contamination = contamination
        self._rng = random.Random(seed)
        self._trees: list[IsolationTreeNode] = []
        self._cn: float = 0.0  # c(subsample_size), cached after fit
        self._lock = threading.Lock()
        # Height limit per the original IF paper: ceil(log2(subsample_size))
        self._height_limit = math.ceil(math.log2(max(subsample_size, 2)))

    def fit(self, data: list[list[float]]) -> None:
        """Build isolation trees from the training data.

        Each tree uses a random subsample of `subsample_size` rows.  If
        fewer rows than the subsample size are available all rows are used.

        Args:
            data: List of feature vectors (all must have the same length).

        Raises:
            ValueError: If data is empty or feature vectors have length 0.
        """
        if not data:
            raise ValueError("Cannot fit IsolationForest on empty data")
        if not data[0]:
            raise ValueError("Feature vectors must have at least one dimension")

        n = len(data)
        sample_size = min(self._subsample_size, n)
        trees = BoundedList[IsolationTreeNode](self._n_trees)

        for _ in range(self._n_trees):
            if n <= sample_size:
                subsample = data
            else:
                subsample = self._rng.sample(data, sample_size)
            tree = _build_tree(subsample, 0, self._height_limit, self._rng)
            trees.append(tree)

        with self._lock:
            self._trees = list(trees)
            self._cn = _c(sample_size)

    def score(self, point: list[float]) -> float:
        """Return anomaly score in [0, 1].  Higher values indicate anomalies.

        A score near 1.0 means the point is isolated quickly (anomaly).
        A score near 0.5 means the point behaves like the training data.
        A score near 0.0 means the point is deeply embedded in a cluster.

        Args:
            point: Feature vector to score.

        Returns:
            Anomaly score in [0, 1].

        Raises:
            RuntimeError: If the forest has not been fitted yet.
        """
        with self._lock:
            trees = self._trees
            cn = self._cn

        if not trees:
            raise RuntimeError("IsolationForest has not been fitted — call fit() first")

        mean_depth = sum(_path_length(t, point, 0) for t in trees) / len(trees)

        if cn <= 0.0:
            # Degenerate: only one sample — every point is an anomaly
            return 1.0

        return float(2.0 ** (-mean_depth / cn))

    def is_anomaly(self, point: list[float]) -> bool:
        """Return True if the point's anomaly score exceeds the threshold.

        The threshold is derived from the contamination fraction using the
        empirical approximation: threshold = 2^(-(1 - contamination)).

        Args:
            point: Feature vector to evaluate.

        Returns:
            True if the point is classified as an anomaly.
        """
        threshold = 2.0 ** (-(1.0 - self._contamination))
        score_value: float = cast(float, self.score(point))
        return bool(score_value > threshold)


# ---------------------------------------------------------------------------
# Warmup state helper
# ---------------------------------------------------------------------------


@dataclass
class _DetectorState:
    """Internal warmup buffer and fitted forest for one detector instance."""

    buffer: deque[list[float]] = field(default_factory=lambda: deque(maxlen=1000))
    forest: IsolationForest | None = None
    # How often to refit the forest (every N observations after warmup)
    refit_interval: int = 100
    obs_since_refit: int = 0

    def __repr__(self) -> str:
        return "_DetectorState(...)"


# ---------------------------------------------------------------------------
# Primary detector (8 dims)
# ---------------------------------------------------------------------------


class _PrimaryObservation(NamedTuple):
    """Validated 8-dimensional observation for the primary detector."""

    latency: float
    error_rate: float
    token_usage: float
    quality_score: float
    context_window_util: float
    inspector_rejection_rate: float
    output_length_trend: float
    retry_rate: float


class PrimaryAnomalyDetector:
    """8-dimensional detector for per-request metrics.

    Dimensions (in order):
        latency, error_rate, token_usage, quality_score,
        context_window_util, inspector_rejection_rate, output_length_trend,
        retry_rate.

    During warmup (fewer than ``warmup_samples`` observations) `observe`
    returns None.  Once warmed up the Isolation Forest is fitted and
    subsequent calls return an anomaly score in [0, 1].

    The forest is periodically refitted so it adapts to gradual drift in
    the metric distribution.

    Thread-safe: all mutations are protected by an internal lock.

    Args:
        warmup_samples: Minimum observations before scoring begins.
        n_trees: Number of isolation trees per forest fit.
        contamination: Expected anomaly fraction (affects threshold).
        seed: Optional random seed for reproducibility.
    """

    FEATURE_NAMES: list[str] = [
        "latency",
        "error_rate",
        "token_usage",
        "quality_score",
        "context_window_util",
        "inspector_rejection_rate",
        "output_length_trend",
        "retry_rate",
    ]

    def __init__(
        self,
        warmup_samples: int = 300,
        n_trees: int = 100,
        contamination: float = 0.05,
        seed: int | None = None,
    ) -> None:
        self._warmup_samples = warmup_samples
        self._n_trees = n_trees
        self._contamination = contamination
        self._seed = seed
        self._state = _DetectorState(refit_interval=200)
        self._lock = threading.Lock()

    def observe(self, **kwargs: float) -> float | None:
        """Observe per-request metrics and return an anomaly score.

        Keyword argument names must match FEATURE_NAMES.  Missing features
        default to 0.0.  Returns None during the warmup period.

        Args:
            **kwargs: Metric values keyed by feature name.

        Returns:
            Anomaly score in [0, 1] once warmed up, or None during warmup.
        """
        obs = _PrimaryObservation(
            latency=kwargs.get("latency", 0.0),
            error_rate=kwargs.get("error_rate", 0.0),
            token_usage=kwargs.get("token_usage", 0.0),
            quality_score=kwargs.get("quality_score", 0.0),
            context_window_util=kwargs.get("context_window_util", 0.0),
            inspector_rejection_rate=kwargs.get("inspector_rejection_rate", 0.0),
            output_length_trend=kwargs.get("output_length_trend", 0.0),
            retry_rate=kwargs.get("retry_rate", 0.0),
        )
        point = list(obs)
        return self._ingest(point)

    def _ingest(self, point: list[float]) -> float | None:
        """Add point to buffer, maybe refit, and return score.

        Args:
            point: Feature vector for one observation.

        Returns:
            Anomaly score or None during warmup.
        """
        with self._lock:
            self._state.buffer.append(point)
            n = len(self._state.buffer)

            if n < self._warmup_samples:
                return None

            # Initial fit after warmup completes
            if self._state.forest is None:
                self._state.forest = IsolationForest(
                    n_trees=self._n_trees,
                    subsample_size=min(256, n),
                    contamination=self._contamination,
                    seed=self._seed,
                )
                self._state.forest.fit(list(self._state.buffer))
                self._state.obs_since_refit = 0
                logger.debug("PrimaryAnomalyDetector: initial forest fit on %d samples", n)

            self._state.obs_since_refit += 1
            if self._state.obs_since_refit >= self._state.refit_interval:
                # Refit on the most recent 2 x subsample_size observations
                recent = list(self._state.buffer)[-512:]
                self._state.forest.fit(recent)
                self._state.obs_since_refit = 0
                logger.debug("PrimaryAnomalyDetector: forest refitted on %d samples", len(recent))

            return self._state.forest.score(point)


# ---------------------------------------------------------------------------
# Hardware detector (4 dims)
# ---------------------------------------------------------------------------


class _HardwareObservation(NamedTuple):
    """Validated 4-dimensional observation for the hardware detector."""

    gpu_util_pct: float
    vram_util_pct: float
    model_load_unload_freq: float
    cache_hit_rate: float


class HardwareAnomalyDetector:
    """4-dimensional detector for hardware metrics.

    Dimensions (in order):
        gpu_util_pct, vram_util_pct, model_load_unload_freq, cache_hit_rate.

    Mirrors the design of PrimaryAnomalyDetector but operates on hardware
    telemetry.  A shorter warmup period is used because hardware metrics
    settle faster than per-request metrics.

    Thread-safe: all mutations are protected by an internal lock.

    Args:
        warmup_samples: Minimum observations before scoring begins.
        n_trees: Number of isolation trees per forest fit.
        contamination: Expected anomaly fraction (affects threshold).
        seed: Optional random seed for reproducibility.
    """

    FEATURE_NAMES: list[str] = [
        "gpu_util_pct",
        "vram_util_pct",
        "model_load_unload_freq",
        "cache_hit_rate",
    ]

    def __init__(
        self,
        warmup_samples: int = 200,
        n_trees: int = 100,
        contamination: float = 0.05,
        seed: int | None = None,
    ) -> None:
        self._warmup_samples = warmup_samples
        self._n_trees = n_trees
        self._contamination = contamination
        self._seed = seed
        self._state = _DetectorState(refit_interval=100)
        self._lock = threading.Lock()

    def observe(self, **kwargs: float) -> float | None:
        """Observe hardware metrics and return an anomaly score.

        Keyword argument names must match FEATURE_NAMES.  Missing features
        default to 0.0.  Returns None during the warmup period.

        Args:
            **kwargs: Metric values keyed by feature name.

        Returns:
            Anomaly score in [0, 1] once warmed up, or None during warmup.
        """
        obs = _HardwareObservation(
            gpu_util_pct=kwargs.get("gpu_util_pct", 0.0),
            vram_util_pct=kwargs.get("vram_util_pct", 0.0),
            model_load_unload_freq=kwargs.get("model_load_unload_freq", 0.0),
            cache_hit_rate=kwargs.get("cache_hit_rate", 0.0),
        )
        point = list(obs)
        return self._ingest(point)

    def _ingest(self, point: list[float]) -> float | None:
        """Add point to buffer, maybe refit, and return score.

        Args:
            point: Feature vector for one observation.

        Returns:
            Anomaly score or None during warmup.
        """
        with self._lock:
            self._state.buffer.append(point)
            n = len(self._state.buffer)

            if n < self._warmup_samples:
                return None

            if self._state.forest is None:
                self._state.forest = IsolationForest(
                    n_trees=self._n_trees,
                    subsample_size=min(256, n),
                    contamination=self._contamination,
                    seed=self._seed,
                )
                self._state.forest.fit(list(self._state.buffer))
                self._state.obs_since_refit = 0
                logger.debug("HardwareAnomalyDetector: initial forest fit on %d samples", n)

            self._state.obs_since_refit += 1
            if self._state.obs_since_refit >= self._state.refit_interval:
                recent = list(self._state.buffer)[-256:]
                self._state.forest.fit(recent)
                self._state.obs_since_refit = 0
                logger.debug(
                    "HardwareAnomalyDetector: forest refitted on %d samples",
                    len(recent),
                )

            return self._state.forest.score(point)
