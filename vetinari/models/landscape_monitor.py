"""Model landscape monitor — track external model releases and flag stale knowledge.

Background service that checks HuggingFace, llama.cpp, and vllm for new
model releases weekly. Compares rankings against local knowledge files
and flags entries that are outdated or missing.

Pipeline role: Knowledge Maintenance — keeps model knowledge current.
Offline behavior: returns cached data with staleness warning when
network is unavailable. Cache is stored as JSON in .vetinari/cache/landscape/.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from vetinari.boundary_guards import account_evidence_drop
from vetinari.constants import _PROJECT_ROOT, VETINARI_STATE_DIR
from vetinari.learning.atomic_writers import write_json_atomic
from vetinari.utils import privacy_receipt

logger = logging.getLogger(__name__)


# -- Configuration --
_KNOWLEDGE_DIR = _PROJECT_ROOT / "config" / "knowledge"
_DEFAULT_CACHE_DIR = VETINARI_STATE_DIR / "cache" / "landscape"
DEFAULT_CACHE_TTL_DAYS = 7  # Cache validity in days
_CACHE_TTL_SECONDS = DEFAULT_CACHE_TTL_DAYS * 86400
_REQUEST_TIMEOUT_SECS = 30  # HTTP request timeout

# Known source URLs (configurable)
_HUGGINGFACE_API = "https://huggingface.co/api/models"
_LLAMA_CPP_RELEASES = "https://api.github.com/repos/ggerganov/llama.cpp/releases"
_VLLM_RELEASES = "https://api.github.com/repos/vllm-project/vllm/releases"
_DEFAULT_USER_AGENT = "model-landscape-monitor/1.0"


def _vllm_landscape_tracking_enabled() -> bool:
    """Return whether vLLM release monitoring is active for this product config."""
    families_path = _PROJECT_ROOT / "config" / "model_families.yaml"
    try:
        with families_path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except OSError:
        logger.warning("Could not read model family config; disabling vLLM landscape tracking", exc_info=True)
        return False
    for row in data.get("endpoint_families", []):
        if not isinstance(row, dict) or row.get("family") != "vllm":
            continue
        status = str(row.get("status", "")).lower()
        return "disabled" not in status and status not in {"retired", "removed"}
    return True


@dataclass(frozen=True, slots=True)
class ModelRelease:
    """A model or tool release from an external source."""

    source: str  # "huggingface", "llama_cpp", "vllm"
    name: str
    version: str
    released_at: str
    url: str = ""

    def __repr__(self) -> str:
        return f"ModelRelease(name={self.name!r}, version={self.version!r}, source={self.source!r})"


@dataclass(frozen=True, slots=True)
class StaleEntry:
    """A knowledge file entry flagged as potentially stale."""

    knowledge_file: str
    entry_key: str
    reason: str
    last_updated: str = ""

    def __repr__(self) -> str:
        return f"StaleEntry(entry_key={self.entry_key!r}, reason={self.reason!r})"


@dataclass(slots=True)
class LandscapeReport:
    """Aggregated landscape monitoring results."""

    sources_checked: list[str] = field(default_factory=list)
    releases_found: list[ModelRelease] = field(default_factory=list)
    stale_entries: list[StaleEntry] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    from_cache: bool = False
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __repr__(self) -> str:
        return (
            f"LandscapeReport(sources={len(self.sources_checked)}, "
            f"releases={len(self.releases_found)}, stale={len(self.stale_entries)})"
        )


class LandscapeMonitor:
    """Monitor external model landscape and flag stale local knowledge.

    Fetches release data from HuggingFace, llama.cpp, and vllm APIs.
    Compares against local knowledge YAML files. Caches results
    for offline use with configurable TTL.

    Args:
        cache_dir: Directory for cached API responses. Defaults to .vetinari/cache/landscape/.
        cache_ttl_seconds: Cache TTL in seconds. Defaults to 7 days.
        knowledge_dir: Directory containing knowledge YAML files.
    """

    def __init__(
        self,
        cache_dir: Path | None = None,
        cache_ttl_seconds: int = _CACHE_TTL_SECONDS,
        knowledge_dir: Path | None = None,
    ) -> None:
        self._cache_dir = cache_dir or _DEFAULT_CACHE_DIR
        self._cache_ttl = cache_ttl_seconds
        self._knowledge_dir = knowledge_dir or _KNOWLEDGE_DIR
        self._lock = threading.Lock()

    # -- Public API --

    def check_huggingface(self, *, with_meta: bool = False) -> list[ModelRelease] | tuple[list[ModelRelease], bool]:
        """Fetch trending models from HuggingFace API.

        Returns cached data if network is unavailable. Limits to
        top 20 models sorted by downloads.

        Args:
            with_meta: When True, return a (releases, used_stale_cache) tuple so
                callers like ``compare_rankings`` can track cache usage.

        Returns:
            List of ModelRelease from HuggingFace, or a tuple of
            (releases, used_stale_cache) when ``with_meta=True``.
        """
        releases, used_stale_cache = self._fetch_and_parse(
            source="huggingface",
            url=f"{_HUGGINGFACE_API}?sort=downloads&direction=-1&limit=20",
            parser=self._parse_huggingface,
        )
        if with_meta:
            return releases, used_stale_cache
        return releases

    def check_llama_cpp(self, *, with_meta: bool = False) -> list[ModelRelease] | tuple[list[ModelRelease], bool]:
        """Fetch recent releases from llama.cpp GitHub repo.

        Args:
            with_meta: When True, return a (releases, used_stale_cache) tuple so
                callers like ``compare_rankings`` can track cache usage.

        Returns:
            List of ModelRelease from llama.cpp releases, or a tuple of
            (releases, used_stale_cache) when ``with_meta=True``.
        """
        releases, used_stale_cache = self._fetch_and_parse(
            source="llama_cpp",
            url=_LLAMA_CPP_RELEASES,
            parser=self._parse_github_releases,
        )
        if with_meta:
            return releases, used_stale_cache
        return releases

    def check_vllm(self, *, with_meta: bool = False) -> list[ModelRelease] | tuple[list[ModelRelease], bool]:
        """Fetch recent releases from vllm GitHub repo.

        Args:
            with_meta: When True, return a (releases, used_stale_cache) tuple so
                callers like ``compare_rankings`` can track cache usage.

        Returns:
            List of ModelRelease from vllm releases, or a tuple of
            (releases, used_stale_cache) when ``with_meta=True``.
        """
        releases, used_stale_cache = self._fetch_and_parse(
            source="vllm",
            url=_VLLM_RELEASES,
            parser=self._parse_github_releases,
        )
        if with_meta:
            return releases, used_stale_cache
        return releases

    def compare_rankings(self) -> LandscapeReport:
        """Run a full landscape check across all sources.

        Fetches data from all sources (or cache), compares against
        local knowledge, and reports stale entries.

        Returns:
            LandscapeReport with releases found and stale entries.
        """
        report = LandscapeReport()

        # Call each source check method; use the _fetch_and_parse tuple variant so we
        # can track whether any source fell back to stale cached data.
        source_checks = [
            ("huggingface", self.check_huggingface),
            ("llama_cpp", self.check_llama_cpp),
        ]
        if _vllm_landscape_tracking_enabled():
            source_checks.append(("vllm", self.check_vllm))
        else:
            logger.info("Skipping vLLM landscape tracking because the vLLM backend is disabled by default")
        for source_name, fetch_fn in source_checks:
            try:
                releases, used_stale_cache = fetch_fn(with_meta=True)
                report.releases_found.extend(releases)
                if not used_stale_cache:
                    report.sources_checked.append(source_name)
                else:
                    report.errors.append(f"Source {source_name}: unreachable and no cache - not recorded as checked")
                if used_stale_cache:
                    report.from_cache = True
            except Exception as exc:
                logger.warning("Could not check %s — skipping this source", source_name)
                source_record = {"source": source_name, "error": str(exc)}
                account_evidence_drop(source_record, "landscape_check", logger=logger)
                report.errors.append(f"Failed to check {source_name}")

        # Check for stale knowledge
        report.stale_entries = self.flag_stale_knowledge()
        return report

    def flag_stale_knowledge(self) -> list[StaleEntry]:
        """Compare cached model data against knowledge YAML and flag stale entries.

        Reads model_families.yaml and checks if any listed families
        are missing from recent external data or have no recent release.

        Returns:
            List of StaleEntry for knowledge items that may be outdated.
        """
        stale: list[StaleEntry] = []
        families_path = self._knowledge_dir / "model_families.yaml"
        if not families_path.exists():
            logger.info("No model_families.yaml found — skipping stale check")
            return stale

        with open(families_path, encoding="utf-8") as f:
            families_data = yaml.safe_load(f) or {}

        families = families_data.get("model_families", {})
        if not isinstance(families, dict):
            return stale

        # Load cached HuggingFace data for comparison
        cached_hf = self._load_cache("huggingface")
        known_model_names: set[str] = set()
        if cached_hf:
            for entry in cached_hf:
                model_id = entry.get("modelId", entry.get("name", ""))
                if model_id:
                    known_model_names.add(model_id.lower())

        for family_key, family_info in families.items():
            if not isinstance(family_info, dict):
                continue
            # Check if any variant of this family appears in recent external data
            family_lower = family_key.lower()
            found = any(family_lower in name for name in known_model_names)
            if not found and known_model_names:
                stale.append(
                    StaleEntry(
                        knowledge_file="model_families.yaml",
                        entry_key=family_key,
                        reason="Not found in recent HuggingFace top models",
                    )
                )

        if stale:
            logger.warning("Found %d potentially stale knowledge entries", len(stale))
        return stale

    # -- Internal helpers --

    def _fetch_and_parse(
        self,
        source: str,
        url: str,
        parser: Any,
    ) -> tuple[list[ModelRelease], bool]:
        """Fetch data from URL with cache fallback.

        Args:
            source: Source identifier for cache key.
            url: URL to fetch.
            parser: Function to parse raw JSON into ModelRelease list.

        Returns:
            Tuple of (releases, used_stale_cache).  ``used_stale_cache`` is
            True when the result came from an out-of-date cache because the
            network was unavailable.
        """
        # Check cache first
        cached = self._load_cache(source)
        cache_age = self._cache_age(source)
        if cached is not None and cache_age < self._cache_ttl:
            logger.info("Using cached %s data (age: %ds)", source, cache_age)
            return parser(source, cached), False

        # Try network fetch
        try:
            raw = self._http_get(url)
            data = json.loads(raw)
            self._save_cache(source, data)
            return parser(source, data), False
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            # Offline fallback — stale cache
            if cached is not None:
                logger.warning(
                    "Network unavailable for %s (error: %s) — using stale cache (age: %ds)",
                    source,
                    type(exc).__name__,
                    cache_age,
                )
                return parser(source, cached), True
            logger.warning(
                "Network unavailable for %s and no cache exists — returning empty",
                source,
            )
            return [], True

    @staticmethod
    def _http_get(url: str) -> str:
        """Perform an HTTP GET request.

        Args:
            url: URL to fetch.

        Returns:
            Response body as string.

        Raises:
            urllib.error.URLError: On network failure.
        """
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise urllib.error.URLError(f"unsupported URL scheme for model landscape source: {url}")
        user_agent = os.environ.get("VETINARI_LANDSCAPE_USER_AGENT", _DEFAULT_USER_AGENT)
        request = urllib.request.Request(url, headers={"User-Agent": user_agent})
        # Use the module-level urlopen so tests can patch
        # ``vetinari.models.landscape_monitor.urllib.request.urlopen`` and have
        # the patch take effect on this seam.
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECS) as response:  # nosec B310 - URL scheme validated above
            return response.read().decode("utf-8")

    @staticmethod
    def _parse_huggingface(source: str, data: list[dict[str, Any]]) -> list[ModelRelease]:
        """Parse HuggingFace API response into ModelRelease list."""
        releases: list[ModelRelease] = []
        if not isinstance(data, list):
            return releases
        for entry in data[:20]:
            model_id = entry.get("modelId", entry.get("id", "unknown"))
            releases.append(
                ModelRelease(
                    source=source,
                    name=str(model_id),
                    version=str(entry.get("sha", "latest")),
                    released_at=str(entry.get("lastModified", "")),
                    url=f"https://huggingface.co/{model_id}",
                )
            )
        return releases

    @staticmethod
    def _parse_github_releases(source: str, data: list[dict[str, Any]]) -> list[ModelRelease]:
        """Parse GitHub releases API response into ModelRelease list."""
        releases: list[ModelRelease] = []
        if not isinstance(data, list):
            return releases
        releases.extend(
            ModelRelease(
                source=source,
                name=str(entry.get("name", entry.get("tag_name", "unknown"))),
                version=str(entry.get("tag_name", "")),
                released_at=str(entry.get("published_at", "")),
                url=str(entry.get("html_url", "")),
            )
            for entry in data[:10]
        )
        return releases

    # -- Cache management --

    def _cache_path(self, source: str) -> Path:
        return self._cache_dir / f"{source}.json"

    def _load_cache(self, source: str) -> Any | None:
        path = self._cache_path(source)
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                cached = json.load(f)
                if isinstance(cached, dict) and "data" in cached and "privacy_receipt" in cached:
                    return cached["data"]
                return cached
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "Could not load landscape cache for source %r — corrupted or unreadable file, cache will be skipped",
                source,
            )
            return None

    def _save_cache(self, source: str, data: Any) -> None:
        with self._lock:
            write_json_atomic(
                self._cache_path(source),
                {
                    "source": source,
                    "data": data,
                    "privacy_receipt": privacy_receipt(
                        privacy_class="public",
                        retention_days=DEFAULT_CACHE_TTL_DAYS,
                        source="landscape_monitor.cache",
                        redaction_applied=False,
                    ),
                },
            )

    def _cache_age(self, source: str) -> float:
        """Return cache age in seconds, or infinity if no cache."""
        path = self._cache_path(source)
        if not path.exists():
            return float("inf")
        return time.time() - path.stat().st_mtime
