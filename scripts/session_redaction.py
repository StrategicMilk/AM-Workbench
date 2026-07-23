"""Redaction helpers for workflow session, prompt, and evidence text."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
SECRET_RE = re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|authorization|bearer)\b\s*[:=]\s*([^\s,;]+)")
UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
SESSION_FILE_RE = re.compile(r"\b[0-9a-f]{32,}\b", re.IGNORECASE)


def redact_session_id(value: object) -> str:
    raw = "" if value is None else str(value)
    if not raw:
        return ""
    if UUID_RE.fullmatch(raw) is None and SESSION_FILE_RE.fullmatch(raw) is None:
        return raw
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"session:{digest}"


def redact_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    text = SECRET_RE.sub(lambda m: f"{m.group(1)}=[REDACTED_SECRET]", text)
    text = UUID_RE.sub(lambda m: redact_session_id(m.group(0)), text)
    text = SESSION_FILE_RE.sub(lambda m: redact_session_id(m.group(0)), text)
    return text


def redact_record(value: Any) -> Any:
    """Return a scrubbed deep copy of nested JSON-like data."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, Mapping):
        return {str(k): redact_record(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(redact_record(v) for v in value)
    if isinstance(value, list):
        return [redact_record(v) for v in value]
    return copy.deepcopy(value)


def redact_preview(value: object, *, limit: int = 160) -> str:
    return redact_text(value)[:limit]


def _selftest() -> None:
    raw = {
        "session": "123e4567-e89b-12d3-a456-426614174000",
        "email": "user@example.com",
        "secret": "api_key=abc123",
    }
    scrubbed = redact_record(raw)
    assert raw["email"] == "user@example.com"
    encoded = json.dumps(scrubbed)
    assert "user@example.com" not in encoded
    assert "abc123" not in encoded
    assert redact_record(scrubbed) == scrubbed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        _selftest()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
