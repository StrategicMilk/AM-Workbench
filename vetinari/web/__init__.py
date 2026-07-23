"""Retained Python web compatibility layer.

The primary AM Workbench API host is the native Rust kernel. Modules in this
package remain for compatibility tests and protected Python sibling services.
"""

from __future__ import annotations

CURRENT_API_HOST = "Rust Axum kernel"
CURRENT_ROUTE_SURFACE_REFS = ("crates/amw-kernel/src/api/routes", "src-tauri")

__all__ = ["CURRENT_API_HOST", "CURRENT_ROUTE_SURFACE_REFS"]
