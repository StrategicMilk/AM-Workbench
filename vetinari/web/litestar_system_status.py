"""Retired Litestar system-status API compatibility metadata."""

from __future__ import annotations

ROUTE_STATUS = {
    "surface": "system-status",
    "runtime": "retired-litestar",
    "replacement": "crates/amw-kernel",
    "fail_closed": True,
}

ENDPOINTS = ("/api/system/status", "/api/system/health")


def describe_routes() -> dict[str, object]:
    """Return non-runtime metadata for the retired system-status endpoints."""
    return {"status": dict(ROUTE_STATUS), "endpoints": list(ENDPOINTS)}
