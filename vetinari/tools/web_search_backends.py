"""Backend search implementations for WebSearchTool.

Each function implements one external search provider and returns a list of
``SearchResult`` objects.  All backends are called by ``WebSearchTool`` in
``vetinari.tools.web_search_tool``; they are kept here to stay under the
550-line ceiling.

Backends covered:
- DuckDuckGo (library + HTTP fallback)
- SerpAPI (Google)
- Tavily
- Wikipedia
- arXiv
- SearXNG
- Brave (via BraveSearchTool)
"""

from __future__ import annotations

import logging
import re
from importlib import import_module
from typing import Any

from vetinari.constants import WEB_SEARCH_SHORT_TIMEOUT
from vetinari.tools.web_search_provider_backends import (
    _WIKIPEDIA_HOSTS as _WIKIPEDIA_HOSTS,
)
from vetinari.tools.web_search_provider_backends import (
    _redact_query as _redact_query,
)
from vetinari.tools.web_search_provider_backends import (
    _resolve_wikipedia_host as _resolve_wikipedia_host,
)
from vetinari.tools.web_search_provider_backends import (
    search_arxiv as search_arxiv,
)
from vetinari.tools.web_search_provider_backends import (
    search_brave as search_brave,
)
from vetinari.tools.web_search_provider_backends import (
    search_searxng as search_searxng,
)
from vetinari.tools.web_search_provider_backends import (
    search_serpapi as search_serpapi,
)
from vetinari.tools.web_search_provider_backends import (
    search_tavily as search_tavily,
)
from vetinari.tools.web_search_provider_backends import (
    search_wikipedia as search_wikipedia,
)
from vetinari.tools.web_search_types import SearchResult, SourceCredibility

logger = logging.getLogger(__name__)


DEFAULT_SEARCH_FALLBACK_ORDER = ("brave", "duckduckgo")


# Optional DDG library — same availability check as the parent module.
_DDG_PROVIDER = "none"
_DDGS: Any | None = None
try:
    try:
        _DDGS = import_module("ddgs").DDGS

        _DDG_PROVIDER = "ddgs"
    except ImportError as exc:
        raise ImportError("Install the 'search' extra: pip install vetinari[search]") from exc
    _DDG_AVAILABLE = True
except ImportError:
    _DDG_AVAILABLE = False

# ---------------------------------------------------------------------------
# DuckDuckGo
# ---------------------------------------------------------------------------


def search_duckduckgo(
    query: str,
    max_results: int,
    language: str,
    time_range: str | None,
) -> list[SearchResult]:
    """Search using DuckDuckGo via the ddgs/duckduckgo-search library or HTTP fallback.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.
        language: BCP-47 language/region code passed to the DDG API.
        time_range: Optional time filter (day/week/month/year); unused by HTTP path.

    Returns:
        List of SearchResult objects, possibly empty on failure.
    """
    if _DDG_PROVIDER == "ddgs" and _DDG_AVAILABLE and _DDGS is not None:
        try:
            results: list[SearchResult] = []
            with _DDGS() as ddgs:
                results.extend(
                    SearchResult(
                        title=r.get("title", ""),
                        url=r.get("href", ""),
                        snippet=r.get("body", ""),
                        source_reliability=SourceCredibility.score_url(r.get("href", "")),
                        source_type="web",
                        query_used=query,
                    )
                    for r in ddgs.search(query, max_results=max_results, region=language)
                )
            return results
        except Exception as exc:
            logger.error(
                "DuckDuckGo library search failed for %r — falling back to HTTP: %s",
                _redact_query(query),
                exc,
            )
            return search_duckduckgo_http(query, max_results, language)
    if _DDG_PROVIDER == "duckduckgo_search":
        logger.info("Using DuckDuckGo HTTP fallback because only legacy duckduckgo_search is installed")
        return search_duckduckgo_http(query, max_results, language)
    else:
        logger.warning("ddgs/duckduckgo-search not installed — using HTTP fallback")
        return search_duckduckgo_http(query, max_results, language)


def search_duckduckgo_http(
    query: str,
    max_results: int,
    language: str,
) -> list[SearchResult]:
    """Fallback DuckDuckGo search using plain HTTP requests.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.
        language: Language code (currently unused in HTTP path).

    Returns:
        List of SearchResult objects parsed from the HTML response.
    """
    try:
        import requests

        url = "https://html.duckduckgo.com/html/"
        data = {"q": query, "b": ""}

        resp = requests.post(url, data=data, timeout=WEB_SEARCH_SHORT_TIMEOUT)
        resp.raise_for_status()

        results = []
        pattern = (
            r'<a class="result__a" href="([^"]+)"[^>]*>([^<]+)</a>.*?'
            r'<a class="result__snippet"[^>]*>([^<]+)</a>'
        )

        for match in re.findall(pattern, resp.text, re.DOTALL)[:max_results]:
            result_url, title, snippet = match
            results.append(
                SearchResult(
                    title=title.strip(),
                    url=result_url,
                    snippet=snippet.strip(),
                    source_reliability=SourceCredibility.score_url(result_url),
                    source_type="web",
                    query_used=query,
                ),
            )

        return results
    except Exception as exc:
        logger.error("DuckDuckGo HTTP search failed for %s: %s", _redact_query(query), exc)
        return []
