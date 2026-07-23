"""Telemetry event hook for completed scraper dispatcher calls.

``emit_completion`` publishes a ``ScrapingFetchCompleted`` event to the
process-local ``vetinari.events`` bus. The bus has an Event-wide observability
subscriber, and focused tests subscribe a concrete consumer to the same event
class to prove the publish-to-consume path is live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from vetinari.clock import Clock, SystemClock
from vetinari.events import Event, get_event_bus
from vetinari.scraping.contracts import ScrapeRequest, ScraperResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScrapingFetchPayload:
    """Typed payload for ``scraping.fetch.completed``."""

    url_host: str
    backend_chain: tuple[str, ...]
    attempts: int
    cached: bool
    passed: bool
    reason: str
    http_status: int | None
    extracted_chars: int
    latency_ms: float
    fetched_at_utc: str

    def __repr__(self) -> str:
        return (
            "ScrapingFetchPayload("
            f"url_host={self.url_host!r}, attempts={self.attempts!r}, "
            f"cached={self.cached!r}, passed={self.passed!r}, reason={self.reason!r})"
        )


@dataclass(frozen=True, slots=True)
class ScrapingFetchCompleted(Event):
    """EventBus wrapper for scraper completion telemetry."""

    payload: ScrapingFetchPayload

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_type", "scraping.fetch.completed")


def emit_completion(
    req: ScrapeRequest,
    result: ScraperResult,
    *,
    started_at_monotonic: float,
    clock: Clock | None = None,
) -> None:
    """Publish one structured completion event for a dispatcher call.

    Args:
        req: Scrape request being completed.
        result: Final scrape result.
        started_at_monotonic: Monotonic timestamp captured before dispatch.
        clock: Optional clock used to make telemetry timestamps deterministic.
    """
    active_clock = clock or SystemClock()
    fetched_at = active_clock.utc_now()
    latency_ms = max(0.0, (active_clock.monotonic() - started_at_monotonic) * 1000.0)
    payload = ScrapingFetchPayload(
        url_host=urlsplit(req.url).hostname or "",
        backend_chain=result.backend_chain or (result.backend,),
        attempts=result.attempts,
        cached=result.cached,
        passed=result.passed,
        reason=result.reason.value,
        http_status=result.http_status,
        extracted_chars=result.extracted_chars,
        latency_ms=latency_ms,
        fetched_at_utc=fetched_at.isoformat(),
    )
    try:
        get_event_bus().publish_async(
            ScrapingFetchCompleted(
                event_type="scraping.fetch.completed",
                timestamp=datetime.fromisoformat(payload.fetched_at_utc).timestamp(),
                payload=payload,
            )
        )
    except Exception as exc:
        logger.warning("scraping telemetry publish failed: %s", exc)
