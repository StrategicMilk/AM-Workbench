"""Typed contracts for Vetinari's self-hosted scraping pipeline.

The static backend, dynamic backend, dispatcher, and later hardening plug-ins
communicate through these value objects. Failures are explicit
``ScraperResult`` instances so callers never interpret an empty string as a
successful scrape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from vetinari.privacy import privacy_receipt


class ScrapeFailureReason(str, Enum):
    """Typed outcome reasons for scraper success and failure paths."""

    OK = "ok"
    NETWORK_ERROR = "network_error"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"
    TIMEOUT = "timeout"
    MIME_NOT_HTML = "mime_not_html"
    EXTRACTION_EMPTY = "extraction_empty"
    EXTRACTION_TOO_SHORT = "extraction_too_short"
    MIME_HTML_BUT_EMPTY = "mime_html_but_empty"
    NEEDS_JS = "needs_js"
    PAYWALL_DETECTED = "paywall_detected"
    ROBOTS_DENIED = "robots_denied"
    RATE_LIMITED = "rate_limited"
    OVERSIZE_BODY = "oversize_body"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    # Reserved for Pack K hardening (URL allowlist, retry policy, cache).
    URL_BLOCKED = "url_blocked"
    RETRY_EXHAUSTED = "retry_exhausted"
    CACHE_DISABLED = "cache_disabled"
    # Reserved for Pack L call-site input validation (timeout out of range,
    # missing required parameter, scheme rejection at the route boundary).
    INVALID_REQUEST = "invalid_request"


class CachePolicy(str, Enum):
    """Cache policy values; str inheritance preserves existing comparisons."""

    DEFAULT = "default"
    BYPASS = "bypass"
    NO_STORE = "no_store"


@dataclass(frozen=True, slots=True)
class ScrapeRequest:
    """Input contract for scraper backends and dispatchers."""

    url: str
    timeout_s: float = 15.0
    max_bytes: int = 5_000_000
    user_agent: str | None = None
    accept_language: str = "en"
    cache_policy: CachePolicy = CachePolicy.DEFAULT
    privacy_class: str = "public"
    subject_id: str | None = None
    retention_days: int = 7
    follow_redirects: bool = False

    def __post_init__(self) -> None:
        """Validate privacy metadata before a request reaches a backend."""
        if not isinstance(self.cache_policy, CachePolicy):
            try:
                object.__setattr__(self, "cache_policy", CachePolicy(str(self.cache_policy)))
            except ValueError as exc:
                allowed = ", ".join(policy.value for policy in CachePolicy)
                msg = f"cache_policy must be one of: {allowed}"
                raise ValueError(msg) from exc
        privacy_receipt(
            privacy_class=self.privacy_class,
            subject_id=self.subject_id,
            retention_days=self.retention_days,
            source="scraping.request",
            redaction_applied=True,
        )

    def __repr__(self) -> str:
        return f"ScrapeRequest(url={self.url!r}, timeout_s={self.timeout_s!r}, max_bytes={self.max_bytes!r})"


@dataclass(frozen=True, slots=True)
class ScraperResult:
    """Result contract shared by every scraper backend."""

    passed: bool
    reason: ScrapeFailureReason
    url: str
    final_url: str | None
    http_status: int | None
    mime: str | None
    title: str | None
    text: str | None
    extracted_chars: int
    fetched_at_utc: datetime
    backend: str
    error_detail: str | None
    backend_chain: tuple[str, ...] = ()
    cached: bool = False
    attempts: int = 1

    def __post_init__(self) -> None:
        """Enforce success and failure invariants at construction time."""
        if self.fetched_at_utc.tzinfo is None or self.fetched_at_utc.utcoffset() is None:
            msg = "fetched_at_utc must be timezone-aware UTC"
            raise ValueError(msg)
        if self.fetched_at_utc.utcoffset() != timezone.utc.utcoffset(self.fetched_at_utc):
            msg = "fetched_at_utc must use UTC offset"
            raise ValueError(msg)
        reason_value = self.reason.value if isinstance(self.reason, Enum) else str(self.reason)
        if self.passed:
            if reason_value != ScrapeFailureReason.OK.value:
                msg = "passed=True requires reason=OK"
                raise ValueError(msg)
            if self.text is None or self.extracted_chars <= 0:
                msg = "passed=True requires non-empty text and extracted_chars > 0"
                raise ValueError(msg)
        elif reason_value == ScrapeFailureReason.OK.value:
            msg = "passed=False cannot use reason=OK"
            raise ValueError(msg)

    def __repr__(self) -> str:
        return (
            "ScraperResult("
            f"passed={self.passed!r}, reason={self.reason.value!r}, url={self.url!r}, "
            f"backend={self.backend!r}, extracted_chars={self.extracted_chars!r})"
        )


class ScraperCacheProtocol(Protocol):
    """Cache extension point reserved for Pack K hardening."""

    def get(self, req: ScrapeRequest) -> ScraperResult | None:
        """Return a cached scrape result when available.

        Args:
            req: Scrape request key.

        Returns:
            Cached result when present, otherwise ``None``.
        """

    def put(self, req: ScrapeRequest, result: ScraperResult) -> None:
        """Persist a successful scrape result.

        Args:
            req: Scrape request key.
            result: Successful scrape result to store.
        """


class RetryPolicyProtocol(Protocol):
    """Retry extension point reserved for Pack K hardening."""

    def should_retry(self, result: ScraperResult, attempt: int) -> bool:
        """Return whether another scrape attempt should be made.

        Args:
            result: Latest scrape result.
            attempt: One-based attempt number.

        Returns:
            ``True`` when the dispatcher should try again.
        """

    def delay_s(self, attempt: int) -> float:
        """Return the delay before the next attempt.

        Args:
            attempt: One-based next attempt number.

        Returns:
            Delay in seconds.
        """
