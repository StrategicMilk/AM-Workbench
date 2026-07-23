"""Additional web search provider implementations."""

from __future__ import annotations

import logging
import re
from typing import cast
from urllib.parse import urljoin, urlparse

from vetinari.boundary_guards import account_evidence_drop
from vetinari.constants import WEB_SEARCH_LONG_TIMEOUT, WEB_SEARCH_SHORT_TIMEOUT
from vetinari.security.fail_closed import sanitize_untrusted_text
from vetinari.tools.web_search_types import SearchResult, SourceCredibility

logger = logging.getLogger(__name__)


_WIKIPEDIA_HOSTS = {
    "ar": "ar.wikipedia.org",
    "de": "de.wikipedia.org",
    "en": "en.wikipedia.org",
    "es": "es.wikipedia.org",
    "fr": "fr.wikipedia.org",
    "it": "it.wikipedia.org",
    "ja": "ja.wikipedia.org",
    "ko": "ko.wikipedia.org",
    "pt": "pt.wikipedia.org",
    "ru": "ru.wikipedia.org",
    "zh": "zh.wikipedia.org",
}


def _redact_query(query: str) -> str:
    return f"<redacted:{len(query)} chars>"


def _safe_query(query: str) -> str:
    return sanitize_untrusted_text(query, max_length=2_000)


def _resolve_wikipedia_host(language: str) -> str | None:
    lang_code = language.split("-", maxsplit=1)[0].lower()
    if not lang_code.isalpha():
        return None
    return _WIKIPEDIA_HOSTS.get(lang_code)


def _duckduckgo_fallback(
    query: str,
    max_results: int,
    language: str,
    time_range: str | None,
) -> list[SearchResult]:
    from vetinari.tools.web_search_backends import search_duckduckgo

    return search_duckduckgo(query, max_results, language, time_range)


# ---------------------------------------------------------------------------
# SerpAPI (Google)
# ---------------------------------------------------------------------------


def search_serpapi(
    query: str,
    max_results: int,
    language: str,
    time_range: str | None,
    serapi_key: str,
) -> list[SearchResult]:
    """Search using SerpAPI (Google).

    Falls back to DuckDuckGo when the API key is absent.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.
        language: Language code (forwarded to SerpAPI).
        time_range: Optional time filter passed to the ``tbs`` parameter.
        serapi_key: SerpAPI authentication key.

    Returns:
        List of SearchResult objects, possibly empty on failure.

    Raises:
        RuntimeError: If the SerpAPI request or response handling fails.
    """
    query = _safe_query(query)
    language = sanitize_untrusted_text(language, max_length=40)
    if not serapi_key:
        logger.warning("SerpAPI key not set — falling back to DuckDuckGo for query %s", _redact_query(query))
        return _duckduckgo_fallback(query, max_results, language, time_range)

    try:
        import requests

        params: dict = {
            "q": query,
            "api_key": serapi_key,
            "num": max_results,
            "engine": "google",
        }
        if time_range:
            params["tbs"] = f"qdr:{time_range}"

        resp = requests.get(
            "https://serpapi.com/search",
            params=params,
            timeout=WEB_SEARCH_LONG_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                published_at=item.get("date"),
                source_reliability=SourceCredibility.score_url(item.get("link", "")),
                source_type="web",
                query_used=query,
            )
            for item in data.get("organic_results", [])[:max_results]
        ]
    except Exception as exc:
        logger.error("SerpAPI search failed for %s: %s", _redact_query(query), exc)
        account_evidence_drop("serpapi", "web_search_provider_failure", logger=logger)
        raise RuntimeError("SerpAPI search failed") from exc


# ---------------------------------------------------------------------------
# Tavily
# ---------------------------------------------------------------------------


def search_tavily(
    query: str,
    max_results: int,
    language: str,
    time_range: str | None,
    tavily_key: str,
) -> list[SearchResult]:
    """Search using the Tavily AI-optimized search API.

    Falls back to DuckDuckGo when the API key is absent.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.
        language: Language code (unused by Tavily API).
        time_range: Time filter (unused by Tavily API).
        tavily_key: Tavily authentication key.

    Returns:
        List of SearchResult objects, possibly empty on failure.

    Raises:
        RuntimeError: If the Tavily request or response handling fails.
    """
    query = _safe_query(query)
    if not tavily_key:
        logger.warning("Tavily key not set — falling back to DuckDuckGo for query %s", _redact_query(query))
        return _duckduckgo_fallback(query, max_results, language, time_range)

    query = _safe_query(query)
    language = sanitize_untrusted_text(language, max_length=40)
    try:
        import requests

        headers = {"Authorization": f"Bearer {tavily_key}"}
        payload = {
            "query": query,
            "max_results": max_results,
            "include_answer": True,
            "include_raw_content": False,
        }

        resp = requests.post(
            "https://api.tavily.com/search",
            json=payload,
            headers=headers,
            timeout=WEB_SEARCH_LONG_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                source_reliability=SourceCredibility.score_url(item.get("url", "")),
                source_type="web",
                query_used=query,
            )
            for item in data.get("results", [])[:max_results]
        ]
    except Exception as exc:
        logger.error("Tavily search failed for %s: %s", _redact_query(query), exc)
        account_evidence_drop("tavily", "web_search_provider_failure", logger=logger)
        raise RuntimeError("Tavily search failed") from exc


# ---------------------------------------------------------------------------
# Wikipedia
# ---------------------------------------------------------------------------


def search_wikipedia(
    query: str,
    max_results: int,
    language: str,
    time_range: str | None,
) -> list[SearchResult]:
    """Search Wikipedia directly via its public REST API.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.
        language: BCP-47 language tag; the prefix is used as the Wikipedia
            subdomain (e.g. ``en`` for ``en.wikipedia.org``).
        time_range: Unused — Wikipedia search does not support time filtering.

    Returns:
        List of SearchResult objects, possibly empty on failure.

    Raises:
        RuntimeError: If the Wikipedia request or response handling fails.
    """
    query = _safe_query(query)
    try:
        import requests

        host = _resolve_wikipedia_host(language)
        if host is None:
            logger.warning("Wikipedia language not supported: %r", language)
            return []

        url = f"https://{host}/w/api.php"
        params: dict[str, str | int] = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": max_results,
            "format": "json",
            "origin": "*",
        }

        resp = requests.get(url, params=params, timeout=WEB_SEARCH_SHORT_TIMEOUT, allow_redirects=False)
        status_code = getattr(resp, "status_code", None)
        if isinstance(status_code, int) and 300 <= status_code < 400:
            location = resp.headers.get("location", "")
            redirected = urljoin(url, location)
            parsed = urlparse(redirected)
            if parsed.scheme != "https" or parsed.netloc != host:
                logger.warning("Wikipedia redirect rejected for host %s", host)
                return []
            resp = requests.get(redirected, params=params, timeout=WEB_SEARCH_SHORT_TIMEOUT, allow_redirects=False)
        resp.raise_for_status()
        data = resp.json()

        results: list[SearchResult] = []
        for item in data.get("query", {}).get("search", [])[:max_results]:
            page_id = item.get("pageid")
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=f"https://{host}/wiki?curid={page_id}",
                    snippet=re.sub(r"<[^>]+>", "", item.get("snippet", "")),
                    source_reliability=0.9,
                    source_type="wikipedia",
                    query_used=query,
                ),
            )

        return results
    except Exception as exc:
        logger.error("Wikipedia search failed for %s: %s", _redact_query(query), exc)
        account_evidence_drop("wikipedia", "web_search_provider_failure", logger=logger)
        raise RuntimeError("Wikipedia search failed") from exc


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------


def search_arxiv(
    query: str,
    max_results: int,
    language: str,
    time_range: str | None,
) -> list[SearchResult]:
    """Search arXiv for academic papers via the Atom feed API.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.
        language: Unused — arXiv search is language-agnostic.
        time_range: Unused — arXiv API does not expose time filtering in the
            basic query parameters.

    Returns:
        List of SearchResult objects, possibly empty on failure.

    Raises:
        RuntimeError: If the arXiv request or response handling fails.
    """
    try:
        import requests
        from defusedxml.ElementTree import fromstring as _xml_fromstring

        url = "https://export.arxiv.org/api/query"
        params: dict[str, str | int] = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }

        resp = requests.get(url, params=params, timeout=WEB_SEARCH_SHORT_TIMEOUT)
        resp.raise_for_status()

        root = _xml_fromstring(resp.content)
        ns = {"atom": "http://www.w3.org/2005/Atom"}

        results = []
        for entry in root.findall("atom:entry", ns)[:max_results]:
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            link_el = entry.find("atom:id", ns)
            published_el = entry.find("atom:published", ns)

            title = title_el.text.strip() if title_el is not None else ""
            summary = summary_el.text.strip() if summary_el is not None else ""
            link = link_el.text if link_el is not None else ""
            published = published_el.text if published_el is not None else None

            results.append(
                SearchResult(
                    title=title,
                    url=link,
                    snippet=summary[:300] + "..." if len(summary) > 300 else summary,
                    published_at=published,
                    source_reliability=0.95,
                    source_type="arxiv",
                    query_used=query,
                ),
            )

        return results
    except Exception as exc:
        logger.error("arXiv search failed for %s: %s", _redact_query(query), exc)
        account_evidence_drop("arxiv", "web_search_provider_failure", logger=logger)
        raise RuntimeError("arXiv search failed") from exc


# ---------------------------------------------------------------------------
# SearXNG
# ---------------------------------------------------------------------------


def search_searxng(
    query: str,
    max_results: int,
    language: str,
    time_range: str | None,
    searxng_url: str,
) -> list[SearchResult]:
    """Search using a self-hosted SearXNG instance.

    Falls back gracefully when the SearXNG URL is not configured.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.
        language: BCP-47 language/region code forwarded to SearXNG.
        time_range: Unused by this implementation (SearXNG supports it but the
            parameter is not exposed here for simplicity).
        searxng_url: Base URL of the SearXNG instance
            (e.g. ``http://localhost:8888``).

    Returns:
        List of SearchResult objects, possibly empty on failure.

    Raises:
        RuntimeError: If the SearXNG request or response handling fails.
    """
    query = _safe_query(query)
    language = sanitize_untrusted_text(language, max_length=40)
    if not searxng_url:
        logger.warning("SearXNG URL not configured — falling back to DuckDuckGo for %s", _redact_query(query))
        return _duckduckgo_fallback(query, max_results, language, time_range)

    try:
        import requests

        url = f"{searxng_url.rstrip('/')}/search"
        params: dict[str, str] = {
            "q": query,
            "format": "json",
            "engines": "google,duckduckgo,bing",
            "language": language,
        }

        resp = requests.get(url, params=params, timeout=WEB_SEARCH_LONG_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                source_reliability=SourceCredibility.score_url(item.get("url", "")),
                source_type="searxng",
                query_used=query,
            )
            for item in data.get("results", [])[:max_results]
        ]
    except Exception as exc:
        logger.error("SearXNG search failed for %s: %s", _redact_query(query), exc)
        account_evidence_drop("searxng", "web_search_provider_failure", logger=logger)
        raise RuntimeError("SearXNG search failed") from exc


# ---------------------------------------------------------------------------
# Brave
# ---------------------------------------------------------------------------


def search_brave(
    query: str,
    max_results: int,
    language: str,
    time_range: str | None,
    *,
    timeout_seconds: float | None = None,
    fallback_order: tuple[str, ...] = ("brave", "duckduckgo"),
) -> list[SearchResult]:
    """Search using the Brave Search API via BraveSearchTool.

    Falls back to DuckDuckGo if BraveSearchTool is unavailable (missing
    package or API key).

    Args:
        query: Search query string.
        max_results: Maximum number of results.
        language: Language code (forwarded to BraveSearchTool).
        time_range: Optional time filter (unused by Brave API).
        timeout_seconds: Optional raw HTTP timeout override.
        fallback_order: Provider order; non-Brave first entry uses DuckDuckGo first.

    Returns:
        List of SearchResult objects.
    """
    query = _safe_query(query)
    language = sanitize_untrusted_text(language, max_length=40)
    if fallback_order and fallback_order[0] != "brave":
        logger.debug("Brave fallback order starts with %s; using DuckDuckGo first", fallback_order[0])
        return _duckduckgo_fallback(query, max_results, language, time_range)

    try:
        from vetinari.tools.brave_search_tool import BraveSearchTool

        brave_kwargs: dict[str, object] = {"language": language}
        if timeout_seconds is not None:
            brave_kwargs["timeout_seconds"] = timeout_seconds
        brave = BraveSearchTool(**brave_kwargs)
        if brave.is_available:
            results = brave.search(query, max_results=max_results)
            if results:
                return cast(list[SearchResult], results)
            logger.debug("Brave Search returned no results — falling back to DuckDuckGo")
        else:
            logger.debug("BraveSearchTool not available — falling back to DuckDuckGo")
    except Exception as exc:
        logger.warning(
            "Brave Search failed for %r — falling back to DuckDuckGo: %s",
            _redact_query(query),
            exc,
        )

    return _duckduckgo_fallback(query, max_results, language, time_range)
