"""Fail-closed timestamp parsing for persisted decision records."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class DecisionTimeError(ValueError):
    """Raised when a persisted decision timestamp is invalid."""


def parse_decision_time(value: object, *, field: str = "decided_at_utc") -> datetime:
    """Parse an ISO-8601 instant as UTC or raise DecisionTimeError.

    Returns:
        Parsed UTC datetime.

    Raises:
        DecisionTimeError: If the value is not a valid instant.
    """
    if not isinstance(value, str) or not value.strip():
        raise DecisionTimeError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise DecisionTimeError(f"{field} is not a valid ISO-8601 instant") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def max_decision_time(
    records: Iterable[Mapping[str, Any]], *, key: str = "decided_at_utc", strict: bool = True
) -> datetime | None:
    """Return the latest valid decision time, optionally skipping malformed rows.

    Returns:
        Latest parsed UTC datetime, or None when no records are valid.

    Raises:
        DecisionTimeError: If strict mode sees a malformed timestamp.
    """
    latest: datetime | None = None
    for record in records:
        try:
            parsed = parse_decision_time(record.get(key), field=key)
        except DecisionTimeError:
            if strict:
                raise
            logger.warning("Dropping malformed decision timestamp.", exc_info=True)
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest
