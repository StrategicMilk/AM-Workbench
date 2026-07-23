"""Vetinari Workflow -- manufacturing-inspired workflow controls.

Provides formal quality gates, statistical process control (SPC),
Andon stop-the-line signals, Nelson rules, and WIP limits for the
orchestration pipeline.
"""

from __future__ import annotations

from vetinari.workflow.andon import (
    AndonSignal,
    AndonSystem,
    NelsonViolation,
    get_andon_system,
    reset_andon_system,
)
from vetinari.workflow.nelson_rules import NelsonRuleDetector
from vetinari.workflow.quality_gates import (
    GateAction,
    WorkflowGateRunner,
    get_gate_runner,
)
from vetinari.workflow.spc import (
    SPCMonitor,
    get_spc_monitor,
    reset_spc_monitor,
)
from vetinari.workflow.wip import WIPConfig, WIPTracker
from vetinari.workflow.wiring import (
    check_andon_before_dispatch,
    complete_and_pull,
    dispatch_or_queue,
    get_dispatch_status,
    raise_quality_andon,
    wire_workflow_subsystem,
)

__all__ = [
    "AndonSignal",
    "AndonSystem",
    "GateAction",
    "NelsonRuleDetector",
    "NelsonViolation",
    "SPCMonitor",
    "WIPConfig",
    "WIPTracker",
    "WorkflowGateRunner",
    "check_andon_before_dispatch",
    "complete_and_pull",
    "dispatch_or_queue",
    "get_andon_system",
    "get_dispatch_status",
    "get_gate_runner",
    "get_spc_monitor",
    "raise_quality_andon",
    "reset_andon_system",
    "reset_spc_monitor",
    "wire_workflow_subsystem",
]

__version__ = "1.0.0"
