"""Web Search Tool for Vetinari.

Provides comprehensive web search capabilities with provenance tracking,
source credibility scoring, and citation support.

Supports multiple backends:
- SearXNG (default, self-hosted — run via docker/docker-compose.yml)
- DuckDuckGo (fallback when SearXNG is unavailable)
- SerpAPI (Google)
- Wikipedia (direct API)
- arXiv (academic papers)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from vetinari.constants import (
    DEFAULT_SEARCH_BACKEND,
    DEFAULT_SEARXNG_URL,
    WEB_SEARCH_PROBE_TIMEOUT,
)
from vetinari.tools.web_search_types import (
    SearchResult,
)
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


class SearchBackend(Enum):
    """Search backend."""

    DUCKDUCKGO = "duckduckgo"
    SERPAPI = "serpapi"
    TAVILY = "tavily"
    WIKIPEDIA = "wikipedia"
    ARXIV = "arxiv"
    BRAVE = "brave"
    SEARXNG = "searxng"
    LOCAL = "local"  # Offline/local knowledge base


@dataclass
class SearchResponse:
    """Complete search response with metadata."""

    results: list[SearchResult]
    query: str
    backend: str
    total_results: int
    execution_time_ms: int
    citations: list[str] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"SearchResponse(backend={self.backend!r},"
            f" total_results={self.total_results!r},"
            f" execution_time_ms={self.execution_time_ms!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)

    def get_citations(self) -> list[str]:
        """Build formatted citation strings for all results.

        Returns:
            List of citation strings in the form "[N] Title. URL", one per
            search result, numbered from 1.
        """
        citations = []
        for i, r in enumerate(self.results, 1):
            citations.append(f"[{i}] {r.title}. {r.url}")
        return citations


class WebSearchTool:
    """Comprehensive web search tool with multi-backend support.

    Features:
    - Multiple search backends
    - Source credibility scoring
    - Provenance tracking
    - Citation generation
    - Rate limiting and caching
    """

    def __init__(
        self,
        backend: str | None = None,
        serapi_key: str | None = None,
        tavily_key: str | None = None,
        searxng_url: str | None = None,
        cache_ttl: int = 3600,
    ):
        """Initialize web search tool.

        Args:
            backend: Search backend to use (default: searxng; falls back to
                duckduckgo when SearXNG is unreachable)
            serapi_key: SerpAPI key for Google search
            tavily_key: Tavily API key (unused — kept for API compatibility)
            searxng_url: SearXNG instance URL (default: http://localhost:8888)
            cache_ttl: Cache TTL in seconds
        """
        self.backend_name = backend or DEFAULT_SEARCH_BACKEND
        self.serapi_key = serapi_key or os.environ.get("SERPAPI_KEY", "")
        self.tavily_key = tavily_key or os.environ.get("TAVILY_API_KEY", "")
        self.searxng_url = searxng_url or DEFAULT_SEARXNG_URL
        self.cache_ttl = cache_ttl

        # Simple in-memory cache
        self._cache: dict[str, tuple] = {}

        # Rate limiting
        self._request_times: list[float] = []
        self._min_request_interval = 1.0  # seconds

        # Setup backend
        self._setup_backend()

        logger.info("WebSearchTool initialized with backend: %s", self.backend_name)

    @staticmethod
    def _check_searxng_available(url: str) -> bool:
        """Probe the SearXNG instance to confirm it is reachable.

        Attempts a lightweight JSON search request with a 2-second timeout so
        that startup is not blocked when SearXNG is not running.

        Args:
            url: Base URL of the SearXNG instance (e.g. http://localhost:8888).

        Returns:
            True if SearXNG responded successfully, False otherwise.
        """
        try:
            import requests

            probe_url = f"{url.rstrip('/')}/search"
            resp = requests.get(probe_url, params={"q": "test", "format": "json"}, timeout=WEB_SEARCH_PROBE_TIMEOUT)
            return resp.status_code == 200
        except Exception:
            logger.warning(
                "SearXNG probe at %s failed — instance unreachable, will fall back to DuckDuckGo backend",
                url,
            )
            return False

    def _setup_backend(self) -> None:
        """Configure the active search function.

        When the configured backend is SearXNG but the instance is unreachable,
        automatically falls back to DuckDuckGo so the tool remains usable without
        requiring Docker to be running.
        """
        effective_backend = self.backend_name

        # Auto-fallback: SearXNG requested but not reachable → DuckDuckGo
        if effective_backend == SearchBackend.SEARXNG.value and not self._check_searxng_available(self.searxng_url):
            logger.info(
                "SearXNG not available at %s, falling back to DuckDuckGo",
                self.searxng_url,
            )
            effective_backend = SearchBackend.DUCKDUCKGO.value
            # Keep self.backend_name as "searxng" so callers know the intent;
            # _search_func handles the actual routing.

        if effective_backend == SearchBackend.DUCKDUCKGO.value:
            self._search_func = self._search_duckduckgo
        elif effective_backend == SearchBackend.SERPAPI.value:
            self._search_func = self._search_serpapi
        elif effective_backend == SearchBackend.TAVILY.value:
            # Tavily removed from core deps; route through DuckDuckGo as fallback
            logger.warning("Tavily backend is no longer supported; using DuckDuckGo instead")
            self._search_func = self._search_duckduckgo
        elif effective_backend == SearchBackend.WIKIPEDIA.value:
            self._search_func = self._search_wikipedia
        elif effective_backend == SearchBackend.ARXIV.value:
            self._search_func = self._search_arxiv
        elif effective_backend == SearchBackend.BRAVE.value:
            self._search_func = self._search_brave
        elif effective_backend == SearchBackend.SEARXNG.value:
            self._search_func = self._search_searxng
        else:
            logger.warning("Unknown backend %s, using DuckDuckGo", effective_backend)
            self._search_func = self._search_duckduckgo

    def search(
        self,
        query: str,
        max_results: int = 5,
        language: str = "en",
        time_range: str | None = None,
    ) -> SearchResponse:
        """Perform a web search.

        Args:
            query: Search query
            max_results: Maximum number of results
            language: Language code
            time_range: Time range (day, week, month, year)

        Returns:
            SearchResponse with results and metadata
        """
        start_time = time.time()

        # Check cache
        cache_key = f"{self.backend_name}:{query}:{max_results}:{language}:{time_range}"
        if cache_key in self._cache:
            cached_time, cached_response = self._cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                logger.debug("Cache hit for query: %s", query)
                return cached_response

        # Rate limiting
        self._apply_rate_limit()

        # Perform search
        try:
            results = self._search_func(query, max_results, language, time_range)
        except Exception as e:
            logger.error("Search failed: %s", e)
            results = []

        # Build response
        response = SearchResponse(
            results=results,
            query=query,
            backend=self.backend_name,
            total_results=len(results),
            execution_time_ms=int((time.time() - start_time) * 1000),
            citations=[f"[{i + 1}] {r.title}. {r.url}" for i, r in enumerate(results[:max_results])] if results else [],
            provenance=[
                {"backend": self.backend_name, "timestamp": datetime.now(timezone.utc).isoformat(), "query": query}
            ],
        )

        # Update cache
        self._cache[cache_key] = (time.time(), response)

        return response

    def search_news(
        self,
        query: str,
        max_results: int = 5,
        language: str = "en",
    ) -> SearchResponse:
        """Search for recent news articles using the Brave Search news endpoint.

        Delegates to :class:`~vetinari.tools.brave_search_tool.BraveSearchTool`
        when available and the API key is configured.  Falls back to a
        standard web search via the configured backend when Brave is
        unavailable so callers always receive a usable response.

        Args:
            query: News search query string.
            max_results: Maximum number of results to return.
            language: Language code for results (e.g. ``"en"``).

        Returns:
            SearchResponse containing news articles and metadata.
        """
        start_time = time.time()
        self._apply_rate_limit()

        results: list[SearchResult] = []
        backend_used = self.backend_name

        try:
            from vetinari.tools.brave_search_tool import BraveSearchTool

            brave = BraveSearchTool(language=language)
            if brave.is_available:
                results = brave.search_news(query, max_results=max_results)
                backend_used = "brave_news"
            else:
                logger.debug("BraveSearchTool not available for news — falling back to standard search")
        except ImportError:
            logger.debug("BraveSearchTool not installed — news search uses standard backend")
        except Exception:
            logger.warning(
                "Brave news search failed for query %r — falling back to standard search",
                query,
                exc_info=True,
            )

        if not results:
            # Fall back to standard web search when Brave is unavailable
            results = self._search_func(query, max_results, language, None)
            backend_used = self.backend_name

        return SearchResponse(
            results=results,
            query=query,
            backend=backend_used,
            total_results=len(results),
            execution_time_ms=int((time.time() - start_time) * 1000),
            citations=[f"[{i + 1}] {r.title}. {r.url}" for i, r in enumerate(results[:max_results])] if results else [],
            provenance=[{"backend": backend_used, "timestamp": datetime.now(timezone.utc).isoformat(), "query": query}],
        )

    def _apply_rate_limit(self):
        """Apply rate limiting between requests."""
        now = time.time()
        self._request_times = [t for t in self._request_times if now - t < 60]

        if self._request_times:
            time_since_last = now - self._request_times[-1]
            if time_since_last < self._min_request_interval:
                sleep_time = self._min_request_interval - time_since_last
                logger.debug("Rate limiting: sleeping %.2fs", sleep_time)
                time.sleep(sleep_time)

        self._request_times.append(now)

    @staticmethod
    def _search_brave(query: str, max_results: int, language: str, time_range: str) -> list[SearchResult]:
        """Delegate to web_search_backends.search_brave."""
        from vetinari.tools.web_search_backends import search_brave

        return search_brave(query, max_results, language, time_range)

    @staticmethod
    def _search_duckduckgo(query: str, max_results: int, language: str, time_range: str) -> list[SearchResult]:
        """Delegate to web_search_backends.search_duckduckgo."""
        from vetinari.tools.web_search_backends import search_duckduckgo

        return search_duckduckgo(query, max_results, language, time_range)

    @staticmethod
    def _search_duckduckgo_http(query: str, max_results: int, language: str) -> list[SearchResult]:
        """Delegate to web_search_backends.search_duckduckgo_http (HTTP fallback)."""
        from vetinari.tools.web_search_backends import search_duckduckgo_http

        return search_duckduckgo_http(query, max_results, language)

    def _search_serpapi(self, query: str, max_results: int, language: str, time_range: str) -> list[SearchResult]:
        """Delegate to web_search_backends.search_serpapi (Google via SerpAPI)."""
        from vetinari.tools.web_search_backends import search_serpapi

        return search_serpapi(query, max_results, language, time_range, self.serapi_key)

    def _search_tavily(self, query: str, max_results: int, language: str, time_range: str) -> list[SearchResult]:
        """Delegate to web_search_backends.search_tavily."""
        from vetinari.tools.web_search_backends import search_tavily

        return search_tavily(query, max_results, language, time_range, self.tavily_key)

    @staticmethod
    def _search_wikipedia(query: str, max_results: int, language: str, time_range: str) -> list[SearchResult]:
        """Delegate to web_search_backends.search_wikipedia."""
        from vetinari.tools.web_search_backends import search_wikipedia

        return search_wikipedia(query, max_results, language, time_range)

    @staticmethod
    def _search_arxiv(query: str, max_results: int, language: str, time_range: str) -> list[SearchResult]:
        """Delegate to web_search_backends.search_arxiv."""
        from vetinari.tools.web_search_backends import search_arxiv

        return search_arxiv(query, max_results, language, time_range)

    def _search_searxng(self, query: str, max_results: int, language: str, time_range: str) -> list[SearchResult]:
        """Delegate to web_search_backends.search_searxng."""
        from vetinari.tools.web_search_backends import search_searxng

        return search_searxng(query, max_results, language, time_range, self.searxng_url)

    def search_multiple_queries(self, queries: list[str], max_results_per_query: int = 3) -> list[SearchResponse]:
        """Search multiple queries and aggregate results.

        Useful for comprehensive research on a topic.

        Args:
            queries: List of search query strings to execute.
            max_results_per_query: Maximum results fetched per individual query.

        Returns:
            List of SearchResponse objects, one per successfully executed query.
            Queries that raise exceptions are skipped and logged.
        """
        all_responses = []

        for query in queries:
            try:
                response = self.search(query, max_results=max_results_per_query)
                all_responses.append(response)
            except Exception as e:
                logger.error("Query '%s' failed: %s", query, e)

        return all_responses

    def research_topic(self, topic: str, aspects: list[str] | None = None) -> dict[str, Any]:
        """Perform comprehensive research on a topic. See ``web_search_research``.

        Args:
            topic: The subject to research (e.g. "Python async performance").
            aspects: Specific dimensions to investigate. None uses a default set of aspects.

        Returns:
            Dict with ``topic``, ``aspects`` studied, and ``findings`` per aspect.
        """
        from vetinari.tools.web_search_research import research_topic as _research_topic

        return _research_topic(self, topic, aspects)

    def clear_cache(self) -> None:
        """Clear the search cache."""
        self._cache.clear()
        logger.info("Search cache cleared")

    def multi_source_search(
        self,
        query: str,
        max_results: int = 5,
        backends: list[str] | None = None,
    ) -> SearchResponse:
        """Search across multiple backends and merge results. See ``web_search_research``.

        Args:
            query: Search query string to submit to all backends.
            max_results: Maximum number of results to return per backend.
            backends: Backend names to query (e.g. ["brave", "duckduckgo"]). None uses all available.

        Returns:
            Merged SearchResponse with deduplicated results from all queried backends.
        """
        from vetinari.tools.web_search_research import multi_source_search as _multi_source_search

        return _multi_source_search(self, query, max_results, backends)

    def verify_claim(
        self,
        claim: str,
        min_sources: int = 2,
        max_search_results: int = 5,
    ) -> dict[str, Any]:
        """Verify a factual claim against multiple web sources. See ``web_search_research``.

        Args:
            claim: The factual statement to verify (e.g. "Python was created in 1991").
            min_sources: Minimum number of sources required to mark a claim as verified.
            max_search_results: Maximum search results to retrieve when gathering evidence.

        Returns:
            Dict with ``verified`` bool, ``confidence`` score, and ``sources`` supporting or refuting the claim.
        """
        from vetinari.tools.web_search_research import verify_claim as _verify_claim

        return _verify_claim(self, claim, min_sources, max_search_results)


# Global instance
_search_tool: WebSearchTool | None = None
_search_tool_lock = threading.Lock()


def get_search_tool() -> WebSearchTool:
    """Get or create the global search tool instance.

    Returns:
        The singleton WebSearchTool, constructed with default backend
        settings on first call.
    """
    global _search_tool
    if _search_tool is None:
        with _search_tool_lock:
            if _search_tool is None:
                _search_tool = WebSearchTool()
    return _search_tool
