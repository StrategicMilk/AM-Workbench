# SPDX-FileCopyrightText: 2024-2026 Vetinari Contributors
# SPDX-License-Identifier: Apache-2.0
"""Log Aggregation Backend Implementations for the Vetinari Dashboard.

Contains all concrete backend classes that ship log records to external
systems, plus the SSEBackend for real-time dashboard streaming.

Supported backends
------------------
  file     Write newline-delimited JSON to a local file.
  datadog  Send log entries via the Datadog Logs Intake API.
  webhook  POST aggregated logs to an arbitrary HTTP endpoint.
  sse      Buffer records for Server-Sent Events streaming clients.

All network backends degrade gracefully: if the dependency (``requests``) is
missing, or the remote endpoint is unreachable, the error is logged and the
call returns False rather than raising.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from collections import deque
from pathlib import Path
from typing import Any

from vetinari.constants import DATADOG_LOGS_URL, LOG_BACKEND_BUFFER_SIZE, LOG_BACKEND_TIMEOUT, LOGS_DIR
from vetinari.dashboard.log_aggregator import BackendBase, LogRecord
from vetinari.http import create_session

logger = logging.getLogger(__name__)

DEFAULT_FILE_BACKEND_MAX_BYTES = 10 * 1024 * 1024  # Rotate dashboard JSONL logs before 10 MiB.
DEFAULT_FILE_BACKEND_BACKUP_COUNT = 5  # Keep five bounded dashboard log backups.

# GDPR Art. 17 retention window for log records that may carry request bodies
# or user-identifying fields. Configured via VETINARI_LOG_PII_TTL_DAYS; default
# 30 days. Backends that surface this value to downstream retention policies
# (file rotation, Datadog ingestion tagging, webhook receiver) MUST attach it
# to records that contain PII so the GDPR-compliant erasure window is honored.
LOG_PII_TTL_DAYS = int(os.environ.get("VETINARI_LOG_PII_TTL_DAYS", "30"))  # GDPR Art. 17 retention bound

DATADOG_LOGS_URLS_BY_SITE = {
    "us1": DATADOG_LOGS_URL,
    "us3": "https://http-intake.logs.us3.datadoghq.com/api/v2/logs",
    "us5": "https://http-intake.logs.us5.datadoghq.com/api/v2/logs",
    "eu": "https://http-intake.logs.datadoghq.eu/api/v2/logs",
    "ap1": "https://http-intake.logs.ap1.datadoghq.com/api/v2/logs",
    "ap2": "https://http-intake.logs.ap2.datadoghq.com/api/v2/logs",
    "gov": "https://http-intake.logs.ddog-gov.com/api/v2/logs",
}
DATADOG_SITE_JURISDICTIONS = {
    "us1": "US",
    "us3": "US",
    "us5": "US",
    "gov": "US",
    "eu": "EU",
    "ap1": "AP",
    "ap2": "AP",
}


def _jurisdiction_family(jurisdiction: str) -> str:
    normalized = jurisdiction.strip().upper()
    if normalized.startswith(("US", "CA-US")) or normalized in {"USA", "UNITED STATES"}:
        return "US"
    if normalized.startswith(("EU", "EEA")):
        return "EU"
    if normalized.startswith(("AP", "ASIA", "JP", "AU", "SG")):
        return "AP"
    return normalized


# ---------------------------------------------------------------------------
# File backend
# ---------------------------------------------------------------------------


class FileBackend(BackendBase):
    """Appends newline-delimited JSON to a local file."""

    name = "file"

    def __init__(self) -> None:
        self._path: str | None = None
        self._max_bytes = DEFAULT_FILE_BACKEND_MAX_BYTES
        self._backup_count = DEFAULT_FILE_BACKEND_BACKUP_COUNT
        self._lock = threading.Lock()

    def configure(
        self,
        path: str = str(LOGS_DIR / "vetinari_audit.jsonl"),
        max_bytes: int = DEFAULT_FILE_BACKEND_MAX_BYTES,
        backup_count: int = DEFAULT_FILE_BACKEND_BACKUP_COUNT,
        **_: Any,
    ) -> None:
        """Configure the file backend.

        Args:
            path: Filesystem path of the output JSONL file.  Parent
                directories are created automatically.
            max_bytes: Maximum active JSONL size before rotation.
            backup_count: Number of rotated backups to retain.
            **_: Ignored extra keyword arguments.

        Raises:
            ValueError: If ``max_bytes`` or ``backup_count`` is not positive.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if backup_count <= 0:
            raise ValueError("backup_count must be positive")
        self._path = path
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        Path(path).resolve().parent.mkdir(parents=True, exist_ok=True)

    def send(self, records: list[LogRecord]) -> bool:
        """Append records to the configured file.

        Args:
            records: Batch of log records to write.

        Returns:
            True on success, False if the backend is unconfigured or an
            OSError occurs.
        """
        if not self._path:
            logger.warning("FileBackend not configured (no path set).")
            return False
        payload = "".join(rec.to_json() + "\n" for rec in records)
        try:
            with self._lock:
                path = Path(self._path)
                _rotate_jsonl_if_needed(
                    path,
                    len(payload.encode("utf-8")),
                    max_bytes=self._max_bytes,
                    backup_count=self._backup_count,
                )
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(payload)
            return True
        except OSError as exc:
            logger.error("FileBackend.send failed: %s", exc)
            return False

    def close(self) -> None:
        """Release resources held by this backend.

        File handles are opened and closed per ``send()`` call, so there is
        nothing to release here.
        """
        return None


def _rotate_jsonl_if_needed(path: Path, incoming_bytes: int, *, max_bytes: int, backup_count: int) -> None:
    """Rotate a dashboard JSONL log before the next append exceeds its cap."""
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        logger.warning("Exception handled by  rotate jsonl if needed fallback", exc_info=True)
        return
    if current_size + incoming_bytes <= max_bytes:
        return
    oldest = path.with_name(f"{path.name}.{backup_count}")
    if _path_exists_strict(oldest):
        oldest.unlink()
    for index in range(backup_count - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        if _path_exists_strict(source):
            source.replace(path.with_name(f"{path.name}.{index + 1}"))
    path.replace(path.with_name(f"{path.name}.1"))


def _path_exists_strict(path: Path) -> bool:
    """Return whether a path exists without hiding unreadable state."""
    try:
        path.stat()
    except FileNotFoundError:
        logger.warning("Exception handled by  path exists strict fallback", exc_info=True)
        return False
    return True


# ---------------------------------------------------------------------------
# Datadog backend
# ---------------------------------------------------------------------------


class DatadogBackend(BackendBase):
    """Sends records via the Datadog Logs Intake API."""

    name = "datadog"
    _DD_URL = DATADOG_LOGS_URL

    def __init__(self) -> None:
        self._api_key: str | None = None
        self._service: str = "vetinari"
        self._ddsource: str = "python"
        self._ddtags: str = ""
        self._logs_url: str = DATADOG_LOGS_URLS_BY_SITE["us1"]
        self._site: str = "us1"

    def configure(
        self,
        api_key: str = "",
        service: str = "vetinari",
        ddsource: str = "python",
        ddtags: str = "",
        site: str = "us1",
        endpoint_url: str | None = None,
        jurisdiction: str = "",
        allow_cross_border: bool = False,
        **_: Any,
    ) -> None:
        """Configure the Datadog backend.

        Args:
            api_key: Datadog API key for authentication.
            service: Service name tag applied to every log entry.
            ddsource: Source tag (language / integration name).
            ddtags: Comma-separated key:value tag string.
            site: Datadog site key controlling the regional intake endpoint.
            endpoint_url: Optional custom logs intake endpoint. Requires an
                explicit cross-border override when a jurisdiction is provided.
            jurisdiction: Data-residency family for log export requests.
            allow_cross_border: Explicit override for custom or mismatched
                external log shipping destinations.
            **_: Ignored extra keyword arguments.

        Raises:
            ValueError: If site or jurisdiction settings are unsupported.
        """
        normalized_site = site.strip().lower() or "us1"
        if normalized_site not in DATADOG_LOGS_URLS_BY_SITE:
            raise ValueError(f"Unsupported Datadog site: {site!r}")

        expected_family = DATADOG_SITE_JURISDICTIONS[normalized_site]
        requested_family = _jurisdiction_family(jurisdiction) if jurisdiction else expected_family
        if requested_family != expected_family and not allow_cross_border:
            raise ValueError(
                "Datadog jurisdiction mismatch: "
                f"{jurisdiction!r} requires {requested_family}, site {normalized_site!r} is {expected_family}"
            )
        if endpoint_url and jurisdiction and not allow_cross_border:
            raise ValueError("Custom Datadog endpoint requires explicit cross-border authorization.")

        self._api_key = api_key
        self._service = service
        self._ddsource = ddsource
        self._ddtags = ddtags
        self._site = normalized_site
        self._logs_url = endpoint_url or DATADOG_LOGS_URLS_BY_SITE[normalized_site]

    def send(self, records: list[LogRecord]) -> bool:
        """Send records to Datadog Logs Intake API.

        Args:
            records: Batch of log records to ship.

        Returns:
            True on success, False if the backend is unconfigured, the
            ``requests`` package is missing, or the HTTP call fails.
        """
        if not self._api_key:
            logger.warning("DatadogBackend not configured (api_key missing).")
            return False
        payload = [
            {
                "ddsource": self._ddsource,
                "ddtags": self._ddtags,
                "service": self._service,
                "message": rec.message,
                "status": rec.level,
                **rec.to_dict(),
            }
            for rec in records
        ]
        headers = {
            "DD-API-KEY": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            with create_session() as session:
                resp = session.post(
                    self._logs_url,
                    json=payload,
                    headers=headers,
                    timeout=LOG_BACKEND_TIMEOUT,
                )
            if resp.status_code not in (200, 202):
                logger.error(
                    "DatadogBackend received HTTP %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
            return True
        except Exception as exc:
            logger.error("DatadogBackend.send error: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Webhook backend (generic HTTP POST)
# ---------------------------------------------------------------------------


class WebhookBackend(BackendBase):
    """POST aggregated logs to an external webhook URL."""

    name = "webhook"

    def __init__(self) -> None:
        self._url: str | None = None
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        self._timeout: int = LOG_BACKEND_TIMEOUT

    def configure(
        self,
        url: str = "",
        headers: dict[str, str] | None = None,
        timeout: int = LOG_BACKEND_TIMEOUT,
        **_: Any,
    ) -> None:
        """Configure the webhook backend.

        Args:
            url: Target webhook URL.
            headers: Optional extra HTTP headers to merge into each request.
            timeout: Request timeout in seconds.
            **_: Ignored extra keyword arguments.
        """
        self._url = url
        if headers:
            self._headers.update(headers)
        self._timeout = timeout

    def send(self, records: list[LogRecord]) -> bool:
        """POST records as a JSON array to the configured webhook.

        Args:
            records: Batch of log records to ship.

        Returns:
            True on success, False if the backend is unconfigured, the
            ``requests`` package is missing, or the HTTP call fails.
        """
        if not self._url:
            logger.warning("WebhookBackend not configured (url missing).")
            return False
        payload = [rec.to_dict() for rec in records]
        try:
            with create_session() as session:
                resp = session.post(
                    self._url,
                    json=payload,
                    headers=self._headers,
                    timeout=self._timeout,
                )
            if not resp.ok:
                logger.error(
                    "WebhookBackend received HTTP %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
                return False
            return True
        except Exception as exc:
            logger.error("WebhookBackend.send error: %s", exc)
            return False


# ---------------------------------------------------------------------------
# SSE (Server-Sent Events) Backend — streams log records to connected clients
# ---------------------------------------------------------------------------


class SSEBackend(BackendBase):
    """Backend that buffers log records for Server-Sent Events (SSE) streaming.

    Dashboard clients connect via an SSE endpoint and receive real-time log
    updates. Records are kept in a bounded deque; older entries are discarded
    when the buffer fills.
    """

    name = "sse"

    def __init__(self) -> None:
        self._buffer: deque = deque(maxlen=LOG_BACKEND_BUFFER_SIZE)
        self._lock = threading.Lock()
        self._clients: list[queue.Queue] = []
        self._clients_lock = threading.Lock()

    def configure(self, max_buffer: int = LOG_BACKEND_BUFFER_SIZE, **_: Any) -> None:
        """Configure the SSE backend.

        Args:
            max_buffer: Maximum number of log records to retain in memory.
            **_: Ignored extra keyword arguments.
        """
        with self._lock:
            self._buffer = deque(maxlen=max_buffer)

    def add_client(self, q: queue.Queue) -> None:
        """Register a new SSE client queue for real-time log delivery.

        Args:
            q: Queue that will receive serialised log records as they arrive.
        """
        with self._clients_lock:
            self._clients.append(q)

    def remove_client(self, q: queue.Queue) -> None:
        """Unregister a previously-registered SSE client queue.

        Args:
            q: The queue to remove from the client list.
        """
        import contextlib

        with self._clients_lock, contextlib.suppress(ValueError):
            self._clients.remove(q)

    def send(self, records: list[LogRecord]) -> bool:
        """Buffer records for SSE consumption and forward to connected clients.

        Args:
            records: Batch of log records to buffer.

        Returns:
            Always True — buffering does not fail.
        """
        with self._lock:
            for rec in records:
                self._buffer.append(rec)
        # Fan out to connected SSE clients.
        # Use rec.to_json() — json.dumps(dataclass) with default=str produces a
        # repr blob rather than a proper JSON object, so the SSE consumer cannot
        # parse it.
        with self._clients_lock:
            dead: list[queue.Queue] = []
            for client_q in self._clients:
                for rec in records:
                    try:
                        client_q.put_nowait(rec.to_json())
                    except Exception:
                        dead.append(client_q)
                        break
            import contextlib

            for dq in dead:
                with contextlib.suppress(ValueError):
                    self._clients.remove(dq)
        return True

    def get_recent(self, limit: int = 50) -> list[LogRecord]:
        """Return the most recent records (newest last).

        Args:
            limit: Maximum number of records to return.

        Returns:
            Up to ``limit`` most recent log records, ordered oldest-first.
        """
        with self._lock:
            items = list(self._buffer)
        return items[-limit:]

    def close(self) -> None:
        """Discard all buffered records and disconnect all SSE clients.

        Clears both the in-memory record buffer and the connected client queue
        list so that subsequent calls to send() and get_recent() return empty
        results and no stale queue references are held after shutdown.
        """
        with self._lock:
            self._buffer.clear()
        # Clear the client list under its own lock so in-flight send() calls
        # that hold _clients_lock see a consistent empty list after close().
        with self._clients_lock:
            self._clients.clear()


# ---------------------------------------------------------------------------
# SSE singleton helpers
# ---------------------------------------------------------------------------

_sse_backend_instance: SSEBackend | None = None
_sse_lock = threading.Lock()


def get_sse_backend() -> SSEBackend:
    """Return the global SSEBackend singleton.

    Returns:
        The process-wide SSEBackend instance, created on first call.
    """
    global _sse_backend_instance
    if _sse_backend_instance is None:
        with _sse_lock:
            if _sse_backend_instance is None:
                _sse_backend_instance = SSEBackend()
    return _sse_backend_instance


def reset_sse_backend() -> None:
    """Destroy the SSEBackend singleton (for tests / clean shutdown)."""
    global _sse_backend_instance
    with _sse_lock:
        if _sse_backend_instance is not None:
            _sse_backend_instance.close()
        _sse_backend_instance = None


def webhook_trigger_backend_snapshot() -> dict[str, int]:
    """Snapshot of per-event webhook trigger counts (FSA-0396).

    Thin pass-through to ``vetinari.notifications.webhook.webhook_event_trigger_counts``
    that lets dashboard backends and operator-status routes surface how
    many times each event has fired without importing the registry directly.

    Returns:
        Mapping of event name to trigger count.  Empty dict when no events
        have fired (or when ``clear_webhook_event_callbacks`` has just run).
    """
    from vetinari.notifications.webhook import webhook_event_trigger_counts

    return webhook_event_trigger_counts()
