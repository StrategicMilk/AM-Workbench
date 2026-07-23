"""Model discovery search workflow and cloud-provider candidate helpers.

The mixin in this module ranks local and external model candidates while the
facade class in ``vetinari.model_discovery`` preserves the public import path.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from vetinari.boundary_guards import require_nonempty
from vetinari.config.models import load_models_config
from vetinari.config_paths import resolve_config_path
from vetinari.constants import MODEL_DISCOVERY_TIMEOUT
from vetinari.model_discovery_artifacts import _infer_model_family, _matches_objective
from vetinari.model_discovery_cache import _load_from_cache, _save_to_cache
from vetinari.model_discovery_types import ModelCandidate

logger = logging.getLogger(__name__)

_MODEL_DISCOVERY_SEARCH_WORKERS = 4  # One bounded worker per external discovery source.
_PERSISTED_MODEL_DISCOVERY_STATE_FILES = frozenset({"download_jobs.json"})

# Shared search executor lifecycle:
# - written lazily by _get_model_discovery_search_executor()
# - read by _ModelDiscoverySearch._search_external_sources()
# - drained by _shutdown_model_discovery_search_executor() at interpreter exit/tests
# - protected by _MODEL_DISCOVERY_SEARCH_EXECUTOR_LOCK for create/shutdown races
_MODEL_DISCOVERY_SEARCH_EXECUTOR: ThreadPoolExecutor | None = None
_MODEL_DISCOVERY_SEARCH_EXECUTOR_LOCK = threading.Lock()


def _get_adapters() -> tuple[type[Any], type[Any], type[Any], type[Any]]:
    """Import and return the four source adapter classes."""
    from vetinari.model_discovery_adapters import (
        GitHubAdapter,
        HuggingFaceAdapter,
        PapersWithCodeAdapter,
        RedditAdapter,
    )

    return HuggingFaceAdapter, RedditAdapter, GitHubAdapter, PapersWithCodeAdapter


def _get_model_discovery_search_executor() -> ThreadPoolExecutor:
    """Return the shared bounded executor for external model discovery searches.

    Returns:
        A process-scope ThreadPoolExecutor reused across discovery searches.
    """
    global _MODEL_DISCOVERY_SEARCH_EXECUTOR

    if _MODEL_DISCOVERY_SEARCH_EXECUTOR is None:
        with _MODEL_DISCOVERY_SEARCH_EXECUTOR_LOCK:
            if _MODEL_DISCOVERY_SEARCH_EXECUTOR is None:
                _MODEL_DISCOVERY_SEARCH_EXECUTOR = ThreadPoolExecutor(
                    max_workers=_MODEL_DISCOVERY_SEARCH_WORKERS,
                    thread_name_prefix="model_discovery",
                )
    return _MODEL_DISCOVERY_SEARCH_EXECUTOR


def _shutdown_model_discovery_search_executor() -> None:
    """Drain and clear the shared discovery-search executor.

    This is used by interpreter teardown and tests so the reusable executor has
    a defined lifecycle without returning to per-search construction.
    """
    global _MODEL_DISCOVERY_SEARCH_EXECUTOR

    with _MODEL_DISCOVERY_SEARCH_EXECUTOR_LOCK:
        executor = _MODEL_DISCOVERY_SEARCH_EXECUTOR
        _MODEL_DISCOVERY_SEARCH_EXECUTOR = None

    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=False)


atexit.register(_shutdown_model_discovery_search_executor)


def _calculate_score(candidate: ModelCandidate) -> float:
    """Calculate a composite final score for a model candidate.

    Combines hard data score, benchmark score, sentiment score, and
    recency score into a single ranking value.

    Args:
        candidate: The model candidate to score.

    Returns:
        A float score between 0.0 and 1.0.
    """
    return (
        candidate.hard_data_score * 0.4
        + candidate.benchmark_score * 0.25
        + candidate.sentiment_score * 0.15
        + candidate.recency_score * 0.2
    )


def _generate_rationale(candidate: ModelCandidate, recency_score: float) -> str:
    """Generate a short human-readable rationale for why a model was ranked.

    Args:
        candidate: The model candidate.
        recency_score: How recently the model was updated (0.0-1.0).

    Returns:
        A short rationale string.
    """
    parts: list[str] = []
    if candidate.source_type == "local":
        parts.append("locally available")
    if candidate.hard_data_score >= 0.7:
        parts.append("strong community metrics")
    if candidate.benchmark_score >= 0.5:
        parts.append("good benchmarks")
    if recency_score >= 0.8:
        parts.append("recently updated")
    if candidate.memory_gb <= 8:
        parts.append("fits in VRAM")
    return "; ".join(parts) if parts else "general candidate"


def _local_candidate(model_dict: dict[str, Any]) -> ModelCandidate:
    """Convert a local model dict (from ModelPool) into a ModelCandidate.

    Args:
        model_dict: Model dict with keys like id, name, memory_gb, capabilities.

    Returns:
        A ModelCandidate representing the local model.
    """
    return ModelCandidate(
        id=model_dict.get("id", ""),
        name=model_dict.get("name", model_dict.get("id", "")),
        source_type=require_nonempty("local", field_name="source_type"),
        metrics={"source": "local"},
        memory_gb=model_dict.get("memory_gb", 0),
        context_len=model_dict.get("context_len", 0),
        hard_data_score=0.8,
        benchmark_score=0.0,
        sentiment_score=0.5,
        recency_score=1.0,
    )


def _candidate_matches_filters(
    candidate: ModelCandidate,
    *,
    objective: str | None = None,
    family: str | None = None,
    min_size_gb: float | None = None,
    max_size_gb: float | None = None,
    quantization: str | None = None,
    file_type: str | None = None,
) -> bool:
    identity = f"{candidate.id} {candidate.name}"
    if objective and objective.strip().lower() not in {"", "general", "any"}:
        objective_value = objective.strip().lower()
        recommended = {item.lower() for item in candidate.recommended_for}
        if objective_value not in recommended and not _matches_objective(identity, objective_value):
            return False
    if family and family.strip().lower() not in {"", "any"} and _infer_model_family(identity) != family.strip().lower():
        return False
    if min_size_gb is not None and candidate.memory_gb < min_size_gb:
        return False
    if max_size_gb is not None and candidate.memory_gb > max_size_gb:
        return False
    tags = " ".join(str(tag) for tag in candidate.metrics.get("tags", []))
    haystack = f"{identity} {tags}".lower()
    if (
        quantization
        and quantization.strip().lower() not in {"", "any"}
        and quantization.strip().lower() not in haystack
    ):
        return False
    return not (
        file_type
        and file_type.strip().lower() not in {"", "any"}
        and file_type.strip().lower().lstrip(".") not in haystack
    )


def _extract_keywords(text: str) -> list[str]:
    keywords: list[str] = []
    text_lower = text.lower()
    categories = {
        "code": ["code", "program", "develop", "implement", "software", "app", "web", "script", "function", "api"],
        "reasoning": ["reason", "think", "analyze", "solve", "problem", "logic", "math"],
        "chat": ["chat", "conversation", "talk", "message", "respond"],
        "creative": ["write", "story", "creative", "article", "content"],
        "data": ["data", "database", "sql", "query", "analyze", "etl"],
    }
    for category, words in categories.items():
        if any(w in text_lower for w in words):
            keywords.append(category)
    return keywords


def _query_receipt(query: str) -> dict[str, Any]:
    digest = hashlib.sha256(query.encode("utf-8", errors="replace")).hexdigest()
    return {"query_sha256": digest, "query_length": len(query)}


def _query_log_id(query: str) -> str:
    receipt = _query_receipt(query)
    return f"len={receipt['query_length']} sha256={str(receipt['query_sha256'])[:12]}"


def _configured_cloud_models(provider: str) -> list[dict[str, Any]]:
    """Return configured cloud model rows for provider.

    The runtime catalog is the currency authority. If it is unavailable,
    model discovery fails closed for cloud fallback candidates instead of
    emitting stale hardcoded model IDs.
    """
    try:
        loaded = load_models_config(resolve_config_path("models.yaml"))
    except Exception:
        logger.warning("Cloud model catalog unavailable; skipping %s fallback candidates", provider, exc_info=True)
        return []
    if not isinstance(loaded, dict):
        return []
    rows = loaded.get("cloud_models", [])
    if not isinstance(rows, list):
        return []
    provider_lower = provider.lower()
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("provider", "")).lower() == provider_lower
        and str(row.get("status", "available")).lower() == "available"
    ]


class _ModelDiscoverySearch:
    """Search behavior mixed into the public ModelDiscovery facade."""

    # -- main search ---------------------------------------------------------

    def search(
        self,
        query: str,
        local_models: list[dict[str, Any]] | None = None,
        *,
        objective: str | None = None,
        family: str | None = None,
        min_size_gb: float | None = None,
        max_size_gb: float | None = None,
        quantization: str | None = None,
        file_type: str | None = None,
    ) -> list[ModelCandidate]:
        """Search model candidates.

        Args:
            query: Query value consumed by search().
            local_models: Local models value consumed by search().
            objective: Objective value consumed by search().
            family: Family value consumed by search().
            min_size_gb: Min size gb value consumed by search().
            max_size_gb: Max size gb value consumed by search().
            quantization: Quantization value consumed by search().
            file_type: File path or file-like value consumed by the operation.

        Returns:
            Value produced for the caller.
        """
        all_candidates: list[ModelCandidate] = []
        if local_models:
            all_candidates.extend(_local_candidate(m) for m in local_models)
        logger.info("Searching for model candidates; query=%s", _query_log_id(query))
        external = self._search_external_sources(query)
        all_candidates.extend(external)
        all_candidates.extend(self._search_cloud_providers(query))
        seen: set[str] = set()
        unique: list[ModelCandidate] = []
        for c in all_candidates:
            if c.id not in seen:
                seen.add(c.id)
                unique.append(c)
        for c in unique:
            prev_rationale = c.short_rationale
            c.final_score = _calculate_score(c)
            if not prev_rationale:
                c.short_rationale = _generate_rationale(c, c.recency_score)
        unique = [
            candidate
            for candidate in unique
            if _candidate_matches_filters(
                candidate,
                objective=objective,
                family=family,
                min_size_gb=min_size_gb,
                max_size_gb=max_size_gb,
                quantization=quantization,
                file_type=file_type,
            )
        ]
        unique.sort(key=lambda x: x.final_score, reverse=True)
        try:
            from vetinari.learning.benchmark_seeder import BenchmarkSeeder

            seeder = BenchmarkSeeder()
            for candidate in unique:
                if require_nonempty(candidate.source_type, field_name="source_type") == "local":
                    seeder.seed_model(candidate.id)
        except ImportError:
            logger.debug("BenchmarkSeeder not available — Thompson Sampling priors will start flat")
        except Exception:
            logger.warning(
                "Could not seed Thompson Sampling priors for discovered models — model selection will use flat priors",
                exc_info=True,
            )
        return unique[:15]

    # Backward-compat alias for ModelSearchEngine callers
    def search_for_task(
        self,
        task_description: str,
        local_models: list[dict[str, Any]] | None = None,
    ) -> list[ModelCandidate]:
        """Search for model candidates matching a task description.

        Backward-compatibility alias that delegates to :meth:`search`.

        Args:
            task_description: Natural-language description of the task to find models for.
            local_models: Optional pre-fetched list of locally available model dicts.

        Returns:
            Ranked list of ModelCandidate objects suitable for the task.
        """
        return self.search(task_description, local_models)

    # -- source groupings (kept for backward compat / patchability) ----------

    def _search_external_sources(self, query: str) -> list[ModelCandidate]:
        """Search HF, Reddit, GitHub, and PapersWithCode concurrently with caching."""
        candidates: list[ModelCandidate] = []

        # Define search tasks: (prefix, search_function)
        search_tasks = [
            ("hf", lambda q: self.hf_adapter.search_models(q, limit=8)),
            ("reddit", lambda q: self.reddit_adapter.search_local_llm_posts(q, limit=5)),
            ("github", lambda q: self.github_adapter.search_repos(q, limit=5)),
            ("pwc", lambda q: self.pwc_adapter.search_papers(q, limit=3)),
        ]

        # Execute all searches concurrently
        executor = _get_model_discovery_search_executor()
        futures: dict[Future[list[ModelCandidate]], str] = {
            executor.submit(self._cached_search, prefix, query, fn): prefix for prefix, fn in search_tasks
        }
        for future in as_completed(futures):
            prefix = futures[future]
            try:
                results = future.result(timeout=MODEL_DISCOVERY_TIMEOUT)
                candidates.extend(results)
                logger.info("%s found %d candidates", prefix.upper(), len(results))
            except Exception as exc:
                logger.warning(
                    "Model discovery provider search failed; provider=%s query=%s error_type=%s "
                    "impact=skipping this provider and continuing action=check provider availability or cache state",
                    prefix,
                    _query_log_id(query),
                    type(exc).__name__,
                    exc_info=True,
                )

        return candidates

    # -- cache helpers -------------------------------------------------------

    def _cached_search(
        self,
        prefix: str,
        query: str,
        fetch_fn: Callable[[str], list[ModelCandidate]],
    ) -> list[ModelCandidate]:
        cache_key = hashlib.md5(query.encode("utf-8"), usedforsecurity=False).hexdigest()
        cache_file = self.cache_dir / f"{prefix}_{cache_key}.json"
        cached = _load_from_cache(cache_file)
        if cached is not None:
            return cached
        results = fetch_fn(query)
        if results:
            _save_to_cache(cache_file, results)
        return results

    def refresh_all_caches(self) -> None:
        """Refresh transient search caches without deleting persisted download state."""
        for cache_file in self.cache_dir.glob("*.json"):
            if not cache_file.is_file() or cache_file.name in _PERSISTED_MODEL_DISCOVERY_STATE_FILES:
                continue
            cache_file.unlink()
        logger.info("Model cache cleared")

    def get_cached_candidates(self) -> list[ModelCandidate]:
        """Get cached candidates.

        Returns:
            List of results.
        """
        candidates: list[ModelCandidate] = []
        for cache_file in self.cache_dir.glob("*.json"):
            cached = _load_from_cache(cache_file)
            if cached:
                candidates.extend(cached)
        return candidates

    # -- cloud providers -----------------------------------------------------

    def _search_cloud_providers(self, query: str) -> list[ModelCandidate]:
        candidates: list[ModelCandidate] = []
        candidates.extend(self._search_claude(query))
        candidates.extend(self._search_gemini(query))
        return candidates

    @staticmethod
    def _search_claude(query: str) -> list[ModelCandidate]:
        has_api_access = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY"))
        if not has_api_access:
            try:
                from vetinari.credentials import get_credential_manager

                if get_credential_manager().has_credential("claude"):
                    has_api_access = True  # token exists in credential vault
            except (ImportError, AttributeError):
                logger.debug("Credential manager unavailable for Claude discovery check — env-only fallback")
        if not has_api_access:
            return []
        keywords = _extract_keywords(query)
        models = _configured_cloud_models("claude")
        candidates = []
        for model in models:
            model_id = str(model.get("model_id", "")).strip()
            model_name = str(model.get("display_name") or model_id).strip()
            context_len = int(model.get("context_window") or 0)
            if not model_id:
                continue
            relevance = (
                0.85
                if any(k in ["reasoning", "think", "analyze"] for k in keywords)
                else 0.75
                if any(k in ["code", "program", "develop"] for k in keywords)
                else 0.5
            )
            candidates.append(
                ModelCandidate(
                    id=f"claude:{model_id}",
                    name=model_name,
                    source_type=require_nonempty("claude", field_name="source_type"),
                    metrics={**_query_receipt(query), "relevance": relevance},
                    memory_gb=0,
                    context_len=context_len,
                    version=model_id,
                    last_updated=datetime.now(timezone.utc).isoformat(),
                    hard_data_score=0.9,
                    benchmark_score=0.92,
                    sentiment_score=0.88,
                    provenance=[
                        {
                            "source_type": "claude",
                            "url": f"https://console.anthropic.com/{model_id}",
                            "last_checked": datetime.now(timezone.utc).isoformat(),
                            "confidence": 0.9,
                        },
                    ],
                ),
            )
        return candidates

    @staticmethod
    def _search_gemini(query: str) -> list[ModelCandidate]:
        has_api_access = bool(os.environ.get("GEMINI_API_KEY"))
        if not has_api_access:
            try:
                from vetinari.credentials import get_credential_manager

                if get_credential_manager().has_credential("gemini"):
                    has_api_access = True  # token exists in credential vault
            except (ImportError, AttributeError):
                logger.debug("Credential manager unavailable for Gemini discovery check — env-only fallback")
        if not has_api_access:
            return []
        keywords = _extract_keywords(query)
        models = _configured_cloud_models("gemini")
        candidates = []
        for model in models:
            model_id = str(model.get("model_id", "")).strip()
            model_name = str(model.get("display_name") or model_id).strip()
            context_len = int(model.get("context_window") or 0)
            if not model_id:
                continue
            relevance = (
                0.8
                if any(k in ["reasoning", "think", "analyze"] for k in keywords)
                else 0.82
                if any(k in ["creative", "write", "story"] for k in keywords)
                else 0.75
                if any(k in ["code", "program", "develop"] for k in keywords)
                else 0.5
            )
            candidates.append(
                ModelCandidate(
                    id=f"gemini:{model_id}",
                    name=model_name,
                    source_type=require_nonempty("gemini", field_name="source_type"),
                    metrics={**_query_receipt(query), "relevance": relevance},
                    memory_gb=0,
                    context_len=context_len,
                    version=model_id,
                    last_updated=datetime.now(timezone.utc).isoformat(),
                    hard_data_score=0.85,
                    benchmark_score=0.88,
                    sentiment_score=0.82,
                    provenance=[
                        {
                            "source_type": "gemini",
                            "url": f"https://aistudio.google.com/app/{model_id}",
                            "last_checked": datetime.now(timezone.utc).isoformat(),
                            "confidence": 0.85,
                        },
                    ],
                ),
            )
        return candidates
