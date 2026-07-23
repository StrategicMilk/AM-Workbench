"""WIP + Andon wiring — connects the WIP tracker and Andon stop-the-line system into the task dispatch pipeline.

This is the dispatch gate layer: every task entering the Foreman->Worker->
Inspector pipeline passes through here before an agent slot is claimed.
WIP limits prevent resource exhaustion by capping concurrent tasks per agent
type.  The Andon system halts all dispatch when critical quality failures are
detected, implementing a factory-floor "stop-the-line" pattern.

Pipeline position: Intake -> Planning -> **Dispatch Gate** -> Execution -> Quality Gate.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from vetinari.workflow.andon import AndonSignal, AndonSystem, get_andon_system
from vetinari.workflow.wip import WIPConfig, WIPTracker

logger = logging.getLogger(__name__)


# -- Module-level WIPTracker singleton --------------------------------------
#
# Written by: _get_wip_tracker() on first call.
# Read by:    dispatch_or_queue(), complete_and_pull().
# Lifecycle:  created once, lives for process lifetime.
# Protected by: _wip_lock (double-checked locking pattern).

_wip_tracker: WIPTracker | None = None
_wip_lock = threading.Lock()


def reset_wip_tracker() -> None:
    """Reset the WIPTracker singleton to ``None`` for test isolation."""
    global _wip_tracker
    with _wip_lock:
        _wip_tracker = None


# -- Private helpers --------------------------------------------------------


def _get_wip_tracker() -> WIPTracker:
    """Return the process-global WIPTracker, creating it on first call.

    Uses double-checked locking so that concurrent callers at startup
    never create more than one instance.

    Returns:
        The singleton WIPTracker for this process.
    """
    global _wip_tracker
    if _wip_tracker is None:
        with _wip_lock:
            if _wip_tracker is None:
                _wip_tracker = WIPTracker(WIPConfig())
                logger.debug("WIPTracker singleton created")
    return _wip_tracker


# -- Public dispatch API ----------------------------------------------------


def dispatch_or_queue(agent_type: str, task_id: str) -> bool:
    """Attempt to start a task immediately, queuing it if the WIP limit is full.

    Checks the per-agent-type WIP limit via ``can_start()``.  When capacity
    is available the task is started and ``True`` is returned, signalling the
    caller that it may proceed with execution.  When the limit is reached the
    task is placed in the waiting queue and ``False`` is returned — the task
    will be pulled automatically when a slot opens via :func:`complete_and_pull`.

    Args:
        agent_type: The agent type string (e.g. ``AgentType.WORKER.value``).
        task_id: Unique identifier for the task being dispatched.

    Returns:
        ``True`` if the task was started immediately, ``False`` if it was queued.
    """
    tracker = _get_wip_tracker()
    if tracker.start_task(agent_type, task_id):
        logger.info("Dispatch: started task %s for agent_type=%s", task_id, agent_type)
        return True

    tracker.enqueue(agent_type, task_id)
    logger.info(
        "Dispatch: task %s queued for agent_type=%s (WIP limit reached)",
        task_id,
        agent_type,
    )
    return False


def complete_and_pull(agent_type: str, task_id: str) -> dict[str, Any] | None:
    """Mark a task complete and pull the next queued task for the same agent type.

    Frees the WIP slot held by *task_id* and, if any tasks are waiting in the
    queue for *agent_type*, pulls the oldest one and auto-starts it.  The
    returned dict is the full queue entry (with ``task_id``, ``agent_type``,
    and ``enqueued_at``) so the caller can hand the pulled task to the
    appropriate agent.

    Args:
        agent_type: The agent type that has just finished a task.
        task_id: The completed task identifier.

    Returns:
        The pulled task dict (keys: ``task_id``, ``agent_type``,
        ``enqueued_at``), or ``None`` if the queue is empty.
    """
    tracker = _get_wip_tracker()
    pulled = tracker.complete_task(agent_type, task_id)
    if pulled is not None:
        logger.info(
            "Dispatch: pulled queued task %s for agent_type=%s after completing %s",
            pulled["task_id"],
            agent_type,
            task_id,
        )
    return pulled


def check_andon_before_dispatch() -> bool:
    """Return ``True`` when dispatch is safe (Andon system is not paused).

    Callers MUST check this before calling :func:`dispatch_or_queue`.  A
    paused Andon system means an unacknowledged critical or emergency signal
    is active — no new tasks should start until the condition is resolved.

    Returns:
        ``True`` if dispatch is permitted, ``False`` if the Andon system
        is currently paused due to an unacknowledged critical signal.
    """
    andon: AndonSystem = get_andon_system()
    if andon.is_paused():
        logger.warning("Dispatch blocked: Andon system is paused -- acknowledge all critical signals before resuming")
        return False
    return True


def raise_quality_andon(
    source: str,
    message: str,
    affected_tasks: list[str] | None = None,
) -> AndonSignal:
    """Raise a critical Andon signal from a quality failure, halting dispatch.

    Quality gates (Inspector results, SPC violations, cost overruns) call
    this to stop-the-line.  The signal is always ``severity="critical"`` so
    the Andon system enters its paused state immediately.  Dispatch will be
    blocked for all agent types until the signal is acknowledged.

    Args:
        source: Name of the quality gate or subsystem raising the signal
            (e.g. ``"inspector-agent"``, ``"cost-gate"``).
        message: Human-readable description of the failure.
        affected_tasks: Optional list of task IDs affected by the failure.

    Returns:
        The created :class:`~vetinari.workflow.andon.AndonSignal`.
    """
    andon: AndonSystem = get_andon_system()
    signal = andon.raise_signal(
        source=source,
        severity="critical",
        message=message,
        affected_tasks=affected_tasks,
    )
    if affected_tasks is None:
        affected_tasks = []
    logger.critical(
        "Quality Andon raised by %s: %s (affected_tasks=%s)",
        source,
        message,
        affected_tasks,
    )
    return signal


def get_dispatch_status() -> dict[str, Any]:
    """Return a combined snapshot of the dispatch gate's current state.

    Aggregates WIP utilization per agent type, whether the Andon system is
    paused, the number of tasks waiting in the WIP queue, and the names of
    any active unacknowledged signals.  Suitable for a dashboard health check
    or a ``/status`` API endpoint.

    Returns:
        A dict with the following keys:

        * ``wip_utilization`` — per-agent-type utilization from
          :meth:`~vetinari.workflow.wip.WIPTracker.get_utilization`.
        * ``andon_paused`` — ``True`` while unacknowledged critical signals exist.
        * ``queue_depth`` — total tasks waiting for a WIP slot.
        * ``active_andon_signals`` — list of unacknowledged signal source strings.
    """
    tracker = _get_wip_tracker()
    andon: AndonSystem = get_andon_system()
    active_signals = andon.get_active_signals()
    return {
        "wip_utilization": tracker.get_utilization(),
        "andon_paused": andon.is_paused(),
        "queue_depth": tracker.get_queue_depth(),
        "active_andon_signals": [s.source for s in active_signals],
    }


# -- Subsystem initialisation -----------------------------------------------


def wire_workflow_subsystem() -> None:
    """Initialise the WIP tracker singleton and confirm the subsystem is ready.

    Call once at application startup (e.g. from the Litestar lifespan hook)
    to eagerly create the WIPTracker rather than waiting for the first
    dispatch call.  Idempotent — safe to call multiple times.
    """
    _get_wip_tracker()
    logger.info("Workflow subsystem ready (WIP tracker + Andon wiring initialised)")
