"""Notification Manager — central dispatcher routing to all configured channels.

Priority tiers determine routing:
  - CRITICAL: ALL channels immediately (security alert, budget breach, Andon pull)
  - HIGH: Desktop + dashboard immediately (approval needed, task failed)
  - MEDIUM: Dashboard immediately, webhook batched hourly (task completed)
  - LOW: Dashboard badge only (routine completions, metrics)

Includes semantic batching: groups related notifications by type within a
configurable time window to avoid notification spam.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from vetinari.types import NotificationPriority
from vetinari.ux import display_label, display_label_or_humanize

logger = logging.getLogger(__name__)


# Semantic batching window in seconds
_BATCH_WINDOW_SECONDS = 60.0

# Channel routing by priority tier
_PRIORITY_ROUTING: dict[NotificationPriority, list[str]] = {
    NotificationPriority.CRITICAL: ["dashboard", "desktop", "webhook"],
    NotificationPriority.HIGH: ["dashboard", "desktop"],
    NotificationPriority.MEDIUM: ["dashboard", "webhook"],
    NotificationPriority.LOW: ["dashboard_badge"],
}

# Channels that receive batched delivery for MEDIUM priority
_BATCHED_CHANNELS: frozenset[str] = frozenset({"webhook"})


@dataclass(frozen=True, slots=True)
class Notification:
    """A notification ready for dispatch.

    Args:
        notification_id: Unique identifier.
        title: Short headline.
        body: Detailed message content.
        priority: Routing priority tier.
        action_type: What triggered this notification.
        metadata: Additional context for channel-specific formatting.
        created_at: When the notification was created (ISO 8601 UTC).
    """

    notification_id: str
    title: str
    body: str
    priority: NotificationPriority
    action_type: str
    metadata: dict[str, Any]
    created_at: str

    def __repr__(self) -> str:
        return (
            f"Notification(id={self.notification_id!r}, priority={self.priority.value!r}, action={self.action_type!r})"
        )


# Type for channel handler: receives a list of notifications to deliver.
# Returning False is an explicit negative acknowledgement.
ChannelHandler = Callable[[list[Notification]], bool | None]


class NotificationManager:
    """Central notification dispatcher with priority-based channel routing.

    Channels register via ``register_channel(name, handler)``.  When
    ``notify()`` is called, the notification is routed to the appropriate
    channels based on its priority tier.

    Side effects in __init__:
      - Starts a background timer thread for batch flushing (daemon=True)
    """

    def __init__(self, batch_flush_interval_s: float = _BATCH_WINDOW_SECONDS, autostart: bool = True) -> None:
        self._lock = threading.Lock()
        self._channels: dict[str, ChannelHandler] = {}
        self._batch_buffer: dict[str, list[Notification]] = defaultdict(list)
        self._last_flush: float = time.monotonic()
        self._delivery_log: list[dict[str, Any]] = []
        self._batch_flush_interval_s = max(float(batch_flush_interval_s), 0.01)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.Lock()
        if autostart:
            self.start()

    def start(self) -> None:
        """Start the daemon batch flush worker."""
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_flush_worker,
                name="notification-batch-flush",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        """Stop the daemon batch flush worker and drain pending batches."""
        self._stop_event.set()
        with self._lifecycle_lock:
            if self._thread is not None:
                self._thread.join(timeout=self._batch_flush_interval_s + 2)
                self._thread = None
        self.flush_batches()

    def close(self) -> None:
        """Alias for stop() for callers that use resource-style cleanup."""
        self.stop()

    def _run_flush_worker(self) -> None:
        while not self._stop_event.wait(timeout=self._batch_flush_interval_s):
            try:
                self.flush_batches()
            except Exception:
                logger.warning("Notification batch flush worker failed; will retry next interval", exc_info=True)

    def register_channel(self, name: str, handler: ChannelHandler) -> None:
        """Register a notification channel.

        Args:
            name: Channel identifier (e.g. ``"dashboard"``, ``"desktop"``, ``"webhook"``).
            handler: Callable that receives a list of Notification objects to deliver.
        """
        with self._lock:
            self._channels[name] = handler
        logger.info("Registered notification channel: %s", name)

    def unregister_channel(self, name: str) -> None:
        """Remove a notification channel.

        Args:
            name: Channel identifier to remove.
        """
        with self._lock:
            self._channels.pop(name, None)
        logger.info("Unregistered notification channel: %s", name)

    @staticmethod
    def _deliver_to_channel(
        channel_name: str,
        handler: ChannelHandler,
        notifications: list[Notification],
    ) -> tuple[bool, dict[str, str] | None]:
        """Deliver notifications to one channel and normalize failure state."""
        try:
            accepted = handler(notifications)
        except Exception as exc:
            logger.warning(
                "Failed to deliver %d notification(s) to channel %s; recovery required before retry",
                len(notifications),
                channel_name,
                exc_info=True,
            )
            return False, {
                "channel": channel_name,
                "reason": exc.__class__.__name__,
                "recovery_action": "inspect channel runtime logs and retry delivery",
            }
        if accepted is False:
            logger.warning(
                "Notification channel %s returned negative acknowledgement; delivery not marked successful",
                channel_name,
            )
            return False, {
                "channel": channel_name,
                "reason": "negative_ack",
                "recovery_action": "check channel configuration and retry delivery",
            }
        return True, None

    def notify(
        self,
        title: str,
        body: str,
        priority: NotificationPriority,
        action_type: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create and dispatch a notification to appropriate channels.

        Args:
            title: Short headline for the notification.
            body: Detailed message content.
            priority: Routing priority tier.
            action_type: What triggered this notification (for filtering/grouping).
            metadata: Additional context for channel-specific formatting.

        Returns:
            The notification_id for tracking.
        """
        notification = Notification(
            notification_id=f"ntf_{uuid.uuid4().hex[:12]}",
            title=title,
            body=body,
            priority=priority,
            action_type=action_type,
            metadata=metadata or {},
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        target_channels = _PRIORITY_ROUTING.get(priority, ["dashboard"])
        delivered_to: list[str] = []
        delivery_failures: list[dict[str, str]] = []

        with self._lock:
            for channel_name in target_channels:
                if channel_name not in self._channels:
                    continue

                # Batch MEDIUM-priority notifications for webhook channels
                if priority == NotificationPriority.MEDIUM and channel_name in _BATCHED_CHANNELS:
                    self._batch_buffer[channel_name].append(notification)
                    continue

                handler = self._channels[channel_name]
                delivered, failure = self._deliver_to_channel(channel_name, handler, [notification])
                if delivered:
                    delivered_to.append(channel_name)
                elif failure is not None:
                    delivery_failures.append(failure)

            # Record delivery
            self._delivery_log.append({
                "notification_id": notification.notification_id,
                "priority": priority.value,
                "priority_label": display_label(priority),
                "action_type": action_type,
                "action_label": display_label_or_humanize(action_type) if action_type else "",
                "delivered_to": delivered_to,
                "failed_channels": [failure["channel"] for failure in delivery_failures],
                "delivery_failures": delivery_failures,
                "created_at": notification.created_at,
            })

        logger.info(
            "Notification %s (%s) dispatched to %s",
            notification.notification_id,
            priority.value,
            ", ".join(delivered_to) if delivered_to else "no channels",
        )
        return notification.notification_id

    def flush_batches(self) -> int:
        """Flush all batched notifications to their channels.

        Called periodically by the scheduler or manually. Delivers
        accumulated MEDIUM-priority notifications to batched channels.

        Returns:
            Number of notifications flushed.
        """
        total_flushed = 0
        with self._lock:
            delivered_channels: list[str] = []
            for channel_name, notifications in self._batch_buffer.items():
                if not notifications:
                    delivered_channels.append(channel_name)  # empty — nothing to retry
                    continue
                handler = self._channels.get(channel_name)
                if handler is None:
                    logger.warning(
                        "No handler registered for batched notification channel %s; retaining buffer",
                        channel_name,
                    )
                    continue

                # Semantic batching: group by action_type
                batched = self._semantic_batch(notifications)
                delivered, _failure = self._deliver_to_channel(channel_name, handler, batched)
                if delivered:
                    total_flushed += len(notifications)
                    delivered_channels.append(channel_name)
                else:
                    logger.warning(
                        "Failed to flush %d batched notifications to %s; retaining in buffer for next flush",
                        len(notifications),
                        channel_name,
                    )
                    # Do NOT add to delivered_channels — buffer retained for retry

            # Only clear channels that succeeded or were empty.
            # Failed channels keep their buffered notifications for the next flush.
            for ch in delivered_channels:
                self._batch_buffer.pop(ch, None)
            self._last_flush = time.monotonic()

        if total_flushed > 0:
            logger.info("Flushed %d batched notifications", total_flushed)
        return total_flushed

    @staticmethod
    def _semantic_batch(notifications: list[Notification]) -> list[Notification]:
        """Group related notifications into summary notifications.

        Groups by action_type and replaces clusters of 3+ with a single
        summary notification (e.g., "3 prompts optimized" instead of 3 separate).

        Args:
            notifications: Raw notification list.

        Returns:
            Deduplicated/batched notification list.
        """
        if len(notifications) <= 2:
            return notifications

        groups: dict[str, list[Notification]] = defaultdict(list)
        for n in notifications:
            groups[n.action_type].append(n)

        result: list[Notification] = []
        for action_type, group in groups.items():
            if len(group) < 3:
                result.extend(group)
            else:
                # Create a summary notification
                summary = Notification(
                    notification_id=f"ntf_{uuid.uuid4().hex[:12]}",
                    title=f"{len(group)} {display_label_or_humanize(action_type)} actions completed",
                    body=f"Batched summary of {len(group)} notifications",
                    priority=group[0].priority,
                    action_type=action_type,
                    metadata={"batch_count": len(group), "batched_ids": [n.notification_id for n in group]},
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                result.append(summary)
        return result

    def get_delivery_log(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent delivery log entries.

        Args:
            limit: Maximum entries to return.

        Returns:
            List of delivery records, most recent first.
        """
        with self._lock:
            return list(reversed(self._delivery_log[-limit:]))

    @property
    def registered_channels(self) -> list[str]:
        """Names of all currently registered channels."""
        with self._lock:
            return list(self._channels.keys())


# -- Singleton ----------------------------------------------------------------

_manager: NotificationManager | None = None
_manager_lock = threading.Lock()


def get_notification_manager() -> NotificationManager:
    """Get or create the singleton NotificationManager.

    Returns:
        The singleton NotificationManager instance.
    """
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = NotificationManager()
    return _manager


def reset_notification_manager() -> None:
    """Stop and clear the process-wide notification manager."""
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.stop()
            _manager = None
