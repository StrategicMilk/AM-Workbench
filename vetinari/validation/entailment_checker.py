"""Entailment Checker — Tier 2 of the verification cascade.

Checks whether a response semantically entails the task requirements using
lightweight NLP heuristics (keyword overlap, requirement coverage) — no LLM
needed for most cases.

Pipeline role: Called by CascadeOrchestrator when Tier 1 (StaticVerifier)
passes but score confidence is still uncertain.  Avoids an LLM call by using
token overlap and structural coverage as a proxy for entailment.

When explicitly enabled, an optional AM Engine cosine-similarity check is
added for higher accuracy (see _semantic_similarity).
"""

from __future__ import annotations

import json
import logging
import math
import multiprocessing
import os
import queue
import re
import sys
import threading
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Stop-words filtered out during keyword extraction — common English words that carry no domain signal
_STOP_WORDS = frozenset({
    "that",
    "this",
    "with",
    "from",
    "have",
    "will",
    "should",
    "would",
    "could",
    "their",
    "there",
    "which",
    "when",
    "then",
    "than",
    "also",
    "into",
    "more",
    "some",
    "each",
    "been",
    "were",
    "they",
    "them",
    "make",
    "made",
    "does",
    "where",
    "what",
    "your",
    "just",
    "only",
    "very",
    "about",
    "after",
})

# Minimum fraction of task requirement keywords that must appear in the response
_MIN_KEYWORD_COVERAGE = 0.4  # 40 % coverage required for PASS

# Cosine similarity threshold when sentence-transformers is available
_SEMANTIC_SIMILARITY_THRESHOLD = 0.55


@dataclass
class EntailmentResult:
    """Result of the entailment check.

    Attributes:
        entailed: True when the response adequately covers the task requirements.
        coverage: Fraction of task keywords found in the response (0.0-1.0).
        similarity: Cosine similarity score if sentence-transformers was used,
            otherwise None.
        missing_keywords: Keywords from the task that were absent in the response.
    """

    entailed: bool
    coverage: float
    similarity: float | None = None
    missing_keywords: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key fields for debugging."""
        return (
            f"EntailmentResult(entailed={self.entailed!r}, "
            f"coverage={self.coverage:.3f}, similarity={self.similarity!r})"
        )


class EntailmentChecker:
    """Tier 2 verifier — semantic coverage check without LLM calls.

    Uses keyword overlap as the primary signal and optional sentence-transformer
    cosine similarity as a secondary signal when the library is installed.

    Example::

        checker = EntailmentChecker()
        result = checker.check(
            task_description="Implement a binary search function",
            response_text="def binary_search(arr, target): ...",
        )
        assert result.entailed
    """

    def check(self, task_description: str, response_text: str) -> EntailmentResult:
        """Check whether *response_text* entails the requirements in *task_description*.

        Args:
            task_description: The original task or requirement text.
            response_text: The response to evaluate.

        Returns:
            :class:`EntailmentResult` with entailment verdict and evidence scores.
        """
        if not task_description or not response_text:
            return EntailmentResult(entailed=False, coverage=0.0, missing_keywords=[])

        # ── Step 1: keyword coverage ─────────────────────────────────────────
        task_keywords = self._extract_keywords(task_description)
        resp_lower = response_text.lower()

        if not task_keywords:
            # No keywords extracted means we cannot verify the response satisfies the task.
            # Returning entailed=True here would certify arbitrary content as valid —
            # the "default-pass verifier" anti-pattern.  Return score=0.0 instead.
            return EntailmentResult(entailed=False, coverage=0.0, missing_keywords=[])

        # Use whole-word matching to prevent substring false positives (e.g. "search"
        # must NOT match inside "research").  Two complementary strategies:
        #
        # 1. Word-boundary regex (\b) — catches keywords in prose where word boundaries
        #    are whitespace / punctuation.
        # 2. Identifier-token set — splits the response on non-alpha chars so that
        #    code identifiers like "binary_search" contribute tokens "binary" and
        #    "search".  This prevents \b from missing keywords embedded in snake_case
        #    or camelCase identifiers while still rejecting "research" → "search".
        resp_tokens: set[str] = set(re.split(r"[^a-z]+", resp_lower))
        resp_tokens.discard("")
        response_has_code_structure = bool(re.search(r"\b(?:def|class|async\s+def)\s+[a-zA-Z_]\w*", response_text))

        def _matches(kw: str) -> bool:
            if kw == "implement" and response_has_code_structure:
                return True
            return bool(re.search(rf"\b{re.escape(kw)}\b", resp_lower)) or kw in resp_tokens

        found = [kw for kw in task_keywords if _matches(kw)]
        missing = [kw for kw in task_keywords if not _matches(kw)]
        coverage = len(found) / len(task_keywords)
        keyword_echo = self._is_keyword_echo(task_description, response_text, task_keywords)

        logger.debug(
            "EntailmentChecker: coverage=%.3f (%d/%d keywords found)",
            coverage,
            len(found),
            len(task_keywords),
        )

        # ── Step 2: optional semantic similarity ─────────────────────────────
        similarity: float | None = None
        try:
            similarity = self._semantic_similarity(task_description, response_text)
            if similarity is not None:
                logger.debug("EntailmentChecker: semantic similarity=%.3f", similarity)
        except Exception as exc:
            logger.warning(
                "EntailmentChecker: semantic similarity check failed (%s) — falling back to keyword coverage only", exc
            )

        # ── Step 3: verdict ──────────────────────────────────────────────────
        if similarity is not None:
            # When we have semantic similarity, require BOTH coverage and similarity
            entailed = (
                coverage >= _MIN_KEYWORD_COVERAGE and similarity >= _SEMANTIC_SIMILARITY_THRESHOLD and not keyword_echo
            )
        else:
            entailed = coverage >= _MIN_KEYWORD_COVERAGE and not keyword_echo

        return EntailmentResult(
            entailed=entailed,
            coverage=round(coverage, 3),
            similarity=round(similarity, 3) if similarity is not None else None,
            missing_keywords=missing,
        )

    # ── helpers ──────────────────────────────────────────────────────────────

    def _extract_keywords(self, text: str) -> list[str]:
        """Extract meaningful content words from *text* for overlap checking.

        Filters out stop-words and short tokens to focus on domain vocabulary.

        Args:
            text: Input text to tokenise.

        Returns:
            Deduplicated list of lowercase content words (length >= 4).
        """
        words = re.findall(r"\b[a-z]{4,}\b", text.lower())
        return list(dict.fromkeys(w for w in words if w not in _STOP_WORDS))

    def _is_keyword_echo(self, task_description: str, response_text: str, task_keywords: list[str]) -> bool:
        """Return True when the response only echoes requirement words.

        Keyword coverage is useful evidence only when the response contributes
        implementation, decision, or answer content. A copied requirement string
        can otherwise pass the coverage threshold without doing the task.
        """
        normalized_task = " ".join(re.findall(r"\b[a-z0-9_]+\b", task_description.lower()))
        normalized_response = " ".join(re.findall(r"\b[a-z0-9_]+\b", response_text.lower()))
        if normalized_task and normalized_task == normalized_response:
            return True
        response_words = [
            word for word in re.findall(r"\b[a-z]{4,}\b", response_text.lower()) if word not in _STOP_WORDS
        ]
        if not response_words:
            return False
        task_keyword_set = set(task_keywords)
        response_word_set = set(response_words)
        return response_word_set.issubset(task_keyword_set) and len(response_words) <= len(task_keywords) + 2

    @staticmethod
    def _semantic_similarity(text_a: str, text_b: str) -> float | None:
        """Compute cosine similarity between two texts using sentence-transformers.

        Returns None when the library is not installed or an error occurs —
        callers fall back to keyword coverage only.

        Args:
            text_a: First text.
            text_b: Second text.

        Returns:
            Cosine similarity in [0.0, 1.0] or None if unavailable.
        """
        if not _sentence_transformers_entailment_enabled():
            return None
        return _semantic_similarity_subprocess(text_a, text_b)


# ── sentence-transformers singleton ──────────────────────────────────────────


def _sentence_transformers_entailment_enabled() -> bool:
    """Return True only when optional native entailment is explicitly enabled."""
    if os.environ.get(_ENABLE_ST_ENV, "").strip().lower() not in _TRUE_ENV_VALUES:
        return False
    return not (
        os.environ.get("PYTEST_XDIST_WORKER")
        and os.environ.get(_ENABLE_ST_XDIST_ENV, "").strip().lower() not in _TRUE_ENV_VALUES
    )


def _semantic_similarity_subprocess(text_a: str, text_b: str) -> float | None:
    """Compute optional native similarity in a spawned child process.

    The sentence-transformers import graph loads native pyarrow/sklearn/datasets
    extensions on current installs. Running that optional path out-of-process
    keeps a native access violation from terminating the caller.
    """
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    proc = ctx.Process(
        target=_semantic_similarity_worker_process,
        args=(text_a[:512], text_b[:512], result_queue),
    )
    try:
        proc.start()
        proc.join(_semantic_similarity_timeout_seconds())
        if proc.is_alive():
            proc.terminate()
            proc.join(2.0)
            logger.warning("EntailmentChecker: similarity worker timed out - keyword-only mode")
            result_queue.close()
            result_queue.join_thread()
            return None
    except (OSError, RuntimeError) as exc:
        logger.warning("EntailmentChecker: similarity worker failed (%s) - keyword-only mode", exc)
        result_queue.close()
        result_queue.join_thread()
        return None
    if proc.exitcode != 0:
        logger.warning(
            "EntailmentChecker: similarity worker exited %s - keyword-only mode",
            proc.exitcode,
        )
        result_queue.close()
        result_queue.join_thread()
        return None
    try:
        score = result_queue.get_nowait()
    except queue.Empty:
        logger.warning("EntailmentChecker: similarity worker returned no output - keyword-only mode")
        return None
    finally:
        result_queue.close()
        result_queue.join_thread()
    if score is None:
        logger.debug("EntailmentChecker: similarity worker returned no score - keyword-only mode")
        return None
    try:
        return max(0.0, min(1.0, float(score)))
    except (TypeError, ValueError) as exc:
        logger.warning("EntailmentChecker: invalid similarity score (%s) - keyword-only mode", exc)
        return None


def _semantic_similarity_worker_process(text_a: str, text_b: str, result_queue: Any) -> None:
    result_queue.put(_compute_semantic_similarity_in_process(text_a, text_b))


def _semantic_similarity_timeout_seconds() -> float:
    try:
        return max(0.1, float(os.environ.get(_ST_TIMEOUT_ENV, "30")))
    except ValueError as exc:
        logger.warning("EntailmentChecker: invalid similarity timeout (%s); using default", exc)
        return 30.0


def _compute_semantic_similarity_in_process(text_a: str, text_b: str) -> float | None:
    """Compute engine-backed similarity inside the compatibility worker."""
    if not _sentence_transformers_entailment_enabled():
        return None
    try:
        from vetinari.engine.client import EmbeddingsRequest, get_engine_client

        response = get_engine_client().embeddings(EmbeddingsRequest((text_a[:512], text_b[:512])))
        vector_a, vector_b = response.vectors
        denominator = math.sqrt(sum(value * value for value in vector_a)) * math.sqrt(
            sum(value * value for value in vector_b),
        )
        if denominator == 0.0:
            return None
        score = sum(left * right for left, right in zip(vector_a, vector_b, strict=True)) / denominator
        return max(0.0, min(1.0, score))
    except Exception as exc:
        logger.warning(
            "EntailmentChecker: AM Engine similarity unavailable - falling back to keyword coverage",
            extra={"fallback_type": "keyword_coverage", "exc_class": type(exc).__name__},
        )
        return None


def _run_semantic_similarity_worker() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    score = _compute_semantic_similarity_in_process(str(payload.get("text_a", "")), str(payload.get("text_b", "")))
    sys.stdout.write(json.dumps({"similarity": score}))
    return 0


_st_model: Any | None = None
_st_model_loaded = False  # True only when a real local transformer bundle was successfully cached
_st_lock = threading.Lock()
_ENABLE_ST_ENV = "VETINARI_ENABLE_SENTENCE_TRANSFORMERS_ENTAILMENT"
_ENABLE_ST_XDIST_ENV = "VETINARI_ENABLE_SENTENCE_TRANSFORMERS_ENTAILMENT_XDIST"
_MODEL_PATH_ENV = "VETINARI_ENTAILMENT_MODEL_PATH"
_ST_TIMEOUT_ENV = "VETINARI_SENTENCE_TRANSFORMERS_ENTAILMENT_TIMEOUT_SECONDS"
_HF_MODEL_CACHE_DIR = "models--sentence-transformers--all-MiniLM-L6-v2"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _resolve_entailment_model_path() -> str | None:
    configured = os.environ.get(_MODEL_PATH_ENV)
    if configured:
        path = Path(configured).expanduser()
        return str(path) if path.exists() else None
    for hub in _huggingface_hub_roots():
        model_root = hub / _HF_MODEL_CACHE_DIR
        refs_main = model_root / "refs" / "main"
        if refs_main.exists():
            revision = refs_main.read_text(encoding="utf-8").strip()
            snapshot = model_root / "snapshots" / revision
            if snapshot.exists():
                return str(snapshot)
        snapshots = model_root / "snapshots"
        if snapshots.exists():
            candidates = [path for path in snapshots.iterdir() if path.is_dir()]
            if candidates:
                return str(max(candidates, key=lambda path: path.stat().st_mtime))
    return None


def _huggingface_hub_roots() -> list[Path]:
    roots: list[Path] = []
    for env_name in ("HUGGINGFACE_HUB_CACHE",):
        value = os.environ.get(env_name)
        if value:
            roots.append(Path(value).expanduser())
    hf_home = os.environ.get("HF_HOME")
    if hf_home:
        roots.append(Path(hf_home).expanduser() / "hub")
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        roots.append(Path(user_profile) / ".cache" / "huggingface" / "hub")
    home = Path.home()
    roots.append(home / ".cache" / "huggingface" / "hub")
    return list(dict.fromkeys(roots))


def _get_st_model() -> Any | None:
    """Lazily load and cache a lightweight local transformer embedding model.

    The native dependency stack is opt-in via
    VETINARI_ENABLE_SENTENCE_TRANSFORMERS_ENTAILMENT because importing
    sentence-transformers can load pyarrow/sklearn/datasets C extensions in
    worker processes that only need the keyword-coverage fallback.

    Uses double-checked locking so concurrent first calls are safe.  Returns
    None (and logs a debug message) when sentence-transformers is not installed
    or failed to load.  After the singleton is initialized, no further imports
    of sentence_transformers are performed — this avoids torch re-import errors
    in test environments where torch was partially loaded by an earlier import.

    Returns:
        A ``SentenceTransformer`` instance or None if unavailable.
    """
    global _st_model, _st_model_loaded
    if not _sentence_transformers_entailment_enabled():
        return None
    if _st_model is None:
        with _st_lock:
            if _st_model is None:
                try:
                    model_path = _resolve_entailment_model_path()
                    if model_path is None:
                        raise FileNotFoundError("local all-MiniLM-L6-v2 snapshot not found")
                    logging.getLogger("torch.distributed.elastic.multiprocessing.redirects").setLevel(logging.ERROR)
                    transformers: Any = import_module("transformers")
                    torch_module: Any = import_module("torch")
                    tokenizer = transformers.AutoTokenizer.from_pretrained(model_path, local_files_only=True)
                    model = transformers.AutoModel.from_pretrained(model_path, local_files_only=True)
                    model.eval()
                    _st_model = (tokenizer, model, torch_module)
                    _st_model_loaded = True
                except Exception as exc:
                    logger.warning(
                        "EntailmentChecker: native entailment model load failed (%s) - keyword-only mode",
                        exc,
                    )
                    # Cache a sentinel to avoid retrying on every call
                    _st_model = object()
                    _st_model_loaded = False
    # Return the real model only when load succeeded; sentinel maps to None
    return _st_model if _st_model_loaded else None


if __name__ == "__main__" and sys.argv[1:] == ["--semantic-similarity-worker"]:
    raise SystemExit(_run_semantic_similarity_worker())
