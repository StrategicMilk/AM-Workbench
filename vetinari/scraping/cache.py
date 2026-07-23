"""Filesystem TTL cache for scraper results.

ADR-0107 chooses a directory-sharded filesystem cache for the current
single-process scraper deployment. Cache failures are non-fatal: malformed,
expired, or unreadable entries are logged and treated as misses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from vetinari.learning.atomic_writers import write_json_atomic
from vetinari.privacy import PRIVACY_ENVELOPE_KEY, require_privacy_envelope, wrap_for_persistence
from vetinari.scraping.contracts import CachePolicy, ScrapeFailureReason, ScrapeRequest, ScraperResult
from vetinari.security.fail_closed import assert_closed_schema, sanitize_untrusted_text

logger = logging.getLogger(__name__)
_MAX_CACHED_TEXT_CHARS = 2048


class FilesystemCache:
    """Directory-sharded JSON cache implementing ``ScraperCacheProtocol``."""

    def __init__(self, cache_dir: Path, ttl_s: float = 86400.0, max_value_bytes: int = 5_000_000) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl_s = float(ttl_s)
        self.max_value_bytes = int(max_value_bytes)
        # Side effects: lock registry is populated lazily by ``_get_lock_for_key``.
        # That method is both the only reader and the only writer of the dict.
        self._per_key_locks: dict[str, threading.Lock] = {}
        self._lock_registry_lock = threading.Lock()

    def get(self, req: ScrapeRequest) -> ScraperResult | None:
        """Return a cached result or ``None`` on miss.

        Returns:
            Cached scraper result, or ``None`` when no usable entry exists.
        """
        if req.cache_policy is CachePolicy.BYPASS:
            return None
        path = self._path_for_key(self.key_for(req))
        if not path.exists():
            return None
        try:
            if time.time() - os.path.getmtime(path) > self.ttl_s:
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            result = _result_from_json(req, data)
        except Exception as exc:
            logger.warning("scraping_cache path=%s impact=miss rule=read-error error=%s", path, exc)
            return None
        return _with_cached(result)

    def put(self, req: ScrapeRequest, result: ScraperResult) -> None:
        """Store a successful result unless the request opts out.

        Args:
            req: Original scrape request.
            result: Successful scrape result to cache.

        ``cache_policy="bypass"`` skips the read but still writes — the
        caller wanted a fresh fetch and the fresh result is worth caching.
        ``cache_policy="no_store"`` skips the write entirely.
        """
        if not result.passed or req.cache_policy is CachePolicy.NO_STORE:
            return
        if result.text is not None and len(result.text.encode("utf-8")) > self.max_value_bytes:
            return

        key = self.key_for(req)
        path = self._path_for_key(key)
        lock = self._get_lock_for_key(key)
        with lock:
            write_json_atomic(path, _result_to_json(req, result))

    def clear(self) -> None:
        """Remove all cache entries."""
        shutil.rmtree(self.cache_dir, ignore_errors=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def clear_expired(self) -> int:
        """Remove expired JSON entries and return the number removed.

        Returns:
            Number of cache files removed.
        """
        removed = 0
        if not self.cache_dir.exists():
            return removed
        now = time.time()
        for path in self.cache_dir.glob("*/*.json"):
            try:
                if now - os.path.getmtime(path) > self.ttl_s:
                    path.unlink()
                    removed += 1
            except OSError as exc:
                logger.warning("scraping_cache path=%s impact=cleanup-skipped error=%s", path, exc)
        return removed

    @staticmethod
    def key_for(req: ScrapeRequest) -> str:
        """Return ADR-0107 cache key for a request.

        The key intentionally excludes ``cache_policy``: ``"bypass"`` skips
        reads via ``get()`` and ``"no_store"`` skips writes via ``put()``, so
        policy values never need to differentiate cache entries. Including
        them in the key would silently miss reads after a policy change.
        """
        material = (
            f"{sanitize_untrusted_text(req.url, max_length=2_000)}|"
            f"{sanitize_untrusted_text(req.accept_language, max_length=80)}|{req.max_bytes}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _path_for_key(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _get_lock_for_key(self, key: str) -> threading.Lock:
        with self._lock_registry_lock:
            lock = self._per_key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._per_key_locks[key] = lock
            return lock


def _result_to_json(req: ScrapeRequest, result: ScraperResult) -> dict[str, Any]:
    data = asdict(result)
    data.pop("url", None)
    data.pop("final_url", None)
    data["reason"] = result.reason.value
    data["fetched_at_utc"] = result.fetched_at_utc.isoformat()
    data["backend_chain"] = list(result.backend_chain)
    if isinstance(data.get("text"), str) and len(data["text"]) > _MAX_CACHED_TEXT_CHARS:
        data["text"] = data["text"][:_MAX_CACHED_TEXT_CHARS]
        data["extracted_chars"] = len(data["text"])
    return wrap_for_persistence(
        data,
        privacy_class=req.privacy_class,
        subject_id=req.subject_id,
        retention_days=req.retention_days,
        source="scraping.cache",
        redaction_applied=True,
    )


def _replace_with_retry(tmp: Path, path: Path) -> None:
    last_error: PermissionError | None = None
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.01)
    if last_error is not None:
        raise last_error


def _result_from_json(req: ScrapeRequest, data: object) -> ScraperResult:
    if not isinstance(data, dict):
        msg = "cache entry is not an object"
        raise ValueError(msg)
    if PRIVACY_ENVELOPE_KEY not in data:
        msg = "cache entry missing privacy envelope"
        raise ValueError(msg)
    require_privacy_envelope(data)
    data = data.get("payload")
    if not isinstance(data, dict):
        msg = "cache payload is not an object"
        raise ValueError(msg)
    payload: dict[str, object] = data
    required = {"passed", "reason", "fetched_at_utc", "backend", "extracted_chars"}
    assert_closed_schema(
        payload,
        allowed_keys={
            "passed",
            "reason",
            "fetched_at_utc",
            "backend",
            "extracted_chars",
            "http_status",
            "mime",
            "title",
            "text",
            "error_detail",
            "backend_chain",
            "cached",
            "attempts",
        },
        required_keys=required,
    )
    missing = required.difference(payload)
    if missing:
        msg = f"cache entry missing keys: {sorted(missing)}"
        raise ValueError(msg)
    fetched = datetime.fromisoformat(str(payload["fetched_at_utc"]))
    return ScraperResult(
        passed=bool(payload["passed"]),
        reason=ScrapeFailureReason(str(payload["reason"])),
        url=req.url,
        final_url=req.url if bool(payload["passed"]) else None,
        http_status=_optional_int(payload.get("http_status")),
        mime=_optional_str(payload.get("mime")),
        title=_optional_str(payload.get("title")),
        text=_optional_str(payload.get("text")),
        extracted_chars=int(str(payload["extracted_chars"])),
        fetched_at_utc=fetched,
        backend=str(payload["backend"]),
        error_detail=_optional_str(payload.get("error_detail")),
        backend_chain=tuple(str(item) for item in payload.get("backend_chain") or ()),
        cached=bool(payload.get("cached")),
        attempts=int(str(payload.get("attempts", 1))),
    )


def _optional_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) else None


def _optional_int(value: object) -> int | None:
    return int(value) if isinstance(value, int) else None


def _with_cached(result: ScraperResult) -> ScraperResult:
    return ScraperResult(
        passed=result.passed,
        reason=result.reason,
        url=result.url,
        final_url=result.final_url,
        http_status=result.http_status,
        mime=result.mime,
        title=result.title,
        text=result.text,
        extracted_chars=result.extracted_chars,
        fetched_at_utc=result.fetched_at_utc,
        backend=result.backend,
        error_detail=result.error_detail,
        backend_chain=result.backend_chain,
        cached=True,
        attempts=result.attempts,
    )
