"""Agent tool wrapper for Vetinari's hardened scraper.

The `scrape()` callable is the single entry point used by the in-process
ToolRegistry adapter (`ScrapingTool`). Every invocation routes through
`default_dispatcher().fetch()` so Pack K's URL allowlist, cache, retry, and
telemetry extensions apply uniformly across the agent toolkit, the admin HTTP
native API route and the `--scrape` CLI flag.
ADR-0108 records the chosen registration pattern.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any

from vetinari.execution_context import ToolPermission
from vetinari.privacy import PRIVACY_ENVELOPE_KEY, privacy_receipt
from vetinari.scraping.contracts import ScrapeFailureReason, ScrapeRequest, ScraperResult
from vetinari.scraping.dispatcher import default_dispatcher
from vetinari.security.redaction import REDACTED_URL, redact_text
from vetinari.tool_interface import Tool, ToolCategory, ToolMetadata, ToolParameter, ToolRegistry, ToolResult
from vetinari.types import ExecutionMode
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


_MIN_TIMEOUT_S = 0.1
_MAX_TIMEOUT_S = 60.0
_SENSITIVE_DETAIL_MARKERS = ("api_key", "apikey", "password", "secret", "token", "credential")


def _redact_failure_detail(value: object) -> str:
    detail = redact_text(str(value))
    return "[REDACTED]" if any(marker in detail.lower() for marker in _SENSITIVE_DETAIL_MARKERS) else detail


def serialize_result(result: ScraperResult) -> dict[str, Any]:
    """Return a JSON-compatible dict for a scraper result.

    Args:
        result: Scraper result returned by the hardened dispatcher.

    Returns:
        Serialized scraper payload with failure URLs and error details redacted.
    """
    payload = dataclass_to_dict(result)
    if not result.passed:
        payload["url"] = REDACTED_URL
        if payload.get("final_url"):
            payload["final_url"] = REDACTED_URL
        if payload.get("error_detail"):
            payload["error_detail"] = _redact_failure_detail(payload["error_detail"])
    payload[PRIVACY_ENVELOPE_KEY] = privacy_receipt(
        privacy_class="operational",
        source="agent.scraping_tool.result",
        retention_days=1,
        redaction_applied=not result.passed,
    )
    return payload


def _bounded_failure(url: str, reason: ScrapeFailureReason, *, detail: str | None = None) -> dict[str, Any]:
    return serialize_result(
        ScraperResult(
            passed=False,
            reason=reason,
            url=url,
            final_url=None,
            http_status=None,
            mime=None,
            title=None,
            text=None,
            extracted_chars=0,
            fetched_at_utc=datetime.now(timezone.utc),
            backend="agent-tool",
            error_detail=detail,
        )
    )


def scrape(url: str, *, cache_policy: str = "default", timeout_s: float = 15.0) -> dict[str, Any]:
    """Fetch a url and return its main text as JSON.

    Honors per-host rate limits, blocks SSRF / private IPs, caches results for
    24h, and returns a JSON dict with `passed`, `text`, `title`, `reason`,
    `attempts`, `cached`, and related scraper fields.

    Returns:
        JSON-compatible scraper result dictionary.
    """
    if not math.isfinite(timeout_s) or not _MIN_TIMEOUT_S <= timeout_s <= _MAX_TIMEOUT_S:
        logger.debug("scrape rejected timeout_s=%s outside [%.1f, %.1f]", timeout_s, _MIN_TIMEOUT_S, _MAX_TIMEOUT_S)
        return _bounded_failure(
            url,
            ScrapeFailureReason.INVALID_REQUEST,
            detail=f"timeout_s must be finite and in [{_MIN_TIMEOUT_S}, {_MAX_TIMEOUT_S}]",
        )

    try:
        req = ScrapeRequest(url=url, cache_policy=cache_policy, timeout_s=timeout_s)
        return serialize_result(default_dispatcher().fetch(req))
    except Exception as exc:
        logger.warning("scraping tool dispatcher raised: %s", type(exc).__name__)
        return _bounded_failure(url, ScrapeFailureReason.NETWORK_ERROR, detail=type(exc).__name__)


class ScrapingTool(Tool):
    """ToolRegistry adapter for the `scrape` function."""

    def __init__(self) -> None:
        super().__init__(
            ToolMetadata(
                name="scrape",
                description=scrape.__doc__ or "Fetch a web page through Vetinari's hardened scraper.",
                category=ToolCategory.SEARCH_ANALYSIS,
                parameters=[
                    ToolParameter("url", str, "HTTP or HTTPS URL to fetch."),
                    ToolParameter("cache_policy", str, "Cache policy.", required=False, default="default"),
                    ToolParameter("timeout_s", float, "Request timeout in seconds.", required=False, default=15.0),
                ],
                required_permissions=[ToolPermission.NETWORK_REQUEST],
                allowed_modes=[ExecutionMode.EXECUTION],
                tags=["scraping", "web", "hardened"],
            )
        )

    def execute(self, **kwargs: Any) -> ToolResult:
        """Execute the scrape tool through the hardened dispatcher.

        Returns:
            ToolResult containing the serialized scraper result.
        """
        url = kwargs.get("url")
        if not isinstance(url, str) or not url:
            return ToolResult(success=False, output=None, error="url is required and must be a non-empty string")
        result = scrape(
            url,
            cache_policy=str(kwargs.get("cache_policy", "default")),
            timeout_s=float(kwargs.get("timeout_s", 15.0)),
        )
        return ToolResult(success=bool(result.get("passed")), output=result, error=result.get("error_detail"))


def register(registry: ToolRegistry) -> None:
    """Register the scraping tool into a Vetinari ToolRegistry."""
    if registry.get("scrape") is None:
        registry.register(ScrapingTool())
