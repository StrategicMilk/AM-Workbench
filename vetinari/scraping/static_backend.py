"""Static HTML scraping backend for the self-hosted scraping pipeline.

``StaticBackend`` is the fast default path. It fetches HTML with ``httpx`` and
extracts article text with ``trafilatura`` when the optional ``scraping`` extra
is installed. Every network, MIME, and extraction failure returns a typed
``ScraperResult`` with ``passed=False``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from importlib import import_module
from typing import Any
from urllib.parse import urlsplit

import httpx

from vetinari.scraping.contracts import ScrapeFailureReason, ScrapeRequest, ScraperResult
from vetinari.scraping.url_validator import URLValidator, redact_url_for_log

logger = logging.getLogger(__name__)


try:
    trafilatura: Any | None = import_module("trafilatura")
except ImportError:
    trafilatura = None


DEFAULT_UA = "Vetinari-Scraper/1.0 (+https://github.com/StrategicMilk/AM-Workbench; polite-by-default)"
MIN_EXTRACTED_CHARS = 200
PAYWALL_TERMS = ("subscribe", "sign in to read", "this article is for subscribers")


class _TextExtractor(HTMLParser):
    """Small HTML text fallback used only for deterministic classification."""

    def __init__(self) -> None:
        super().__init__()
        self._in_ignored = False
        self._in_title = False
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Handle HTML start tags during parser callbacks.

        Args:
            tag: Start-tag name.
            attrs: Raw tag attributes from ``HTMLParser``.
        """
        if tag in {"script", "style", "noscript"}:
            self._in_ignored = True
        if tag == "title" or tag == "h1":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        """Handle HTML end tags during parser callbacks."""
        if tag in {"script", "style", "noscript"}:
            self._in_ignored = False
        if tag == "title" or tag == "h1":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        """Collect visible text during parser callbacks."""
        text = " ".join(data.split())
        if not text or self._in_ignored:
            return
        self.body_parts.append(text)
        if self._in_title:
            self.title_parts.append(text)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _html_text(html: str) -> tuple[str, str | None]:
    parser = _TextExtractor()
    parser.feed(html)
    text = " ".join(parser.body_parts).strip()
    title = " ".join(parser.title_parts).strip() or None
    return text, title


def _script_count(html: str) -> int:
    return len(re.findall(r"<script\b", html, flags=re.IGNORECASE))


def _looks_like_html(mime: str) -> bool:
    base = mime.split(";", 1)[0].strip().lower()
    return base in {"text/html", "application/xhtml+xml"} or base.endswith("+html")


def _result(
    req: ScrapeRequest,
    *,
    reason: ScrapeFailureReason,
    backend: str,
    final_url: str | None = None,
    http_status: int | None = None,
    mime: str | None = None,
    title: str | None = None,
    text: str | None = None,
    error_detail: str | None = None,
) -> ScraperResult:
    passed = reason is ScrapeFailureReason.OK
    extracted_chars = len(text) if text is not None else 0
    return ScraperResult(
        passed=passed,
        reason=reason,
        url=req.url,
        final_url=final_url,
        http_status=http_status,
        mime=mime,
        title=title,
        text=text,
        extracted_chars=extracted_chars,
        fetched_at_utc=_utcnow(),
        backend=backend,
        error_detail=error_detail,
    )


def extract_html(
    req: ScrapeRequest,
    html: str,
    *,
    final_url: str | None,
    http_status: int | None,
    mime: str | None,
    backend: str,
) -> ScraperResult:
    """Extract text from HTML and classify empty or low-quality outcomes.

    Args:
        req: Original scrape request.
        html: HTML document to extract.
        final_url: Final URL after redirects, if known.
        http_status: HTTP status from the fetch path, if known.
        mime: Response MIME type, if known.
        backend: Backend name to stamp on the result.

    Returns:
        Typed scraper result for the extraction outcome.
    """
    if trafilatura is None:
        logger.warning(
            "scrape_extract backend=%s url=%s impact=optional dependency unavailable",
            backend,
            redact_url_for_log(req.url),
        )
        return _result(
            req,
            reason=ScrapeFailureReason.BACKEND_UNAVAILABLE,
            backend=backend,
            error_detail="trafilatura is not installed",
        )
    body_text, fallback_title = _html_text(html)
    if not html.strip() or not body_text:
        reason = _empty_html_reason(html, body_text)
        return _result(req, reason=reason, backend=backend, final_url=final_url, http_status=http_status, mime=mime)
    extracted = trafilatura.extract(
        html,
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    text = (extracted or "").strip()
    if not text and len(body_text) > MIN_EXTRACTED_CHARS:
        text = body_text
    title = _metadata_title(html, fallback_title)
    if not text:
        reason = _empty_html_reason(html, body_text)
        return _result(
            req, reason=reason, backend=backend, final_url=final_url, http_status=http_status, mime=mime, title=title
        )
    if _script_count(html) > 5 and len(text) < 300:
        failure_reason = ScrapeFailureReason.NEEDS_JS
    else:
        failure_reason = _quality_failure_reason(text)
    if failure_reason is not None:
        return _result(
            req,
            reason=failure_reason,
            backend=backend,
            final_url=final_url,
            http_status=http_status,
            mime=mime,
            title=title,
            text=text,
        )
    return _result(
        req,
        reason=ScrapeFailureReason.OK,
        backend=backend,
        final_url=final_url,
        http_status=http_status,
        mime=mime,
        title=title,
        text=text,
    )


def _empty_html_reason(html: str, body_text: str) -> ScrapeFailureReason:
    if _script_count(html) > 5 and len(body_text) < 300:
        return ScrapeFailureReason.NEEDS_JS
    return ScrapeFailureReason.MIME_HTML_BUT_EMPTY


def _metadata_title(html: str, fallback_title: str) -> str:
    with_metadata = getattr(trafilatura, "extract_metadata", None)
    if with_metadata is None:
        return fallback_title
    metadata = with_metadata(html)
    return getattr(metadata, "title", None) or fallback_title


def _quality_failure_reason(text: str) -> ScrapeFailureReason | None:
    paywall_hits = sum(1 for term in PAYWALL_TERMS if term in text.lower())
    if paywall_hits >= 2 and len(text) < 800:
        return ScrapeFailureReason.PAYWALL_DETECTED
    if len(text) <= MIN_EXTRACTED_CHARS:
        return ScrapeFailureReason.EXTRACTION_TOO_SHORT
    return None


class StaticBackend:
    """Fetch and extract static HTML pages.

    INTERNAL — call via ``default_dispatcher().fetch()`` for production use.
    Direct callers bypass robots.txt enforcement, host rate-limiting, and any
    pre-flight checks registered by Pack K (URL allowlist / SSRF defense).
    Direct use is only appropriate inside ``Dispatcher._fetch_once()`` and in
    isolated unit tests that supply their own safety layer.
    """

    backend_name = "static"

    def __init__(self, url_validator: URLValidator | None = None) -> None:
        self._url_validator = url_validator

    def fetch(self, req: ScrapeRequest) -> ScraperResult:
        """Fetch ``req.url`` and return a typed scrape result.

        Args:
            req: Request describing URL, timeout, and limits.

        Returns:
            Typed scraper result for the static backend.
        """
        if trafilatura is None:
            logger.warning("scrape_static url=%s impact=optional dependency unavailable", redact_url_for_log(req.url))
            return _result(
                req,
                reason=ScrapeFailureReason.BACKEND_UNAVAILABLE,
                backend=self.backend_name,
                error_detail="trafilatura is not installed",
            )
        headers = {
            "User-Agent": req.user_agent or DEFAULT_UA,
            "Accept-Language": req.accept_language,
        }
        try:
            with (
                httpx.Client(timeout=req.timeout_s, follow_redirects=req.follow_redirects, headers=headers) as client,
                client.stream("GET", req.url) as response,
            ):
                status = response.status_code
                final_url = str(response.url)
                mime = response.headers.get("content-type", "")
                failure = self._response_failure(req, status, final_url, mime)
                if failure is not None:
                    return failure
                chunks, oversize = self._read_chunks(response, req.max_bytes)
                if oversize:
                    return _result(
                        req,
                        reason=ScrapeFailureReason.OVERSIZE_BODY,
                        backend=self.backend_name,
                        final_url=final_url,
                        http_status=status,
                        mime=mime,
                    )
                html = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
        except httpx.TimeoutException as exc:
            logger.warning("scrape_static url=%s impact=timeout", redact_url_for_log(req.url))
            return _result(req, reason=ScrapeFailureReason.TIMEOUT, backend=self.backend_name, error_detail=str(exc))
        except httpx.HTTPError as exc:
            logger.warning("scrape_static url=%s impact=network-error", redact_url_for_log(req.url))
            return _result(
                req, reason=ScrapeFailureReason.NETWORK_ERROR, backend=self.backend_name, error_detail=str(exc)
            )
        result = extract_html(req, html, final_url=final_url, http_status=status, mime=mime, backend=self.backend_name)
        if (
            result.reason in {ScrapeFailureReason.MIME_HTML_BUT_EMPTY, ScrapeFailureReason.NEEDS_JS}
            and not urlsplit(req.url).scheme
        ):
            return replace(result, error_detail="invalid URL")
        return result

    def _response_failure(
        self,
        req: ScrapeRequest,
        status: int,
        final_url: str,
        mime: str,
    ) -> ScraperResult | None:
        if 300 <= status < 400:
            if self._url_validator is not None:
                block_result = self._url_validator.check(ScrapeRequest(url=final_url))
                if block_result is None:
                    return None
                return block_result
            return _result(
                req,
                reason=ScrapeFailureReason.URL_BLOCKED,
                backend=self.backend_name,
                final_url=final_url,
                http_status=status,
                mime=mime,
                error_detail="redirect target requires dispatcher validation",
            )
        if 400 <= status < 500:
            return _result(
                req,
                reason=ScrapeFailureReason.HTTP_4XX,
                backend=self.backend_name,
                final_url=final_url,
                http_status=status,
                mime=mime,
            )
        if status >= 500:
            return _result(
                req,
                reason=ScrapeFailureReason.HTTP_5XX,
                backend=self.backend_name,
                final_url=final_url,
                http_status=status,
                mime=mime,
            )
        if not _looks_like_html(mime):
            return _result(
                req,
                reason=ScrapeFailureReason.MIME_NOT_HTML,
                backend=self.backend_name,
                final_url=final_url,
                http_status=status,
                mime=mime,
            )
        return None

    @staticmethod
    def _read_chunks(response: Any, max_bytes: int) -> tuple[list[bytes], bool]:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                return chunks, True
            chunks.append(chunk)
        return chunks, False
