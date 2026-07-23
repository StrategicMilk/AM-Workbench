"""Fail-closed guard primitives package."""

from __future__ import annotations

from vetinari.guards.fail_closed import GateError, closed_gate, require_subsystem, strict_invoke

__all__ = ["GateError", "closed_gate", "require_subsystem", "strict_invoke"]
