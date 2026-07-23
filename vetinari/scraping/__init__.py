"""Self-hosted web scraping contracts and static backend.

Pack K hardening is NOT auto-registered here. Callers that want SSRF
protection, caching, retry, or telemetry MUST import
``vetinari.scraping._register_hardening`` explicitly. This matches the
SHARD-05 wiring contract: explicit-activation makes the dependency visible
to operators, tests, and downstream consumers (Pack L route, agent toolkit,
__main__).
"""

from __future__ import annotations

from vetinari.scraping.contracts import ScrapeFailureReason, ScrapeRequest, ScraperResult
from vetinari.scraping.dispatcher import Dispatcher, default_dispatcher
from vetinari.scraping.extensions import register_cache, register_pre_flight, register_retry_policy
from vetinari.scraping.static_backend import StaticBackend

__all__ = [
    "Dispatcher",
    "ScrapeFailureReason",
    "ScrapeRequest",
    "ScraperResult",
    "StaticBackend",
    "default_dispatcher",
    "register_cache",
    "register_pre_flight",
    "register_retry_policy",
]
