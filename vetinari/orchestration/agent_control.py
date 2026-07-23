"""Operator control state for orchestration agents.

This module owns the pause/redirect state consumed by graph execution.  Web
routes may mutate it, but orchestration code should not import Litestar modules
to learn whether an agent is paused.
"""

from __future__ import annotations

import threading

from vetinari.security.fail_closed import sanitize_untrusted_text

_agent_control_lock = threading.Lock()
_paused_agents: dict[str, dict[str, str]] = {}
_redirect_targets: dict[str, dict[str, str]] = {}


def _safe_control_text(value: str, field_name: str, *, max_length: int = 500) -> str:
    try:
        return sanitize_untrusted_text(value, max_length=max_length)
    except ValueError as exc:
        raise ValueError(f"{field_name} is unsafe: {exc}") from exc


def get_agent_control_state() -> dict[str, dict[str, dict[str, str]]]:
    """Return an isolated snapshot of paused agents and redirect targets.

    Returns:
        Value produced for the caller.
    """
    with _agent_control_lock:
        return {
            "paused": {key: dict(value) for key, value in _paused_agents.items()},
            "redirects": {key: dict(value) for key, value in _redirect_targets.items()},
        }


def pause_agent(agent_id: str, reason: str) -> dict[str, str]:
    """Pause *agent_id* idempotently and return the active pause record.

    Args:
        agent_id: Agent id value consumed by pause_agent().
        reason: Reason value consumed by pause_agent().

    Returns:
        Value produced for the caller.
    """
    agent_id = _safe_control_text(agent_id, "agent_id", max_length=160)
    reason = _safe_control_text(reason, "reason")
    with _agent_control_lock:
        if agent_id in _paused_agents:
            return dict(_paused_agents[agent_id])
        record = {"agent_id": agent_id, "reason": reason}
        _paused_agents[agent_id] = record
        return dict(record)


def redirect_agent(agent_id: str, task_id: str, reason: str) -> dict[str, str]:
    """Redirect *agent_id* to *task_id* and return the redirect record.

    Args:
        agent_id: Agent id value consumed by redirect_agent().
        task_id: Task id value consumed by redirect_agent().
        reason: Reason value consumed by redirect_agent().

    Returns:
        Value produced for the caller.
    """
    agent_id = _safe_control_text(agent_id, "agent_id", max_length=160)
    task_id = _safe_control_text(task_id, "task_id", max_length=160)
    reason = _safe_control_text(reason, "reason")
    with _agent_control_lock:
        record = {"task_id": task_id, "reason": reason}
        _redirect_targets[agent_id] = record
        return dict(record)


def resume_agent(agent_id: str) -> bool:
    """Resume *agent_id* and clear any pending redirect.

    Returns True when a pause record existed.

    Returns:
        Value produced for the caller.
    """
    agent_id = _safe_control_text(agent_id, "agent_id", max_length=160)
    with _agent_control_lock:
        was_paused = _paused_agents.pop(agent_id, None) is not None
        _redirect_targets.pop(agent_id, None)
        return was_paused


def clear_agent_control_state() -> None:
    """Clear all agent control state for tests and process reset hooks."""
    with _agent_control_lock:
        _paused_agents.clear()
        _redirect_targets.clear()
