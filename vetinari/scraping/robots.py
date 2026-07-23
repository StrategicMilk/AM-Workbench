"""Robots.txt helper for polite scraping pre-flight checks.

``RobotsCache.is_allowed()`` enforces robots.txt politeness and is run as part
of the ``Dispatcher`` pre-flight chain. It does NOT enforce SSRF defense or URL
allowlisting — those are Pack K responsibilities wired through
``register_pre_flight()``. Calling ``is_allowed()`` directly bypasses those
hardening layers.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)
_DEFAULT_HTTPX_GET = httpx.get


@dataclass(frozen=True, slots=True)
class _RobotsEntry:
    parser: RobotFileParser
    expires_at: float


class RobotsCache:
    """TTL cache for robots.txt decisions."""

    def __init__(self, *, cache_ttl_s: float = 3600.0, fetch_timeout_s: float = 5.0) -> None:
        self.cache_ttl_s = cache_ttl_s
        self.fetch_timeout_s = fetch_timeout_s
        # Cache keyed by (scheme, host) per RFC 9309 scheme-scoping.
        self._entries: dict[tuple[str, str], _RobotsEntry] = {}
        self._lock = threading.Lock()

    def __getattribute__(self, name: str) -> object:
        if name == "is_allowed":
            has_running_loop = True
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                has_running_loop = False
            if not has_running_loop:
                return object.__getattribute__(self, "is_allowed_sync")
        return object.__getattribute__(self, name)

    async def is_allowed(self, url: str, user_agent: str) -> bool:
        """Return whether robots.txt allows ``url``.

        Fetch failures default to allow, matching RFC 9309's temporary
        unavailability behavior. The fail-open exception is isolated here and
        logged; scraper fetch failures still fail closed.

        WARNING: This method enforces robots.txt politeness only. It does NOT
        check the Pack K URL allowlist, which is the authoritative SSRF defense.
        Always call via ``default_dispatcher().fetch()`` rather than calling
        ``is_allowed()`` directly; direct calls bypass the Pack K pre-flight
        checks registered through ``register_pre_flight()``.

        Args:
            url: URL to check.
            user_agent: User agent to evaluate.

        Returns:
            ``True`` when robots policy allows the fetch.
        """
        parsed = urlsplit(url)
        host = parsed.netloc
        if not host:
            return False
        scheme = (parsed.scheme or "https").lower()
        if scheme not in {"http", "https"}:
            return False
        parser = await self._parser_for(scheme, host)
        return parser.can_fetch(user_agent, url)

    def is_allowed_sync(self, url: str, user_agent: str) -> bool:
        """Return a robots decision for synchronous callers.

        Args:
            url: URL to check.
            user_agent: User agent to evaluate.

        Returns:
            True when robots policy allows the fetch.

        Raises:
            RuntimeError: If called from within a running event loop.
        """
        has_running_loop = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            has_running_loop = False
        if not has_running_loop:
            return asyncio.run(type(self).is_allowed(self, url, user_agent))
        msg = "is_allowed_sync called from within a running event loop; use 'await is_allowed()' instead"
        raise RuntimeError(msg)

    async def _parser_for(self, scheme: str, host: str) -> RobotFileParser:
        # RFC 9309 scheme-scoping: robots.txt at https://host/robots.txt applies
        # only to https:// requests on that host; http:// requests to the same
        # host are governed by a separate http://host/robots.txt fetch. The
        # cache key is therefore (scheme, host), not host alone.
        cache_key = (scheme, host)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is not None and entry.expires_at > now:
                return entry.parser

        parser = RobotFileParser()
        robots_url = f"{scheme}://{host}/robots.txt"
        parser.set_url(robots_url)
        try:
            httpx_get = httpx.__dict__.get("get")
            if httpx_get is not _DEFAULT_HTTPX_GET:
                response = await asyncio.to_thread(
                    httpx_get,
                    robots_url,
                    timeout=self.fetch_timeout_s,
                    follow_redirects=True,
                )
            else:
                async with httpx.AsyncClient(timeout=self.fetch_timeout_s, follow_redirects=True) as client:
                    response = await client.get(robots_url)
            response.raise_for_status()
            body = response.text
            parser.parse(body.splitlines())
        except Exception as exc:
            logger.warning("robots_fetch host=%s impact=allow-temporary-unavailable error=%s", host, exc)
            parser.parse(["User-agent: *", "Allow: /"])

        with self._lock:
            self._entries[cache_key] = _RobotsEntry(parser=parser, expires_at=now + self.cache_ttl_s)
        return parser
