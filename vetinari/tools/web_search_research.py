"""Web search research helpers — composite search operations.

Higher-level search operations (topic research, multi-source aggregation,
claim verification) extracted from WebSearchTool to keep that class within
the 550-line limit while retaining a clean public interface.

Functions in this module accept a ``tool: WebSearchTool`` as their first
argument so they can call ``tool.search()`` and ``tool.multi_source_search()``
without creating circular imports.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vetinari.tools.web_search_tool import WebSearchTool

from vetinari.tools.web_search_tool import SearchResponse
from vetinari.tools.web_search_types import SearchResult

logger = logging.getLogger(__name__)


__all__ = [
    "multi_source_search",
    "research_topic",
    "verify_claim",
]


def research_topic(tool: WebSearchTool, topic: str, aspects: list[str] | None = None) -> dict[str, Any]:
    """Perform comprehensive research on a topic across multiple search queries.

    Generates aspect-specific queries from the topic (e.g. "overview",
    "implementation", "best practices", "tutorials"), searches each in turn,
    deduplicates results by URL, and sorts by source credibility.

    Args:
        tool: The WebSearchTool instance used for individual searches.
        topic: Main topic to research.
        aspects: Specific search queries to use instead of the auto-generated
            defaults.

    Returns:
        Dict with keys: topic (str), total_sources (int),
        high_credibility_sources (int), results (list[dict]),
        citations (list[str]), research_queries (list[str]).
    """
    if aspects is None:
        aspects = [
            f"{topic} overview",
            f"{topic} implementation",
            f"{topic} best practices",
            f"{topic} tutorials",
        ]

    responses = tool.search_multiple_queries(aspects, max_results_per_query=3)

    all_results: list[SearchResult] = []
    for response in responses:
        all_results.extend(response.results)

    # Deduplicate by URL
    seen_urls: set[str] = set()
    unique_results: list[SearchResult] = []
    for r in all_results:
        if r.url not in seen_urls:
            seen_urls.add(r.url)
            unique_results.append(r)

    unique_results.sort(key=lambda x: x.source_reliability, reverse=True)

    return {
        "topic": topic,
        "total_sources": len(unique_results),
        "high_credibility_sources": sum(1 for r in unique_results if r.source_reliability >= 0.8),
        "results": [r.to_dict() for r in unique_results],
        "citations": [f"[{i + 1}] {r.title}: {r.url}" for i, r in enumerate(unique_results[:10])],
        "research_queries": aspects,
    }


def multi_source_search(
    tool: WebSearchTool,
    query: str,
    max_results: int = 5,
    backends: list[str] | None = None,
) -> SearchResponse:
    """Search across multiple backends and merge results.

    Queries each backend in turn, deduplicates by URL, and sorts by source
    credibility.  Temporarily swaps ``tool.backend_name`` per iteration and
    restores it afterwards so the tool's configured backend is unchanged after
    the call.

    Args:
        tool: The WebSearchTool instance to query.
        query: The search query.
        max_results: Max results fetched per backend.
        backends: Backend names to query. Defaults to
            ["duckduckgo", "wikipedia", "arxiv"].

    Returns:
        Merged SearchResponse with deduplicated, credibility-sorted results.
    """
    import time

    if backends is None:
        backends = ["duckduckgo", "wikipedia", "arxiv"]

    start_time = time.time()
    all_results: list[SearchResult] = []
    seen_urls: set[str] = set()
    provenance: list[dict[str, Any]] = []

    original_backend = tool.backend_name
    for backend in backends:
        try:
            tool.backend_name = backend
            response = tool.search(query, max_results=max_results)
            actual_backend = response.backend or backend
            for r in response.results:
                if r.url not in seen_urls:
                    seen_urls.add(r.url)
                    all_results.append(r)
            provenance.append({
                "backend": backend,
                "actual_backend": actual_backend,
                "backend_matched": actual_backend == backend,
                "result_count": len(response.results),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as exc:
            logger.warning(
                "Backend %s failed for query '%s' — skipping this backend: %s",
                backend,
                query,
                exc,
            )
    tool.backend_name = original_backend

    all_results.sort(key=lambda x: x.source_reliability, reverse=True)
    all_results = all_results[: max_results * len(backends)]

    return SearchResponse(
        results=all_results,
        query=query,
        backend=f"multi:{','.join(backends)}",
        total_results=len(all_results),
        execution_time_ms=int((time.time() - start_time) * 1000),
        citations=[f"[{i + 1}] {r.title}. {r.url}" for i, r in enumerate(all_results)],
        provenance=provenance,
    )


def verify_claim(
    tool: WebSearchTool,
    claim: str,
    min_sources: int = 2,
    max_search_results: int = 5,
) -> dict[str, Any]:
    """Verify a factual claim against multiple web sources.

    Implements the "two-source rule": a claim is considered verified when at
    least ``min_sources`` independent sources corroborate it.  Searches both
    for the claim and for its negation to detect contradictions.

    Args:
        tool: The WebSearchTool instance to query.
        claim: The factual statement to verify.
        min_sources: Minimum corroborating sources required for VERIFIED status.
        max_search_results: Max results per individual search call.

    Returns:
        Dict with keys: claim (str), verified (bool), confidence (float),
        verdict (str — "VERIFIED" | "UNVERIFIED" | "UNCERTAIN"),
        supporting_sources (list[dict]), contradicting_sources (list[dict]),
        min_sources_required (int).
    """
    supporting_results = tool.multi_source_search(claim, max_results=max_search_results).results
    negation_query = f"NOT ({claim}) OR problems with {claim}"
    contradicting_results = tool.search(negation_query, max_results=3).results

    claim_lower = claim.lower()
    claim_words = set(claim_lower.split())

    supporting: list[dict[str, Any]] = []
    contradicting: list[dict[str, Any]] = []

    for r in supporting_results:
        text = f"{r.title} {r.snippet}".lower()
        overlap = len(claim_words & set(text.split())) / max(len(claim_words), 1)
        if overlap > 0.2:
            supporting.append({
                "title": r.title,
                "url": r.url,
                "overlap": round(overlap, 2),
                "reliability": r.source_reliability,
            })

    for r in contradicting_results:
        text = f"{r.title} {r.snippet}".lower()
        if any(w in text for w in ["not", "false", "wrong", "incorrect", "debunked", "myth"]):
            contradicting.append({"title": r.title, "url": r.url, "reliability": r.source_reliability})

    verified = len(supporting) >= min_sources
    confidence = min(1.0, len(supporting) / max(min_sources, 1)) * 0.9
    if contradicting:
        confidence *= 0.7

    if verified:
        verdict = "VERIFIED"
    elif not supporting:
        verdict = "UNVERIFIED"
    else:
        verdict = "UNCERTAIN"

    return {
        "claim": claim,
        "verified": verified,
        "confidence": round(confidence, 2),
        "verdict": verdict,
        "supporting_sources": supporting[:5],
        "contradicting_sources": contradicting[:3],
        "min_sources_required": min_sources,
    }
