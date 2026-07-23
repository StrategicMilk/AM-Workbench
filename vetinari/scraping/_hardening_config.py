"""Typed loader for ``config/scraping_hardening.yaml``.

The config is cached after the first read because the values are process
defaults used during Pack K extension registration.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any

from vetinari.scraping.url_validator import URLValidatorConfig

logger = logging.getLogger(__name__)


try:
    yaml: Any | None = import_module("yaml")
except ImportError:
    yaml = None

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _PROJECT_ROOT / "config" / "scraping_hardening.yaml"
_DEFAULT_CACHE_DIR = _PROJECT_ROOT / "outputs" / "scraping_cache"
_HARDENING_CONFIG_CACHE: HardeningConfig | None = None
_CONFIG_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class CacheConfig:
    """Filesystem cache defaults."""

    enabled: bool = True
    cache_dir: Path = _DEFAULT_CACHE_DIR
    ttl_s: float = 86400.0
    max_value_bytes: int = 5_000_000

    def __repr__(self) -> str:
        return (
            "CacheConfig("
            f"enabled={self.enabled!r}, cache_dir={str(self.cache_dir)!r}, "
            f"ttl_s={self.ttl_s!r}, max_value_bytes={self.max_value_bytes!r})"
        )


@dataclass(frozen=True, slots=True)
class RetryConfig:
    """Retry policy defaults."""

    enabled: bool = True
    max_attempts: int = 3
    base_delay_s: float = 1.0
    max_delay_s: float = 30.0
    jitter: float = 0.1

    def __repr__(self) -> str:
        return (
            "RetryConfig("
            f"enabled={self.enabled!r}, max_attempts={self.max_attempts!r}, "
            f"base_delay_s={self.base_delay_s!r}, max_delay_s={self.max_delay_s!r}, "
            f"jitter={self.jitter!r})"
        )


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    """Telemetry defaults."""

    enabled: bool = True
    topic: str = "scraping.fetch.completed"
    log_payload_at_debug: bool = False


@dataclass(frozen=True, slots=True)
class HardeningConfig:
    """Complete Pack K scraping hardening config."""

    url: URLValidatorConfig = field(default_factory=URLValidatorConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)

    def __repr__(self) -> str:
        return (
            "HardeningConfig("
            f"url={self.url!r}, cache={self.cache!r}, "
            f"retry={self.retry!r}, telemetry_enabled={self.telemetry.enabled!r})"
        )


def load_hardening_config(path: Path | None = None) -> HardeningConfig:
    """Return cached hardening config, loading YAML on first use.

    Returns:
        Parsed hardening config, or defaults when no YAML is available.
    """
    global _HARDENING_CONFIG_CACHE
    if path is None and _HARDENING_CONFIG_CACHE is not None:
        return _HARDENING_CONFIG_CACHE
    with _CONFIG_LOCK:
        if path is None and _HARDENING_CONFIG_CACHE is not None:
            return _HARDENING_CONFIG_CACHE
        cfg = _load_from_path(path or _CONFIG_PATH)
        if path is None:
            _HARDENING_CONFIG_CACHE = cfg
        return cfg


def _load_from_path(path: Path) -> HardeningConfig:
    if yaml is None or not path.exists():
        return HardeningConfig()
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        logger.warning(
            "scraping_hardening_config path=%s impact=using-defaults rule=non-mapping-yaml",
            path,
        )
        return HardeningConfig()
    return HardeningConfig(
        url=_url_config(_section(loaded, "url")),
        cache=_cache_config(_section(loaded, "cache")),
        retry=_retry_config(_section(loaded, "retry")),
        telemetry=_telemetry_config(_section(loaded, "telemetry")),
    )


def _section(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _url_config(data: dict[str, object]) -> URLValidatorConfig:
    return URLValidatorConfig(
        schemes=_str_tuple(data.get("schemes"), ("http", "https")),
        host_allowlist=_str_tuple(data.get("host_allowlist"), ()),
        host_denylist=_str_tuple(data.get("host_denylist"), ()),
        dns_cache_ttl_s=_float(data.get("dns_cache_ttl_s"), 60.0),
    )


def _cache_config(data: dict[str, object]) -> CacheConfig:
    return CacheConfig(
        enabled=_bool(data.get("enabled"), True),
        cache_dir=Path(str(data.get("cache_dir", _DEFAULT_CACHE_DIR))),
        ttl_s=_float(data.get("ttl_s"), 86400.0),
        max_value_bytes=_int(data.get("max_value_bytes"), 5_000_000),
    )


def _retry_config(data: dict[str, object]) -> RetryConfig:
    return RetryConfig(
        enabled=_bool(data.get("enabled"), True),
        max_attempts=_int(data.get("max_attempts"), 3),
        base_delay_s=_float(data.get("base_delay_s"), 1.0),
        max_delay_s=_float(data.get("max_delay_s"), 30.0),
        jitter=_float(data.get("jitter"), 0.1),
    )


def _telemetry_config(data: dict[str, object]) -> TelemetryConfig:
    return TelemetryConfig(
        enabled=_bool(data.get("enabled"), True),
        topic=str(data.get("topic", "scraping.fetch.completed")),
        log_payload_at_debug=_bool(data.get("log_payload_at_debug"), False),
    )


def _str_tuple(value: object, default: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    return default


def _float(value: object, default: float) -> float:
    return float(value) if isinstance(value, int | float) else default


def _int(value: object, default: int) -> int:
    return int(value) if isinstance(value, int) else default


def _bool(value: object, default: bool) -> bool:
    return bool(value) if isinstance(value, bool) else default
