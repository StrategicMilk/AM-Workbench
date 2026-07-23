"""URL ingestion helpers for the RAG knowledge base."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)
_MAX_URL_RESPONSE_BYTES = 2_000_000


def validate_ingest_fetch_url(url: str, require_https: bool = True) -> None:
    """Validate the concrete URL fetch target.

    Args:
    url: URL to fetch.
    require_https: Whether non-HTTPS URLs are blocked.

    Raises:
        ValueError: Propagated when validation, persistence, or execution fails.
    """
    parsed = urlparse(url)
    if require_https and parsed.scheme != "https":
        raise ValueError("KnowledgeBase URL ingestion requires https")


def redact_url(url: str) -> str:
    """Return a log-safe URL containing only scheme and host.

    Args:
        url: Raw URL.

    Returns:
        Redacted URL string.
    """
    parsed = urlparse(url)
    host = parsed.hostname or "<missing-host>"
    return f"{parsed.scheme or '<missing-scheme>'}://{host}"


def fetch_url_bytes(url: str) -> tuple[int, dict[str, str], bytes]:
    """Fetch URL response bytes with bounded body size.

    Args:
    url: URL to fetch.

    Returns:
    Tuple of status code, lowercase headers, and response bytes.

    Raises:
        ValueError: Propagated when validation, persistence, or execution fails.
    """
    import httpx

    chunks: list[bytes] = []
    total = 0
    with httpx.stream("GET", url, timeout=10, follow_redirects=False) as resp:
        status_code = int(resp.status_code)
        headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
        if 300 <= status_code < 400:
            return status_code, headers, b""
        resp.raise_for_status()
        for chunk in resp.iter_bytes():
            if not chunk:
                continue
            total += len(chunk)
            if total > _MAX_URL_RESPONSE_BYTES:
                raise ValueError("URL response exceeded KnowledgeBase byte cap")
            chunks.append(chunk)
    return status_code, headers, b"".join(chunks)


def ingest_url_documents(
    kb: object,
    url: str,
    *,
    category: str,
    chunk_size: int,
    chunk_overlap: int,
    require_https: bool,
    max_chunks: int,
    fetcher: Callable[[str], tuple[int, dict[str, str], bytes]],
) -> int:
    """Fetch a URL and ingest its decoded text into a knowledge base.

    Args:
        kb: KnowledgeBase-like object exposing ``add_document``.
        url: URL to ingest.
        category: Document category.
        chunk_size: Maximum chunk size.
        chunk_overlap: Overlap between chunks.
        require_https: Whether fetch targets must be HTTPS.
        max_chunks: Maximum chunks to ingest from the URL.
        fetcher: Fetch callback, injected for patchable tests.

    Returns:
        Number of chunks ingested.
    """
    from vetinari.security import validate_url_no_ssrf

    fetch_url = validate_url_no_ssrf(url)
    validate_ingest_fetch_url(fetch_url, require_https=require_https)
    try:
        status_code, headers, raw_content = fetcher(fetch_url)
        if 300 <= status_code < 400:
            redirected = validate_url_no_ssrf(urljoin(fetch_url, headers.get("location", "")))
            validate_ingest_fetch_url(redirected, require_https=require_https)
            fetch_url = redirected
            status_code, _headers, raw_content = fetcher(fetch_url)
            if 300 <= status_code < 400:
                logger.warning("KnowledgeBase URL redirect chain rejected for %s", redact_url(fetch_url))
                return 0
    except Exception as exc:
        logger.warning("KnowledgeBase URL fetch failed for %s - skipping ingest: %s", redact_url(url), exc)
        return 0
    text = raw_content.decode("utf-8", errors="ignore")
    chunks = kb._chunk(text, chunk_size, chunk_overlap)[:max_chunks]
    count = 0
    for i, chunk in enumerate(chunks):
        kb.add_document(
            content=chunk,
            source=fetch_url,
            category=category,
            doc_id=f"doc_{hashlib.md5(f'{fetch_url}_{i}'.encode(), usedforsecurity=False).hexdigest()[:8]}",
        )
        count += 1
    logger.info("KnowledgeBase ingested %d chunks from URL %s", count, redact_url(fetch_url))
    return count


__all__ = ["fetch_url_bytes", "ingest_url_documents", "redact_url", "validate_ingest_fetch_url"]
