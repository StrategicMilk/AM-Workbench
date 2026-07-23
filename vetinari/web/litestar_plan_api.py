"""Retired Litestar plan API compatibility metadata."""

from __future__ import annotations

ROUTE_STATUS = {
    "surface": "plans",
    "runtime": "retired-litestar",
    "replacement": "crates/amw-kernel",
    "fail_closed": True,
}

ENDPOINTS = ("/api/plans", "/api/plans/{plan_id}")


def describe_routes() -> dict[str, object]:
    """Return non-runtime metadata for the retired plan endpoints."""
    return {"status": dict(ROUTE_STATUS), "endpoints": list(ENDPOINTS)}
