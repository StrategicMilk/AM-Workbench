"""Brave Search Tool for Vetinari.

Provides web search capabilities via the Brave Search API, complementing
the existing DuckDuckGo and SearXNG backends in WebSearchTool.

Uses Vetinari's governed HTTP client and a valid API key in the
``BRAVE_SEARCH_API_KEY`` environment variable.  When the key is unavailable,
all methods raise informative errors rather than silently returning empty
results.

Usage::

    from vetinari.tools.brave_search_tool import BraveSearchTool

    tool = BraveSearchTool()
    results = tool.search("Python async best practices", max_results=5)
    for r in results:
        logger.debug("%s — %s", r.title, r.url)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from vetinari.constants import WEB_SEARCH_SHORT_TIMEOUT
from vetinari.http import GovernedHttpClient, GovernedHttpConfig, create_governed_client
from vetinari.resilience import RetryBudget, RetryPolicy
from vetinari.tools.web_search_types import SearchResult

logger = logging.getLogger(__name__)


_BRAVE_AVAILABLE = True


# ── Constants ────────────────────────────────────────────────────────

_DEFAULT_MAX_RESULTS = 10
_DEFAULT_COUNTRY = "US"
_DEFAULT_LANGUAGE = "en"
_WEB_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_NEWS_SEARCH_URL = "https://api.search.brave.com/res/v1/news/search"

# Brave Search tends to surface high-quality results for technical queries
_BASE_RELIABILITY_SCORE = 0.7


def _redact_query(query: str) -> str:
    return f"<redacted:{len(query)} chars>"


# ── BraveSearchTool ──────────────────────────────────────────────────


class BraveSearchTool:
    """Web search via the Brave Search API.

    Uses raw governed HTTP to provide search results in the same
    ``SearchResult`` format used by ``WebSearchTool``.

    Args:
        api_key: Brave Search API key.  Defaults to the
            ``BRAVE_SEARCH_API_KEY`` environment variable.
        country: Country code for search localisation.
        language: Language code for results.
    """

    def __init__(
        self,
        api_key: str | None = None,
        country: str = _DEFAULT_COUNTRY,
        language: str = _DEFAULT_LANGUAGE,
        http_client: GovernedHttpClient | None = None,
        timeout_seconds: float = WEB_SEARCH_SHORT_TIMEOUT,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("BRAVE_SEARCH_API_KEY", "")
        self._country = country
        self._language = language
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client or create_governed_client(
            GovernedHttpConfig(
                timeout_seconds=timeout_seconds,
                retry_budget=RetryBudget(max_attempts=2, base_delay_seconds=0.2, max_delay_seconds=1.0),
                telemetry_label="brave_search",
                comparison_label="brave_sdk",
            ),
            retry_policy=retry_policy,
        )

        if self._api_key:
            logger.info("BraveSearchTool: initialized with governed HTTP")
        else:
            logger.debug(
                "BraveSearchTool: no API key found; set BRAVE_SEARCH_API_KEY environment variable",
            )

    @property
    def is_available(self) -> bool:
        """Whether the Brave Search client is configured and ready.

        Returns:
            True if the raw HTTP client has a valid API key.
        """
        return bool(self._api_key)

    def search(
        self,
        query: str,
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> list[SearchResult]:
        """Execute a web search and return structured results.

        Args:
            query: The search query string.
            max_results: Maximum number of results to return.

        Returns:
            List of SearchResult objects with title, URL, snippet, and
            provenance metadata.

        Raises:
            RuntimeError: If the client is not available (missing package
                or API key).
        """
        if not self.is_available:
            raise RuntimeError("BraveSearchTool not available: BRAVE_SEARCH_API_KEY not set.")

        try:
            return self.search_required(query, max_results=max_results)
        except Exception as exc:
            logger.warning("BraveSearchTool: search failed for %s: %s", _redact_query(query), exc)
            return []

    def search_required(
        self,
        query: str,
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> list[SearchResult]:
        """Execute a web search where unavailable or empty results are failures.

        Args:
            query: Search query string to submit to Brave.
            max_results: Maximum number of results to request.

        Returns:
            Structured web search results.

        Raises:
            RuntimeError: If the API key is missing or Brave returns no
                structured web results.
        """
        if not self.is_available:
            raise RuntimeError("BraveSearchTool not available: BRAVE_SEARCH_API_KEY not set.")
        raw_results = self._request(_WEB_SEARCH_URL, query, max_results)
        results = self._parse_results(raw_results, query)
        if not results:
            raise RuntimeError("BraveSearchTool returned no structured web results")
        return results

    def search_news(
        self,
        query: str,
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> list[SearchResult]:
        """Search for recent news articles.

        Args:
            query: The news search query.
            max_results: Maximum number of results.

        Returns:
            List of SearchResult objects from news sources.

        Raises:
            RuntimeError: If the client is not available.
        """
        if not self.is_available:
            raise RuntimeError("BraveSearchTool not available for news search")

        try:
            return self.search_news_required(query, max_results=max_results)
        except Exception as exc:
            logger.warning("BraveSearchTool: news search failed for %s: %s", _redact_query(query), exc)
            return []

    def search_news_required(
        self,
        query: str,
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> list[SearchResult]:
        """Search news where unavailable or empty results are failures.

        Args:
            query: News search query string to submit to Brave.
            max_results: Maximum number of results to request.

        Returns:
            Structured news search results.

        Raises:
            RuntimeError: If the API key is missing or Brave returns no
                structured news results.
        """
        if not self.is_available:
            raise RuntimeError("BraveSearchTool not available for news search")
        raw_results = self._request(_NEWS_SEARCH_URL, query, max_results)
        results = self._parse_results(raw_results, query, source_type="news")
        if not results:
            raise RuntimeError("BraveSearchTool returned no structured news results")
        return results

    def _request(self, url: str, query: str, max_results: int) -> dict[str, Any]:
        """Call Brave's HTTPS API with bounded timeout and retry behavior."""
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
        }
        params = {
            "q": query,
            "count": max(1, min(max_results, 20)),
            "country": self._country,
            "search_lang": self._language,
        }
        resp = self._http_client.get(
            url,
            headers=headers,
            params=params,
            timeout=self._timeout_seconds,
            allow_redirects=False,
            telemetry_label="brave_search",
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    def _parse_results(
        self,
        raw: Any,
        query: str,
        source_type: str = "web",
    ) -> list[SearchResult]:
        """Parse raw Brave API response into SearchResult objects.

        Args:
            raw: Raw response from the Brave Search client.
            query: The original query (for provenance).
            source_type: Type of source (web, news).

        Returns:
            Parsed list of SearchResult objects.
        """
        results: list[SearchResult] = []

        # Brave API returns results in web.results or news.results
        items: list[dict[str, Any]] = []
        if isinstance(raw, dict):
            web_results = raw.get("web", {})
            if isinstance(web_results, dict):
                items = web_results.get("results", [])
            if not items:
                news_results = raw.get("news", {})
                if isinstance(news_results, dict):
                    items = news_results.get("results", [])
        elif hasattr(raw, "web_results"):
            # Object-style response from newer client versions
            items = getattr(raw, "web_results", []) or []

        for item in items:
            if isinstance(item, dict):
                title = item.get("title", "")
                url = item.get("url", "")
                snippet = item.get("description", "") or item.get("snippet", "")
                published = item.get("published_at") or item.get("age")
            else:
                # Object-style item
                title = getattr(item, "title", "")
                url = getattr(item, "url", "")
                snippet = getattr(item, "description", "") or getattr(item, "snippet", "")
                published = getattr(item, "published_at", None) or getattr(item, "age", None)

            if title and url:
                results.append(
                    SearchResult(
                        title=title,
                        url=url,
                        snippet=snippet,
                        published_at=str(published) if published else None,
                        source_reliability=_BASE_RELIABILITY_SCORE,
                        source_type=source_type,
                        query_used=query,
                    ),
                )

        return results

    def get_stats(self) -> dict[str, Any]:
        """Return tool status information.

        Returns:
            Dictionary with availability status and configuration.
        """
        return {
            "available": self.is_available,
            "client_installed": False,
            "api_key_set": bool(self._api_key),
            "country": self._country,
            "language": self._language,
            "transport": "governed_http",
        }
