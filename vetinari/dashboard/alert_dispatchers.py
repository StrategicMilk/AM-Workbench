"""Dashboard alert dispatch channels."""

from __future__ import annotations

import logging
import os
import smtplib
import time
from collections.abc import Callable
from email.mime.text import MIMEText

from vetinari.constants import ALERT_SEND_TIMEOUT
from vetinari.dashboard.alert_types import AlertRecord, AlertSeverity
from vetinari.http import create_session
from vetinari.security.redaction import REDACTED_URL, redact_text

logger = logging.getLogger(__name__)

_WEBHOOK_MAX_ATTEMPTS = 3
_WEBHOOK_BACKOFF_BASE = 1.0


def _dispatch_log(alert: AlertRecord) -> None:
    """Emit an alert to the Python logger."""
    level = logging.ERROR if alert.threshold.severity == AlertSeverity.HIGH else logging.WARNING
    _alerts_logger().log(
        level,
        "ALERT [%s] %s: %s %.4g %s %.4g",
        alert.threshold.severity.value.upper(),
        alert.threshold.name,
        alert.threshold.metric_key,
        alert.current_value,
        alert.threshold.condition.value,
        alert.threshold.threshold_value,
        extra={"alert": alert.to_dict()},
    )


def _dispatch_email(alert: AlertRecord) -> None:
    """Send alert via SMTP email."""
    smtp_host = os.environ.get("VETINARI_SMTP_HOST")
    smtp_port = int(os.environ.get("VETINARI_SMTP_PORT", "587"))
    from_addr = os.environ.get("VETINARI_ALERT_FROM")
    to_addr = os.environ.get("VETINARI_ALERT_TO")
    if not smtp_host or not from_addr or not to_addr:
        _alerts_logger().info(
            "EMAIL: skipping alert '%s' - VETINARI_SMTP_HOST, VETINARI_ALERT_FROM, "
            "and VETINARI_ALERT_TO must all be set",
            alert.threshold.name,
        )
        return

    msg = MIMEText(
        f"Alert: {alert.threshold.name}\n"
        f"Severity: {alert.threshold.severity.value.upper()}\n"
        f"Metric: {alert.threshold.metric_key}\n"
        f"Current value: {alert.current_value:.4g}\n"
        f"Threshold ({alert.threshold.condition.value}): {alert.threshold.threshold_value:.4g}\n"
        f"Triggered at: {alert.trigger_time}\n"
    )
    msg["Subject"] = f"[Vetinari Alert] [{alert.threshold.severity.value.upper()}] {alert.threshold.name}"
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            smtp_user = os.environ.get("VETINARI_SMTP_USER")
            smtp_pass = os.environ.get("VETINARI_SMTP_PASS")
            if smtp_user and smtp_pass:
                server.login(smtp_user, smtp_pass)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        _alerts_logger().info("EMAIL: sent alert '%s' to %s", alert.threshold.name, to_addr)
    except Exception:
        _alerts_logger().exception("EMAIL: failed to send alert '%s'", alert.threshold.name)


def _dispatch_webhook(alert: AlertRecord) -> None:
    """POST alert details as JSON with retries and exponential backoff."""
    url = os.environ.get("VETINARI_WEBHOOK_URL")
    if not url:
        _alerts_logger().info("WEBHOOK: skipping alert '%s' - VETINARI_WEBHOOK_URL not set", alert.threshold.name)
        return

    payload = {
        "name": alert.threshold.name,
        "metric": alert.threshold.metric_key,
        "value": alert.current_value,
        "threshold": alert.threshold.threshold_value,
        "severity": alert.threshold.severity.value,
        "timestamp": alert.trigger_time,
    }
    last_exc: Exception | None = None
    for attempt in range(1, _WEBHOOK_MAX_ATTEMPTS + 1):
        try:
            with _create_session() as session:
                response = session.post(url, json=payload, timeout=ALERT_SEND_TIMEOUT)
            response.raise_for_status()
            _alerts_logger().info(
                "WEBHOOK: posted alert '%s' to %s (status %s, attempt %d/%d)",
                alert.threshold.name,
                REDACTED_URL,
                response.status_code,
                attempt,
                _WEBHOOK_MAX_ATTEMPTS,
            )
            return
        except Exception as exc:
            last_exc = exc
            if attempt < _WEBHOOK_MAX_ATTEMPTS:
                delay = _WEBHOOK_BACKOFF_BASE * (2 ** (attempt - 1))
                _alerts_logger().warning(
                    "WEBHOOK: attempt %d/%d failed for alert '%s': %s - retrying in %.1fs",
                    attempt,
                    _WEBHOOK_MAX_ATTEMPTS,
                    alert.threshold.name,
                    exc,
                    delay,
                )
                time.sleep(delay)

    _alerts_logger().error(
        "WEBHOOK: all %d attempts failed for alert '%s' to %s: %s",
        _WEBHOOK_MAX_ATTEMPTS,
        alert.threshold.name,
        REDACTED_URL,
        redact_text(str(last_exc)),
    )


def _dispatch_dashboard(alert: AlertRecord) -> None:
    """Deliver an alert to the dashboard notification manager."""
    try:
        from vetinari.notifications.manager import get_notification_manager
        from vetinari.types import NotificationPriority

        severity_label = alert.threshold.severity.value.upper()
        priority = (
            NotificationPriority.CRITICAL
            if alert.threshold.severity == AlertSeverity.HIGH
            else NotificationPriority.HIGH
        )
        get_notification_manager().notify(
            title=f"[{severity_label}] {alert.threshold.name}",
            body=(
                f"Metric '{alert.threshold.metric_key}' is {alert.current_value:.4g} "
                f"({alert.threshold.condition.value} {alert.threshold.threshold_value:.4g})"
            ),
            priority=priority,
            action_type="alert",
            metadata=alert.to_dict(),
        )
        _alerts_logger().debug("DASHBOARD: dispatched alert '%s' via NotificationManager", alert.threshold.name)
    except Exception as exc:
        _alerts_logger().warning(
            "DASHBOARD: could not dispatch alert '%s' to NotificationManager - dashboard notification skipped: %s",
            alert.threshold.name,
            exc,
        )


DISPATCHERS: dict[str, Callable[[AlertRecord], None]] = {
    "log": _dispatch_log,
    "email": _dispatch_email,
    "webhook": _dispatch_webhook,
    "dashboard": _dispatch_dashboard,
}


def _alerts_logger() -> logging.Logger:
    """Return the public alerts-module logger for existing patch seams."""
    try:
        from vetinari.dashboard import alerts
    except ImportError:
        logger.warning("Exception handled by  alerts logger fallback", exc_info=True)
        return logger
    return alerts.logger


def _create_session() -> object:
    """Return the public alerts-module HTTP session factory for tests."""
    try:
        from vetinari.dashboard import alerts
    except ImportError:
        logger.warning("Exception handled by  create session fallback", exc_info=True)
        return create_session()
    return alerts.create_session()
