"""Alert System for Vetinari Dashboard.

Provides threshold-based alerting on top of the MetricsSnapshot produced by DashboardAPI.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from vetinari.constants import ALERT_SEND_TIMEOUT
from vetinari.dashboard.alert_defaults import DEFAULT_ALERT_THRESHOLDS
from vetinari.dashboard.alert_dispatchers import (
    DISPATCHERS,
    _dispatch_dashboard,
    _dispatch_email,
    _dispatch_log,
    _dispatch_webhook,
)
from vetinari.dashboard.alert_resolution import _resolve_metric
from vetinari.dashboard.alert_types import AlertCondition, AlertRecord, AlertSeverity, AlertThreshold
from vetinari.dashboard.api import DashboardAPI, MetricsSnapshot, get_dashboard_api
from vetinari.database import get_connection
from vetinari.http import create_session as _create_session_factory

logger = logging.getLogger(__name__)


def create_session() -> Any:
    """Return an HTTP session for webhook dispatch patch compatibility."""
    return _create_session_factory()


class AlertEngine:
    """Evaluates registered AlertThreshold rules against live metrics."""

    _instance: AlertEngine | None = None
    _class_lock = threading.Lock()

    def __new__(cls) -> AlertEngine:
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    def _setup(self) -> None:
        self._lock = threading.RLock()
        self._thresholds: dict[str, AlertThreshold] = {}
        self._active: dict[str, AlertRecord] = {}
        self._history: deque[AlertRecord] = deque(maxlen=500)
        self._duration_start: dict[str, float] = {}
        self._custom_db_path: Path | None = None
        self._history_table_initialized = False
        self._register_default_thresholds()

    def _register_default_thresholds(self) -> None:
        """Install always-on alert rules for SEV1-plausible runtime failures."""
        for threshold in DEFAULT_ALERT_THRESHOLDS:
            self._thresholds[threshold.name] = threshold

    def configure_persistence(self, db_path: Path | str | None = None) -> None:
        """Enable SQLite-backed alert history persistence.

        Args:
            db_path: Path to a specific SQLite database file, or None to use
                the unified database.
        """
        self._custom_db_path = Path(db_path) if db_path is not None else None
        if self._custom_db_path is not None:
            self._custom_db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._history_table_initialized = True
        self._load_history()

    def _init_db(self) -> None:
        """Create the alert_history table if it does not exist."""
        try:
            if self._custom_db_path is not None:
                import contextlib
                import sqlite3 as _sqlite3

                with contextlib.closing(_sqlite3.connect(str(self._custom_db_path))) as conn:
                    conn.execute(
                        """CREATE TABLE IF NOT EXISTS alert_history (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            threshold_name TEXT NOT NULL,
                            metric_key TEXT NOT NULL,
                            condition TEXT NOT NULL,
                            threshold_value REAL NOT NULL,
                            severity TEXT NOT NULL,
                            channels TEXT NOT NULL,
                            current_value REAL,
                            trigger_time REAL NOT NULL
                        )"""
                    )
                    conn.commit()
            else:
                conn = get_connection()
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS alert_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        threshold_name TEXT NOT NULL,
                        metric_key TEXT NOT NULL,
                        condition TEXT NOT NULL,
                        threshold_value REAL NOT NULL,
                        severity TEXT NOT NULL,
                        channels TEXT NOT NULL,
                        current_value REAL,
                        trigger_time REAL NOT NULL
                    )"""
                )
                conn.commit()
        except Exception:
            logger.exception("Failed to initialize alert_history table")

    def _persist_alert(self, record: AlertRecord) -> None:
        """Write a single alert record to SQLite."""
        insert_sql = (
            "INSERT INTO alert_history"
            " (threshold_name, metric_key, condition, threshold_value,"
            "  severity, channels, current_value, trigger_time)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        params = (
            record.threshold.name,
            record.threshold.metric_key,
            record.threshold.condition.value,
            record.threshold.threshold_value,
            record.threshold.severity.value,
            json.dumps(record.threshold.channels),
            record.current_value,
            record.trigger_time,
        )
        try:
            if not self._history_table_initialized:
                self._init_db()
                self._history_table_initialized = True
            if self._custom_db_path is not None:
                import contextlib
                import sqlite3 as _sqlite3

                with contextlib.closing(_sqlite3.connect(str(self._custom_db_path))) as conn:
                    conn.execute(insert_sql, params)
                    conn.commit()
            else:
                conn = get_connection()
                conn.execute(insert_sql, params)
                conn.commit()
        except Exception:
            logger.exception("Failed to persist alert '%s' to SQLite", record.threshold.name)

    def _load_history(self) -> None:
        """Load persisted alert history from SQLite into memory."""
        select_sql = (
            "SELECT threshold_name, metric_key, condition,"
            " threshold_value, severity, channels,"
            " current_value, trigger_time"
            " FROM alert_history ORDER BY trigger_time"
        )
        try:
            if self._custom_db_path is not None:
                import contextlib
                import sqlite3 as _sqlite3

                with contextlib.closing(_sqlite3.connect(str(self._custom_db_path))) as conn:
                    rows = conn.execute(select_sql).fetchall()
            else:
                rows = get_connection().execute(select_sql).fetchall()
            with self._lock:
                for row in rows:
                    threshold = AlertThreshold(
                        name=row[0],
                        metric_key=row[1],
                        condition=AlertCondition(row[2]),
                        threshold_value=row[3],
                        severity=AlertSeverity(row[4]),
                        channels=json.loads(row[5]),
                    )
                    record = AlertRecord(threshold=threshold, current_value=row[6], trigger_time=row[7])
                    self._history.append(record)
            logger.debug("Loaded %d persisted alerts from SQLite", len(rows))
        except Exception:
            logger.exception("Failed to load alert history from SQLite")

    def register_threshold(self, threshold: AlertThreshold) -> None:
        """Add or replace a named threshold."""
        with self._lock:
            self._thresholds[threshold.name] = threshold
            logger.debug("Registered alert threshold '%s'", threshold.name)

    def unregister_threshold(self, name: str) -> bool:
        """Remove a threshold by name.

        Returns:
            True if the threshold existed.
        """
        with self._lock:
            existed = name in self._thresholds
            self._thresholds.pop(name, None)
            self._active.pop(name, None)
            self._duration_start.pop(name, None)
            return existed

    def clear_thresholds(self) -> None:
        """Remove all thresholds and reset alert state."""
        with self._lock:
            self._thresholds.clear()
            self._active.clear()
            self._duration_start.clear()
            logger.debug("Cleared all alert thresholds")

    def list_thresholds(self) -> list[AlertThreshold]:
        """List registered thresholds.

        Returns:
            Registered threshold objects.
        """
        with self._lock:
            return list(self._thresholds.values())

    def evaluate_all(self, api: DashboardAPI | None = None) -> list[AlertRecord]:
        """Evaluate all registered thresholds against the current metrics snapshot.

        Args:
            api: DashboardAPI instance to use, or None for the global singleton.

        Returns:
            Alerts that fired in this evaluation cycle.
        """
        if api is None:
            api = get_dashboard_api()

        snapshot: MetricsSnapshot = api.get_latest_metrics()
        snapshot_dict = snapshot.to_dict()
        fired: list[AlertRecord] = []

        with self._lock:
            for name, threshold in list(self._thresholds.items()):
                value = _resolve_metric(snapshot_dict, threshold.metric_key)
                if value is None:
                    if threshold.fail_on_missing_metric:
                        record = self._handle_missing_metric(threshold, name)
                        if record is not None:
                            fired.append(record)
                    else:
                        logger.debug(
                            "Metric key '%s' not found in snapshot; skipping threshold '%s'",
                            threshold.metric_key,
                            name,
                        )
                    continue

                if self._check_condition(threshold, value):
                    record = self._handle_triggered(threshold, name, value)
                    if record is not None:
                        fired.append(record)
                else:
                    self._duration_start.pop(name, None)
                    if name in self._active:
                        del self._active[name]
                        logger.info("Alert '%s' cleared (condition no longer met)", name)

        return fired

    @staticmethod
    def _check_condition(threshold: AlertThreshold, value: float) -> bool:
        if threshold.condition == AlertCondition.GREATER_THAN:
            return value > threshold.threshold_value
        if threshold.condition == AlertCondition.LESS_THAN:
            return value < threshold.threshold_value
        if threshold.condition == AlertCondition.EQUALS:
            return value == threshold.threshold_value
        return False

    def _handle_triggered(self, threshold: AlertThreshold, name: str, value: float) -> AlertRecord | None:
        """Handle a triggered condition, respecting duration requirements."""
        now = time.time()
        if threshold.duration_seconds > 0:
            if name not in self._duration_start:
                self._duration_start[name] = now
                logger.debug(
                    "Alert '%s' condition met; waiting %.1fs before firing",
                    name,
                    threshold.duration_seconds,
                )
                return None
            elapsed = now - self._duration_start[name]
            if elapsed < threshold.duration_seconds:
                logger.debug(
                    "Alert '%s' duration not yet satisfied (%.1f / %.1fs)",
                    name,
                    elapsed,
                    threshold.duration_seconds,
                )
                return None

        if name in self._active:
            return None

        record = AlertRecord(threshold=threshold, current_value=value, trigger_time=now)
        self._active[name] = record
        self._history.append(record)
        self._persist_alert(record)
        self._dispatch(record)
        return record

    def _handle_missing_metric(self, threshold: AlertThreshold, name: str) -> AlertRecord | None:
        """Emit a typed alert when a required metric disappears from telemetry.

        Records ``current_value=float("nan")`` so callers can use the standard
        ``value != value`` NaN test to distinguish "metric absent" from a real
        zero reading. The persistence layer maps NaN onto SQL NULL, so the
        ``alert_history.current_value`` column is declared ``REAL`` (nullable)
        rather than ``REAL NOT NULL``.
        """
        if name in self._active:
            return None
        record = AlertRecord(threshold=threshold, current_value=float("nan"), trigger_time=time.time())
        self._active[name] = record
        self._history.append(record)
        self._persist_alert(record)
        self._dispatch(record)
        return record

    def _dispatch(self, alert: AlertRecord) -> None:
        for channel in alert.threshold.channels:
            dispatcher = DISPATCHERS.get(channel)
            if dispatcher:
                try:
                    dispatcher(alert)
                except Exception as exc:
                    logger.error("Dispatcher '%s' raised an error: %s", channel, exc)
            else:
                logger.warning("Unknown alert channel '%s'", channel)

    def get_active_alerts(self) -> list[AlertRecord]:
        """Return currently active alerts.

        Returns:
            Active alert records.
        """
        with self._lock:
            return list(self._active.values())

    def get_history(self) -> list[AlertRecord]:
        """Return all alerts that have fired in this session.

        Returns:
            Historical alert records.
        """
        with self._lock:
            return list(self._history)

    def evaluate_anomaly(self, event: Any) -> None:
        """Handle an AnomalyDetected event from the EventBus.

        Args:
            event: An AnomalyDetected event from the EventBus.
        """
        agent_type = getattr(event, "agent_type", "unknown")
        anomaly_type = getattr(event, "anomaly_type", "unknown")
        score = getattr(event, "score", 0.0)
        detectors = getattr(event, "triggered_detectors", [])
        timestamp = getattr(event, "timestamp", time.time())

        logger.warning(
            "[AlertEngine] Anomaly detected - agent=%s, type=%s, score=%.3f, detectors=%s",
            agent_type,
            anomaly_type,
            score,
            detectors,
        )

        severity = AlertSeverity.MEDIUM if score < 0.8 else AlertSeverity.HIGH
        threshold_name = f"anomaly:{agent_type}:{anomaly_type}"
        threshold = AlertThreshold(
            name=threshold_name,
            metric_key=f"anomaly.{anomaly_type}",
            condition=AlertCondition.GREATER_THAN,
            threshold_value=0.0,
            severity=severity,
            channels=["log", "dashboard"],
        )
        record = AlertRecord(threshold=threshold, current_value=score, trigger_time=timestamp)
        with self._lock:
            self._active[threshold_name] = record
            self._history.append(record)
        self._persist_alert(record)

    def get_stats(self) -> dict[str, Any]:
        """Summarise current alert engine state.

        Returns:
            Registered threshold, active alert, and total fired counts.
        """
        with self._lock:
            return {
                "registered_thresholds": len(self._thresholds),
                "active_alerts": len(self._active),
                "total_fired": len(self._history),
            }


def get_alert_engine() -> AlertEngine:
    """Return the global AlertEngine singleton."""
    return AlertEngine()


def reset_alert_engine() -> None:
    """Destroy the singleton for tests or clean shutdown."""
    with AlertEngine._class_lock:
        AlertEngine._instance = None
    logger.debug("AlertEngine singleton reset")


__all__ = [
    "ALERT_SEND_TIMEOUT",
    "DEFAULT_ALERT_THRESHOLDS",
    "DISPATCHERS",
    "AlertCondition",
    "AlertEngine",
    "AlertRecord",
    "AlertSeverity",
    "AlertThreshold",
    "_dispatch_dashboard",
    "_dispatch_email",
    "_dispatch_log",
    "_dispatch_webhook",
    "_resolve_metric",
    "get_alert_engine",
    "reset_alert_engine",
]
