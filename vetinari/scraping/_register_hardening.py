"""Register Pack K scraping hardening extensions.

Call :func:`register_extensions` to register URL validation, filesystem
caching, retry policy, and telemetry completion hooks. Registration is
idempotent within an interpreter process.
"""

from __future__ import annotations

from pathlib import Path

from vetinari.scraping import extensions
from vetinari.scraping._hardening_config import load_hardening_config
from vetinari.scraping.cache import FilesystemCache
from vetinari.scraping.retry import ExponentialBackoffRetryPolicy
from vetinari.scraping.telemetry import emit_completion
from vetinari.scraping.url_validator import URLValidator

_REGISTERED = False


def register_extensions() -> None:
    """Register hardening extensions once per interpreter process."""
    global _REGISTERED
    if _REGISTERED:
        return
    cfg = load_hardening_config()
    extensions.register_pre_flight(URLValidator.from_config(cfg.url).check)
    if cfg.cache.enabled:
        extensions.register_cache(
            FilesystemCache(
                cache_dir=Path(cfg.cache.cache_dir),
                ttl_s=cfg.cache.ttl_s,
                max_value_bytes=cfg.cache.max_value_bytes,
            )
        )
    if cfg.retry.enabled:
        extensions.register_retry_policy(
            ExponentialBackoffRetryPolicy(
                max_attempts=cfg.retry.max_attempts,
                base_delay_s=cfg.retry.base_delay_s,
                max_delay_s=cfg.retry.max_delay_s,
                jitter=cfg.retry.jitter,
            )
        )
    if cfg.telemetry.enabled:
        extensions.register_event_hook(emit_completion)
    _sync_default_dispatcher_snapshot()
    _REGISTERED = True


def _sync_default_dispatcher_snapshot() -> None:
    """Expose live extensions through the default dispatcher's public fields."""
    from vetinari.scraping.dispatcher import default_dispatcher

    dispatcher = default_dispatcher()
    dispatcher.pre_flight_checks = tuple(extensions.get_pre_flight_checks())
    dispatcher.cache = extensions.get_cache()
    dispatcher.retry_policy = extensions.get_retry_policy()


register_extensions()
