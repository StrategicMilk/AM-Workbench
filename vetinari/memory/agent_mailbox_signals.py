"""Signal validation and UTC helpers for the typed agent mailbox."""

from __future__ import annotations

from datetime import UTC, datetime

from vetinari.memory.blackboard_v2 import MailboxSubmitRequest


def _missing_submit_signals(request: MailboxSubmitRequest) -> tuple[str, ...]:
    missing: list[str] = []
    if not request.sender.strip():
        missing.append("sender")
    if not request.recipients or any(not recipient.strip() for recipient in request.recipients):
        missing.append("recipients")
    if not request.subject.strip():
        missing.append("subject")
    if not request.content.strip():
        missing.append("content")
    if not request.provenance.get("source", "").strip():
        missing.append("provenance.source")
    if not request.receipt_authority.strip():
        missing.append("receipt_authority")
    if not request.safety_ref.strip():
        missing.append("safety_ref")
    if request.confidence is None or not 0.0 <= request.confidence <= 1.0:
        missing.append("confidence")
    if request.timeout_seconds <= 0:
        missing.append("timeout_seconds")
    if request.max_attempts < 1:
        missing.append("max_attempts")
    return tuple(missing)


def _missing_completion_signals(
    result_summary: str,
    receipt_authority: str,
    provenance: dict[str, str],
) -> tuple[str, ...]:
    missing: list[str] = []
    if not result_summary.strip():
        missing.append("result_summary")
    if not receipt_authority.strip():
        missing.append("receipt_authority")
    if not provenance.get("source", "").strip():
        missing.append("provenance.source")
    return tuple(missing)


def _coerce_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _coerce_utc(value).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
