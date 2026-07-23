"""Dynamic browser-backed scraper for JS-rendered pages.

This optional backend renders pages with Playwright and then uses the same
HTML extraction path as ``StaticBackend``. Browser binaries are NOT installed
by ``pip install``. Run ``python -m playwright install chromium`` after
installing the ``scraping-dynamic`` extra.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from vetinari.scraping._hardening_config import load_hardening_config
from vetinari.scraping.contracts import ScrapeFailureReason, ScrapeRequest, ScraperResult
from vetinari.scraping.static_backend import extract_html
from vetinari.scraping.url_validator import URLValidator, redact_url_for_log

logger = logging.getLogger(__name__)


try:
    from playwright.sync_api import Error as _PlaywrightError
    from playwright.sync_api import TimeoutError as _PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright as _sync_playwright
except ImportError:
    PlaywrightError: type[Exception] = Exception
    PlaywrightTimeoutError: type[Exception] = TimeoutError
    sync_playwright: Any | None = None
else:
    PlaywrightError = _PlaywrightError
    PlaywrightTimeoutError = _PlaywrightTimeoutError
    sync_playwright = _sync_playwright


def _failure(
    req: ScrapeRequest,
    reason: ScrapeFailureReason,
    *,
    http_status: int | None = None,
    error_detail: str | None = None,
    mime: str | None = None,
) -> ScraperResult:
    return ScraperResult(
        passed=False,
        reason=reason,
        url=req.url,
        final_url=None,
        http_status=http_status,
        mime=mime,
        title=None,
        text=None,
        extracted_chars=0,
        fetched_at_utc=datetime.now(timezone.utc),
        backend="dynamic",
        error_detail=error_detail,
    )


class DynamicBackend:
    """Render JS-heavy pages with Playwright before text extraction."""

    backend_name = "dynamic"

    def __init__(
        self,
        *,
        hydration_extra_ms: int = 500,
        url_validator: URLValidator | None = None,
    ) -> None:
        self.hydration_extra_ms = hydration_extra_ms
        # The validator is instantiated once per backend so the dependency is
        # visible and config drift doesn't change behaviour mid-fetch. Tests
        # may inject a custom validator; production callers rely on the
        # default that reads ``config/scraping_hardening.yaml``.
        self._url_validator = url_validator or URLValidator.from_config(load_hardening_config().url)
        self._lock = threading.Lock()
        self._playwright: Any | None = None
        self._browser: Any | None = None

    def __enter__(self) -> DynamicBackend:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the cached browser and Playwright runtime."""
        with self._lock:
            browser = self._browser
            playwright = self._playwright
            self._browser = None
            self._playwright = None
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()

    def _browser_instance(self) -> Any:
        playwright_factory = sync_playwright
        if playwright_factory is None:
            msg = "playwright is not installed"
            raise RuntimeError(msg)
        if self._browser is None:
            with self._lock:
                if self._browser is None:
                    self._playwright = playwright_factory().start()
                    self._browser = self._playwright.chromium.launch(headless=True)
        return self._browser

    def fetch(self, req: ScrapeRequest) -> ScraperResult:
        """Fetch a JS-rendered page and return a typed scrape result.

        Args:
            req: Request describing URL, timeout, and limits.

        Returns:
            Typed scraper result for the dynamic backend.
        """
        try:
            browser = self._browser_instance()
        except Exception as exc:
            logger.warning("scrape_dynamic url=%s impact=browser-unavailable", redact_url_for_log(req.url))
            return _failure(req, ScrapeFailureReason.BACKEND_UNAVAILABLE, error_detail=str(exc))

        context = None
        page = None
        try:
            context = browser.new_context(
                user_agent=req.user_agent,
                extra_http_headers={"Accept-Language": req.accept_language},
                service_workers="block",
            )
            page = context.new_page()
            validator = self._url_validator

            def _block_unsafe(route: Any) -> None:
                # Playwright fires `route` events for every navigation leg
                # AND every subrequest (image, script, fetch, redirect). We
                # re-validate every URL because the top-level goto() preflight
                # can't cover redirects or in-page subrequests. ``max_redirects=0``
                # blocks redirect chains within a single subrequest, and the
                # 3xx guard below catches navigation legs that landed on a
                # redirect Playwright would otherwise auto-follow.
                request = route.request
                if validator.check(ScrapeRequest(url=request.url, timeout_s=req.timeout_s, max_bytes=req.max_bytes)):
                    route.abort()
                    return
                response = route.fetch(max_redirects=0)
                if 300 <= response.status < 400:
                    route.abort()
                    return
                route.fulfill(response=response)

            page.route("**/*", _block_unsafe)
            response = page.goto(req.url, wait_until="domcontentloaded", timeout=req.timeout_s * 1000)
            status = response.status if response is not None else None
            if status is not None and 400 <= status < 500:
                return _failure(req, ScrapeFailureReason.HTTP_4XX, http_status=status, mime="text/html")
            if status is not None and status >= 500:
                return _failure(req, ScrapeFailureReason.HTTP_5XX, http_status=status, mime="text/html")
            page.wait_for_load_state("networkidle", timeout=req.timeout_s * 1000)
            if self.hydration_extra_ms > 0:
                page.wait_for_timeout(self.hydration_extra_ms)
            html = page.content()
        except PlaywrightTimeoutError as exc:
            logger.warning("scrape_dynamic url=%s impact=timeout", redact_url_for_log(req.url))
            return _failure(req, ScrapeFailureReason.TIMEOUT, error_detail=str(exc))
        except PlaywrightError as exc:
            logger.warning("scrape_dynamic url=%s impact=network-error", redact_url_for_log(req.url))
            detail = str(exc)
            if "Executable" in detail and "exist" in detail:
                return _failure(req, ScrapeFailureReason.BACKEND_UNAVAILABLE, error_detail=detail)
            return _failure(req, ScrapeFailureReason.NETWORK_ERROR, error_detail=detail)
        finally:
            if page is not None:
                page.close()
            if context is not None:
                context.close()

        return extract_html(
            req,
            html,
            final_url=req.url,
            http_status=status,
            mime="text/html",
            backend=self.backend_name,
        )
