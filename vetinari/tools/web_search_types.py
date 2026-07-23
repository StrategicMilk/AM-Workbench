"""Shared types for the Vetinari web search subsystem.

Extracted from ``web_search_tool`` so that ``web_search_backends`` can import
them without creating a circular dependency.

Types defined here:
    SearchResult       — a single result with provenance metadata.
    SourceCredibility  — domain-based credibility scoring helper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from vetinari.utils.serialization import dataclass_to_dict


@dataclass(frozen=True, slots=True)
class SearchResult:
    """Single search result with provenance tracking."""

    title: str
    url: str
    snippet: str
    published_at: str | None = None
    source_reliability: float = 0.5  # 0.0 - 1.0
    source_type: str = "web"
    query_used: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        """Show key identifying fields for debugging."""
        return (
            f"SearchResult(title={self.title!r},"
            f" source_type={self.source_type!r},"
            f" source_reliability={self.source_reliability!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)


class SourceCredibility:
    """Source credibility scoring for search results."""

    # Credibility scores by domain type
    DOMAIN_SCORES = {
        # High credibility
        "arxiv.org": 0.95,
        "wikipedia.org": 0.9,
        "github.com": 0.85,
        "stackoverflow.com": 0.8,
        "medium.com": 0.7,
        "docs.python.org": 0.95,
        "docs.ruby-lang.org": 0.95,
        "developer.mozilla.org": 0.95,
        # Government/Academic
        ".edu": 0.9,
        ".gov": 0.95,
        ".org": 0.7,
        # Lower credibility
        ".com": 0.5,
        ".io": 0.5,
    }

    @classmethod
    def score_url(cls, url: str) -> float:
        """Score a URL based on domain credibility.

        Args:
            url: The URL to score.

        Returns:
            Credibility score between 0.0 and 1.0. Known high-trust domains
            (e.g. arxiv.org, .gov, .edu) score higher; defaults to 0.5.
        """
        parsed = urlsplit(url if "://" in url else f"//{url}")
        host = (parsed.hostname or "").lower().rstrip(".")

        # Check exact matches first
        for domain, score in cls.DOMAIN_SCORES.items():
            if domain.startswith("."):
                if domain in {".edu", ".gov"} and host.endswith(domain):
                    return score
            elif host == domain or host.endswith(f".{domain}"):
                return score

        return 0.5  # Default score
