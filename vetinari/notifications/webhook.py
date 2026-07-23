"""Webhook Notifications — Discord, Slack, and generic HTTP delivery.

Delivers notifications to configured webhook endpoints with format-specific
payloads (Discord rich embed, Slack block kit, generic JSON POST).
Includes retry with exponential backoff and per-webhook event filtering.

Configuration loaded from ``config/notifications.yaml``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import yaml

from vetinari.config_paths import resolve_config_path
from vetinari.constants import MAX_RETRIES, RETRY_BASE_DELAY
from vetinari.security.fail_closed import assert_closed_schema, sanitize_untrusted_text
from vetinari.security.redaction import redact_text, redact_value

logger = logging.getLogger(__name__)


_DEFAULT_CONFIG_PATH = resolve_config_path("notifications.yaml")


def _redact_url(url: str) -> str:
    """Return a webhook URL safe for logs and health responses."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return "***"
    return f"{parsed.scheme}://{parsed.netloc}/***"


def _redact_payload_value(value: Any) -> Any:
    """Redact notification content before it leaves the local process."""
    return redact_value(value)


def _redact_payload_text(value: object) -> str:
    """Return webhook text with secrets, provider URLs, and host paths removed."""
    return redact_text(str(value))


# Valid webhook payload format identifiers
_VALID_FORMATS: frozenset[str] = frozenset({"discord", "slack", "generic"})

# Valid notification event names that webhooks may subscribe to
_VALID_EVENTS: frozenset[str] = frozenset({
    "task_completed",
    "training_completed",
    "approval_needed",
    "error",
    "quality_alert",
    "daily_digest",
    "trust_promotion",
    "cost_alert",
    "security_alert",
    "build_complete",
})

if TYPE_CHECKING:
    from vetinari.notifications.manager import Notification


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    """Configuration for a single webhook endpoint.

    Args:
        url: The webhook URL.
        format: Payload format (``"discord"``, ``"slack"``, or ``"generic"``).
        events: List of event types to deliver (empty = all events).
        enabled: Whether this webhook is active.
    """

    url: str
    format: str = "generic"  # discord | slack | generic
    events: list[str] | None = None  # None = all events
    enabled: bool = True

    def __repr__(self) -> str:
        return "WebhookConfig(...)"


class WebhookNotifier:
    """Webhook notification channel with format-specific payloads and retry.

    Loads webhook configurations from YAML and delivers notifications
    via HTTP POST with exponential backoff on failure.

    Side effects in __init__:
      - Reads ``config/notifications.yaml`` from disk
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self._config_path = config_path or _DEFAULT_CONFIG_PATH
        self._webhooks: list[WebhookConfig] = []
        self._lock = threading.Lock()
        self._health: dict[str, dict[str, int]] = {}  # url -> {successes, failures}
        self._load_config()

    def _load_config(self) -> None:
        """Load webhook configurations from YAML file."""
        if not self._config_path.exists():
            logger.info(
                "Webhook config not found at %s — no webhooks configured",
                self._config_path,
            )
            return

        try:
            raw = self._config_path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw) or {}
            webhook_defs = data.get("webhooks", [])
            for entry in webhook_defs:
                if not isinstance(entry, dict) or "url" not in entry:
                    raise ValueError("webhook entry must be an object with a url")
                assert_closed_schema(
                    entry,
                    allowed_keys={"url", "format", "events", "enabled"},
                    required_keys={"url"},
                )

                fmt = sanitize_untrusted_text(entry.get("format", "generic"), max_length=40)
                if fmt not in _VALID_FORMATS:
                    logger.warning(
                        "Webhook for %s has unknown format %r — skipping. Valid formats: %s",
                        _redact_url(entry["url"]),
                        fmt,
                        ", ".join(sorted(_VALID_FORMATS)),
                    )
                    raise ValueError(f"unknown webhook format: {fmt}")

                raw_events: list[str] | None = entry.get("events")
                if raw_events is not None:
                    raw_events = [sanitize_untrusted_text(e, max_length=80) for e in raw_events]
                    unknown = [e for e in raw_events if e not in _VALID_EVENTS]
                    if unknown:
                        logger.warning(
                            "Webhook for %s has unknown event names %r — skipping. Valid events: %s",
                            _redact_url(entry["url"]),
                            unknown,
                            ", ".join(sorted(_VALID_EVENTS)),
                        )
                        continue

                self._webhooks.append(
                    WebhookConfig(
                        url=sanitize_untrusted_text(entry["url"], max_length=2_000),
                        format=fmt,
                        events=raw_events,
                        enabled=entry.get("enabled", True),
                    )
                )
                self._health[entry["url"]] = {"successes": 0, "failures": 0}

            logger.info("Loaded %d webhook configurations", len(self._webhooks))
        except Exception as exc:
            logger.warning(
                "Failed to load webhook config from %s — no webhooks will be active",
                self._config_path,
            )

            raise ValueError(f"invalid webhook config: {self._config_path}") from exc

    def deliver(self, notifications: list[Notification]) -> bool:
        """Deliver notifications to all configured webhooks.

        Each webhook receives only events matching its event filter.
        Delivery uses retry with exponential backoff.

        Args:
            notifications: List of Notification objects to deliver.

        Returns:
            True when every attempted matching webhook delivery succeeds,
            False when any matching webhook delivery fails after retries.
        """
        failed = False
        for webhook in self._webhooks:
            if not webhook.enabled:
                continue

            # Filter notifications by webhook's event subscription
            filtered = self._filter_by_events(notifications, webhook)
            if not filtered:
                continue

            payload = self._format_payload(filtered, webhook.format)
            if not self._send_with_retry(webhook.url, payload):
                failed = True
        return not failed

    @staticmethod
    def _filter_by_events(
        notifications: list[Notification],
        webhook: WebhookConfig,
    ) -> list[Notification]:
        """Filter notifications to only those matching the webhook's event list.

        Both ``events=None`` and ``events=[]`` mean "all events" — the empty
        list is treated as "no filter" rather than "no events allowed", which
        matches the documented contract on ``WebhookConfig``.
        """
        if not webhook.events:  # None or [] both mean "all events"
            return notifications
        return [n for n in notifications if n.action_type in webhook.events]

    def _format_payload(self, notifications: list[Notification], fmt: str) -> dict[str, Any]:
        """Format notification payload for the target platform.

        Args:
            notifications: Notifications to format.
            fmt: Target format (``"discord"``, ``"slack"``, or ``"generic"``).

        Returns:
            Platform-specific payload dict.
        """
        if fmt == "discord":
            return self._format_discord(notifications)
        if fmt == "slack":
            return self._format_slack(notifications)
        return self._format_generic(notifications)

    @staticmethod
    def _format_discord(notifications: list[Notification]) -> dict[str, Any]:
        """Format as Discord rich embed."""
        embeds = []
        for n in notifications[:10]:  # Discord limit: 10 embeds per message
            color_map = {"critical": 0xFF0000, "high": 0xFF8C00, "medium": 0x3498DB, "low": 0x95A5A6}
            embeds.append({
                "title": _redact_payload_text(n.title),
                "description": _redact_payload_text(n.body),
                "color": color_map.get(n.priority.value, 0x95A5A6),
                "footer": {"text": f"Vetinari | {n.action_type}"},
                "timestamp": n.created_at,
            })
        return {"embeds": embeds}

    @staticmethod
    def _format_slack(notifications: list[Notification]) -> dict[str, Any]:
        """Format as Slack block kit message."""
        blocks = []
        for n in notifications:
            emoji_map = {
                "critical": ":rotating_light:",
                "high": ":warning:",
                "medium": ":information_source:",
                "low": ":memo:",
            }
            emoji = emoji_map.get(n.priority.value, ":memo:")
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{emoji} *{_redact_payload_text(n.title)}*\n{_redact_payload_text(n.body)}",
                },
            })
        return {"blocks": blocks}

    @staticmethod
    def _format_generic(notifications: list[Notification]) -> dict[str, Any]:
        """Format as generic JSON POST body."""
        return {
            "source": "vetinari",
            "notifications": [
                {
                    "id": n.notification_id,
                    "title": _redact_payload_text(n.title),
                    "body": _redact_payload_text(n.body),
                    "priority": n.priority.value,
                    "action_type": n.action_type,
                    "metadata": _redact_payload_value(n.metadata),
                    "created_at": n.created_at,
                }
                for n in notifications
            ],
        }

    def _send_with_retry(self, url: str, payload: dict[str, Any]) -> bool:
        """Send payload to webhook URL with exponential backoff retry.

        Args:
            url: The webhook endpoint URL.
            payload: The formatted payload dict.

        Returns:
            True if delivery succeeded, False after all retries exhausted.
        """
        for attempt in range(MAX_RETRIES):
            try:
                import httpx

                with httpx.Client(timeout=10.0) as client:
                    response = client.post(url, json=payload)
                    response.raise_for_status()

                with self._lock:
                    if url in self._health:
                        self._health[url]["successes"] += 1
                return True

            except ImportError:
                logger.warning("httpx not installed — webhook delivery disabled. Install with: pip install httpx")
                return False

            except Exception:
                delay = RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "Webhook delivery to %s failed (attempt %d/%d) — retrying in %.1fs",
                    _redact_url(url),
                    attempt + 1,
                    MAX_RETRIES,
                    delay,
                )
                if attempt < MAX_RETRIES - 1:
                    time.sleep(delay)

        with self._lock:
            if url in self._health:
                self._health[url]["failures"] += 1

        logger.warning(
            "Webhook delivery to %s failed after %d retries — notification dropped",
            _redact_url(url),
            MAX_RETRIES,
        )
        return False

    def get_health(self) -> dict[str, dict[str, int]]:
        """Return per-webhook success/failure counts.

        Returns:
            Dict mapping webhook URLs to their delivery health stats.
        """
        with self._lock:
            return {_redact_url(url): dict(stats) for url, stats in self._health.items()}


def create_webhook_channel(config_path: Path | None = None) -> WebhookNotifier | None:
    """Create and register the webhook notification channel.

    Args:
        config_path: Optional override for YAML config path.

    Returns:
        The WebhookNotifier instance, or None if no webhooks configured.
    """
    try:
        notifier = WebhookNotifier(config_path=config_path)
    except ValueError:
        logger.warning("Webhook channel disabled because config failed validation", exc_info=True)
        return None
    if not notifier._webhooks:
        return None

    try:
        from vetinari.notifications.manager import get_notification_manager

        get_notification_manager().register_channel("webhook", notifier.deliver)
    except Exception:
        logger.warning("Failed to register webhook channel with notification manager")

    return notifier


# ---------------------------------------------------------------------------
# Event callback registry (FSA-0396)
# ---------------------------------------------------------------------------
#
# A lightweight in-process pub/sub so dashboard backends, log forwarders, and
# integration tests can subscribe to lifecycle events ("task_completed",
# "build_failed", ...) without coupling to the WebhookNotifier delivery
# pipeline.  Trigger counts surface through
# vetinari.dashboard.log_backends.webhook_trigger_backend_snapshot().

_event_callbacks_lock = threading.RLock()
_event_callbacks: dict[str, list[Callable[[str, list[Any]], None]]] = defaultdict(list)
_event_trigger_counts: dict[str, int] = defaultdict(int)


def register_webhook_event_callback(event_name: str, callback: Callable[[str, list[Any]], None]) -> None:
    """Register *callback* to fire whenever *event_name* is triggered.

    The callback receives the event name and the list of notification
    payloads supplied to ``trigger_webhook_event``.  Multiple callbacks
    per event name are supported and fire in registration order.

    Args:
            event_name: Logical event id (e.g. ``"task_completed"``).
            callback: Function ``(event_name, notifications) -> None``.

    Raises:
            ValueError: If the event name is not registered.
            UntrustedInputError: If the event name is unsafe text.
    """
    event_name = sanitize_untrusted_text(event_name, max_length=120)
    if event_name not in _VALID_EVENTS:
        raise ValueError(f"unknown webhook event: {event_name}")
    with _event_callbacks_lock:
        _event_callbacks[event_name].append(callback)


def trigger_webhook_event(event_name: str, notifications: list[Any]) -> int:
    """Invoke every registered callback for *event_name*.

    Callback exceptions are logged at WARNING and do NOT abort the
    remaining callbacks — one bad subscriber must not silence the others.

    Args:
        event_name: Event id to fire.
        notifications: Payload list forwarded to each callback.

    Returns:
        Number of callbacks invoked (zero when none registered).

    Raises:
        ValueError: If the event name is not registered.
        UntrustedInputError: If the event name is unsafe text.
    """
    event_name = sanitize_untrusted_text(event_name, max_length=120)
    if event_name not in _VALID_EVENTS:
        raise ValueError(f"unknown webhook event: {event_name}")
    with _event_callbacks_lock:
        callbacks = list(_event_callbacks.get(event_name, ()))
        _event_trigger_counts[event_name] += 1
    invoked = 0
    for callback in callbacks:
        try:
            callback(event_name, notifications)
            invoked += 1
        except Exception:
            logger.warning(
                "Webhook event callback for %r raised — continuing with remaining subscribers",
                event_name,
                exc_info=True,
            )
    return invoked


def clear_webhook_event_callbacks() -> None:
    """Reset every registered callback and trigger counter.

    Provided for tests and clean shutdown — production code should not
    routinely clear the registry.
    """
    with _event_callbacks_lock:
        _event_callbacks.clear()
        _event_trigger_counts.clear()


def webhook_event_trigger_counts() -> dict[str, int]:
    """Return a snapshot of how many times each event has been triggered.

    Used by ``vetinari.dashboard.log_backends.webhook_trigger_backend_snapshot``
    to expose per-event totals to operator dashboards.

    Returns:
        Mapping of event name to trigger count.  An empty dict when no
        events have fired since the last reset.
    """
    with _event_callbacks_lock:
        return dict(_event_trigger_counts)
