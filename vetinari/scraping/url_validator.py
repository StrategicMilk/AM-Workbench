"""SSRF and URL allowlist pre-flight check for the scraping dispatcher.

This module implements ADR-0106. ``URLValidator.check`` is registered as a
dispatcher pre-flight hook by ``vetinari.scraping._register_hardening`` so
unsafe URLs are rejected before robots.txt, rate limiting, or backend fetches
can turn arbitrary user input into an SSRF primitive.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from vetinari.scraping.contracts import ScrapeFailureReason, ScrapeRequest, ScraperResult

logger = logging.getLogger(__name__)


# Side effects: DNS resolution results are cached here. The only reader and
# writer is ``_resolve_cached``; ``_DNS_CACHE_LOCK`` guards insertions and TTL
# refreshes. No DNS lookup happens at module import time.
_DNS_CACHE: dict[tuple[str, int | None], tuple[float, tuple[str, ...]]] = {}
_DNS_CACHE_LOCK = threading.Lock()


def reset_dns_cache_for_tests() -> None:
    """Clear cached DNS answers through the same lock used by validation."""
    with _DNS_CACHE_LOCK:
        _DNS_CACHE.clear()


@dataclass(frozen=True, slots=True)
class URLValidatorConfig:
    """Configuration for ``URLValidator``."""

    schemes: tuple[str, ...] = ("http", "https")
    host_allowlist: tuple[str, ...] = ()
    host_denylist: tuple[str, ...] = ()
    dns_cache_ttl_s: float = 60.0

    def __repr__(self) -> str:
        return (
            "URLValidatorConfig("
            f"schemes={self.schemes!r}, allowlist={len(self.host_allowlist)}, "
            f"denylist={len(self.host_denylist)}, dns_cache_ttl_s={self.dns_cache_ttl_s!r})"
        )


class URLValidator:
    """Fail-closed SSRF guard for scraper requests."""

    def __init__(
        self,
        *,
        schemes: tuple[str, ...] = ("http", "https"),
        host_allowlist: tuple[str, ...] = (),
        host_denylist: tuple[str, ...] = (),
        dns_cache_ttl_s: float = 60.0,
    ) -> None:
        self.schemes = tuple(s.lower() for s in schemes)
        self.host_allowlist = tuple(h.lower() for h in host_allowlist)
        self.host_denylist = tuple(h.lower() for h in host_denylist)
        self.dns_cache_ttl_s = float(dns_cache_ttl_s)

    @classmethod
    def from_config(cls, config: URLValidatorConfig) -> URLValidator:
        """Build a validator from typed hardening config."""
        return cls(
            schemes=config.schemes,
            host_allowlist=config.host_allowlist,
            host_denylist=config.host_denylist,
            dns_cache_ttl_s=config.dns_cache_ttl_s,
        )

    def check(self, req: ScrapeRequest) -> ScraperResult | None:
        """Return a blocking result for unsafe URLs, otherwise ``None``.

        Returns:
            Blocking ScraperResult for unsafe URLs, or ``None`` when allowed.
        """
        try:
            return self._check(req)
        except Exception as exc:
            host = _safe_host(req.url)
            logger.warning("url_validator host=%s impact=blocked rule=exception error=%s", host, exc)
            return _blocked(req, "url validation exception")

    def _check(self, req: ScrapeRequest) -> ScraperResult | None:
        if not req.url:
            return _blocked(req, "empty URL")

        parsed = urlsplit(req.url)
        scheme = parsed.scheme.lower()
        if scheme not in self.schemes:
            return _blocked(req, f"scheme not allowed: {scheme or '<missing>'}")

        host = parsed.hostname
        if not host:
            return _blocked(req, "empty host")
        normalized_host = host.lower()

        if self._matches_host(normalized_host, self.host_denylist):
            return _blocked(req, f"host denylist match: {normalized_host}")
        allowlist_match = self._matches_host(normalized_host, self.host_allowlist)
        if self.host_allowlist and not allowlist_match:
            return _blocked(req, f"host allowlist miss: {normalized_host}")

        port = parsed.port
        # The DNS cache TTL defines the rebinding window: between validation
        # and the actual fetch, a malicious authoritative server may flip the
        # answer from public to private. Lowering this below ~10s makes the
        # cache useless under burst traffic; raising it widens the window.
        resolved = _resolve_cached(normalized_host, port, ttl_s=self.dns_cache_ttl_s)
        for ip_text in resolved:
            if _is_blocked_ip(ip_text, allow_non_global=allowlist_match):
                return _blocked(req, f"private/loopback/link-local IP or non-global IP: {ip_text}")
        return None

    @staticmethod
    def _matches_host(host: str, patterns: tuple[str, ...]) -> bool:
        # Pattern semantics:
        #   "evil.com"   matches "evil.com" AND any subdomain ("sub.evil.com").
        #   "*.evil.com" matches any subdomain only (NOT "evil.com" apex).
        #   ".evil.com"  matches any subdomain only (legacy form, same as "*.").
        # Bare hostnames imply subdomains by default — operators expect
        # blocking the apex to also block subdomains.
        for pattern in patterns:
            if not pattern:
                continue
            if pattern.startswith("*."):
                suffix = pattern[1:]
                if host.endswith(suffix):
                    return True
            elif pattern.startswith("."):
                if host.endswith(pattern):
                    return True
            elif host == pattern or host.endswith("." + pattern):
                return True
        return False


def _resolve_cached(host: str, port: int | None, *, ttl_s: float) -> tuple[str, ...]:
    now = time.monotonic()
    cache_key = (host, port)
    cached = _DNS_CACHE.get(cache_key)
    if cached is not None and now - cached[0] < ttl_s:
        return cached[1]

    with _DNS_CACHE_LOCK:
        cached = _DNS_CACHE.get(cache_key)
        if cached is not None and now - cached[0] < ttl_s:
            return cached[1]
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception as exc:
            logger.warning("url_validator host=%s impact=blocked rule=dns-resolution error=%s", host, exc)
            raise ValueError("dns resolution failed") from exc
        addresses = tuple(sorted({str(info[4][0]) for info in infos}))
        if not addresses:
            logger.warning("url_validator host=%s impact=blocked rule=dns-empty", host)
            raise ValueError("dns resolution failed")
        _DNS_CACHE[cache_key] = (now, addresses)
        return addresses


def _is_blocked_ip(ip_text: str, *, allow_non_global: bool = False) -> bool:
    address = ipaddress.ip_address(ip_text)
    if not allow_non_global and not address.is_global:
        return True
    return any((
        address.is_private,
        address.is_loopback,
        address.is_link_local,
        address.is_multicast,
        address.is_unspecified,
        address.is_reserved,
    ))


def _safe_host(url: str) -> str:
    try:
        return urlsplit(url).hostname or "<empty>"
    except Exception as exc:
        logger.warning("Could not parse host from URL for logging: %s", exc)
        return "<unparseable>"


def redact_url_for_log(url: str) -> str:
    """Return URL origin only, dropping userinfo, path, query, and fragment.

    Returns:
        Redacted URL origin string, or ``<unparseable>``.
    """
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "<empty>"
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, "", "", ""))
    except Exception as exc:
        logger.warning("Could not redact URL for logging: %s", exc)
        return "<unparseable>"


def _blocked(req: ScrapeRequest, detail: str) -> ScraperResult:
    host = _safe_host(req.url)
    logger.warning("url_validator host=%s impact=blocked rule=%s", host, detail)
    return ScraperResult(
        passed=False,
        reason=ScrapeFailureReason.URL_BLOCKED,
        url=redact_url_for_log(req.url),
        final_url=None,
        http_status=None,
        mime=None,
        title=None,
        text=None,
        extracted_chars=0,
        fetched_at_utc=datetime.now(timezone.utc),
        backend="url_validator",
        error_detail=detail,
        backend_chain=("url_validator",),
        cached=False,
        attempts=1,
    )
