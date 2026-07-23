"""Retired Litestar chat API compatibility metadata.

The active runtime API is the Rust/Axum kernel. This module stays importable so
docs and migration checks can prove that the old Litestar chat surface is
retired instead of silently missing or accidentally re-enabled.
"""

from __future__ import annotations

ROUTE_STATUS = {
    "surface": "chat",
    "runtime": "retired-litestar",
    "replacement": "crates/amw-kernel",
    "fail_closed": True,
}

ENDPOINTS = ("/api/chat", "/api/chat/stream")


def describe_routes() -> dict[str, object]:
    """Return non-runtime metadata for the retired chat endpoints."""
    return {"status": dict(ROUTE_STATUS), "endpoints": list(ENDPOINTS)}
