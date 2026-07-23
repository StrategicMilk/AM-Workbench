"""Context bundle resolver for pre-assembling worker context.

This module pre-computes ``sem context`` excerpts at plan time and caches
results to disk so workers do not re-derive the same file context during
dispatch.  No I/O happens at import time.  The module-level
``_RESOLVER_LOCK`` only guards the default resolver singleton.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from vetinari.constants import get_user_dir
from vetinari.security.redaction import redact_text
from vetinari.utils import privacy_receipt

LOGGER = logging.getLogger(__name__)
_RESOLVER_LOCK = threading.Lock()
_DEFAULT_RESOLVER: ContextBundleResolver | None = None
MAX_BUDGET_TOKENS = 8192
DEFAULT_SEM_TIMEOUT_SECONDS = 15.0
DEFAULT_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
CONTEXT_BUNDLE_CACHE_SCHEMA_VERSION = 1
CONTEXT_BUNDLE_PRIVACY_SOURCE = "planning.context_bundle_cache"


@dataclass(frozen=True, slots=True)
class ContextBundleItem:
    """Validated ``sem context`` request for one plan context bundle."""

    entity: str
    file: str
    budget_tokens: int

    def __post_init__(self) -> None:
        # Q-M1 argv-injection hardening: ``entity`` and ``file`` are passed as
        # positional arguments to ``sem context``. A value starting with ``-``
        # would be interpreted by ``sem`` as a flag, not a positional. Reject
        # at construction so HTTP-bridged values cannot smuggle flags.
        for field_name in ("entity", "file"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"ContextBundleItem.{field_name} must be a non-empty string")
            if value.startswith("-"):
                raise ValueError(
                    f"ContextBundleItem.{field_name} cannot start with '-' (argv injection guard): {value!r}"
                )
        if self.budget_tokens <= 0:
            raise ValueError(f"ContextBundleItem.budget_tokens must be positive: {self.budget_tokens}")
        if self.budget_tokens > MAX_BUDGET_TOKENS:
            raise ValueError(f"ContextBundleItem.budget_tokens must be <= {MAX_BUDGET_TOKENS}: {self.budget_tokens}")


class ContextBundleResolver:
    """Resolve and cache ``sem context`` excerpts for shard context bundles."""

    def __init__(self, cache_dir: Path | None = None, sem_timeout_seconds: float = DEFAULT_SEM_TIMEOUT_SECONDS) -> None:
        self.project_root = _find_project_root()
        if cache_dir is None:
            cache_dir = get_user_dir() / "context-bundle-cache"
        self.cache_dir = cache_dir
        self._cache: dict[str, str] = {}
        self.sem_timeout_seconds = sem_timeout_seconds

    def resolve(self, item: ContextBundleItem) -> str | None:
        """Resolve one context bundle item through ``sem context``.

        Args:
            item: Bundle item describing the entity, file, and token budget.

        Returns:
            The resolved excerpt text, or ``None`` when the feature is disabled,
            ``sem`` is unavailable, or ``sem`` returns a non-zero exit code.
        """
        if os.environ.get("VETINARI_CONTEXT_BUNDLE") == "off":
            return None

        cache_key = f"{item.entity}:{item.file}:{item.budget_tokens}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        cached = self._read_cache(cache_key)
        if cached is not None:
            self._cache[cache_key] = cached
            return cached

        sem_path = shutil.which("sem")
        if sem_path is None:
            LOGGER.debug("sem executable is unavailable for context bundle %s", cache_key)
            return None

        try:
            completed = subprocess.run(
                [
                    sem_path,
                    "context",
                    item.entity,
                    "--file",
                    item.file,
                    "--budget",
                    str(item.budget_tokens),
                ],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.sem_timeout_seconds,
            )
        except FileNotFoundError:
            LOGGER.debug("sem executable is unavailable for context bundle %s", cache_key)
            return None
        except subprocess.TimeoutExpired:
            LOGGER.warning(
                "sem context timed out after %.1fs for context bundle %s", self.sem_timeout_seconds, cache_key
            )
            return None

        if completed.returncode != 0:
            LOGGER.debug(
                "sem context failed for context bundle %s with exit code %s: %s",
                cache_key,
                completed.returncode,
                completed.stderr.strip(),
            )
            return None

        excerpt = completed.stdout
        self._cache[cache_key] = excerpt
        self._write_cache(cache_key, excerpt)
        return excerpt

    def resolve_all(self, items: list[ContextBundleItem]) -> list[str | None]:
        """Resolve all context bundle items in order."""
        return [self.resolve(item) for item in items]

    def _cache_path(self, cache_key: str) -> Path:
        digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, cache_key: str) -> str | None:
        path = self._cache_path(cache_key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.debug("context bundle cache miss for %s", cache_key, exc_info=True)
            return None
        if not self._is_cache_payload_usable(payload):
            self._discard_cache_file(path)
            return None
        excerpt = payload.get("excerpt")
        return excerpt if isinstance(excerpt, str) else None

    def _write_cache(self, cache_key: str, excerpt: str) -> None:
        path = self._cache_path(cache_key)
        now = time.time()
        redacted_excerpt = _redact_context_excerpt(excerpt)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "schema_version": CONTEXT_BUNDLE_CACHE_SCHEMA_VERSION,
                        "created_at_unix": now,
                        "expires_at_unix": now + DEFAULT_CACHE_TTL_SECONDS,
                        "excerpt": redacted_excerpt,
                        "privacy_receipt": privacy_receipt(
                            privacy_class="subject_data",
                            subject_id="context-bundle-local-user",
                            retention_days=DEFAULT_CACHE_TTL_SECONDS // (24 * 60 * 60),
                            source=CONTEXT_BUNDLE_PRIVACY_SOURCE,
                            redaction_applied=redacted_excerpt != excerpt,
                        ),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            LOGGER.debug("failed to write context bundle cache file %s", path, exc_info=True)

    @staticmethod
    def _is_cache_payload_usable(payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        if payload.get("schema_version") != CONTEXT_BUNDLE_CACHE_SCHEMA_VERSION:
            return False
        expires_at = payload.get("expires_at_unix")
        if not isinstance(expires_at, int | float) or expires_at <= time.time():
            return False
        receipt = payload.get("privacy_receipt")
        if not isinstance(receipt, dict):
            return False
        try:
            privacy_receipt(
                privacy_class=str(receipt.get("privacy_class", "")),
                subject_id=receipt.get("subject_id"),
                retention_days=int(receipt.get("retention_days", 0)),
                source=str(receipt.get("source", "")),
                erasure_token=receipt.get("erasure_token"),
                redaction_applied=bool(receipt.get("redaction_applied", False)),
            )
        except (TypeError, ValueError) as exc:
            LOGGER.debug("context bundle cache privacy receipt rejected: %s", exc)
            return False
        return isinstance(payload.get("excerpt"), str)

    @staticmethod
    def _discard_cache_file(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOGGER.debug("failed to discard stale context bundle cache file %s", path, exc_info=True)


def get_default_resolver() -> ContextBundleResolver:
    """Return the process-local default resolver singleton.

    Returns:
        Shared ContextBundleResolver instance for this process.
    """
    global _DEFAULT_RESOLVER
    with _RESOLVER_LOCK:
        if _DEFAULT_RESOLVER is None:
            _DEFAULT_RESOLVER = ContextBundleResolver()
        return _DEFAULT_RESOLVER


def resolve_context_bundles(
    items: list[ContextBundleItem],
    cache_dir: Path | None = None,
) -> list[str | None]:
    """Primary entry point for resolving context bundle items.

    Args:
        items: Bundle items to resolve.
        cache_dir: Optional cache directory override.

    Returns:
        Resolved excerpts in item order, with ``None`` for unavailable items.
    """
    resolver = get_default_resolver() if cache_dir is None else ContextBundleResolver(cache_dir=cache_dir)
    return resolver.resolve_all(items)


def _find_project_root() -> Path:
    current_file = Path(__file__).resolve()
    for candidate in current_file.parents:
        if (candidate / "pyproject.toml").exists():
            return candidate
    return Path.cwd()


def _redact_context_excerpt(excerpt: str) -> str:
    """Redact raw sem excerpts before disk persistence."""
    return redact_text(excerpt)
