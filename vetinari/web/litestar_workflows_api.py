"""Retired Litestar workflow API compatibility metadata."""

from __future__ import annotations

ROUTE_STATUS = {
    "surface": "workflows",
    "runtime": "retired-litestar",
    "replacement": "crates/amw-kernel",
    "fail_closed": True,
}

ENDPOINTS = ("/api/workflows", "/api/workflows/{workflow_id}")


def describe_routes() -> dict[str, object]:
    """Return non-runtime metadata for the retired workflow endpoints."""
    return {"status": dict(ROUTE_STATUS), "endpoints": list(ENDPOINTS)}
