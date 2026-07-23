"""Capability Auditor — vetinari.drift.capability_auditor  (Phase 7).

Compares the *documented* capabilities of every agent against the
*implemented* capabilities returned by ``agent.get_capabilities()``.

Any mismatch (extra, missing, or renamed capability) is reported as drift.

Usage
-----
    from vetinari.drift.capability_auditor import get_capability_auditor

    auditor = get_capability_auditor()
    auditor.register_documented(
        AgentType.WORKER.value,
        ["code_generation", "file_writing", "test_creation"]
    )

    findings = auditor.audit_all()
    for f in findings:
        logger.debug(f)
"""

from __future__ import annotations

import importlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vetinari.boundary_guards import require_nonempty
from vetinari.constants import get_user_dir
from vetinari.learning.atomic_writers import write_json_atomic
from vetinari.types import AgentType
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)


CAPABILITY_BASELINE_PATH: str | Path | None = None


def _capability_baseline_path() -> Path:
    """Return the canonical path to the governed capability baseline file.

    Resolved at call time via ``get_user_dir()`` so the path is correct
    regardless of the process working directory or test environment overrides.

    Returns:
        Path to ``<user_dir>/drift_baselines/capabilities.json``.
    """
    if CAPABILITY_BASELINE_PATH is not None:
        return Path(CAPABILITY_BASELINE_PATH)
    return get_user_dir() / "drift_baselines" / "capabilities.json"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class CapabilityFinding:
    """A single capability drift finding for one agent."""

    agent_name: str
    extra_in_code: list[str]  # in code but not in docs
    missing_in_code: list[str]  # in docs but not in code
    is_drift: bool

    def __repr__(self) -> str:
        return f"CapabilityFinding(agent_name={self.agent_name!r}, is_drift={self.is_drift!r})"

    def __str__(self) -> str:
        if not self.is_drift:
            return f"[OK]   {self.agent_name}: capabilities aligned"
        parts = []
        if self.extra_in_code:
            parts.append(f"undocumented={self.extra_in_code}")
        if self.missing_in_code:
            parts.append(f"missing={self.missing_in_code}")
        return f"[DRIFT] {self.agent_name}: {'; '.join(parts)}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return dataclass_to_dict(self)


# ---------------------------------------------------------------------------
# Agent module map
# ---------------------------------------------------------------------------

_AGENT_MODULES: dict[str, str] = {
    AgentType.FOREMAN.value: "vetinari.agents.planner_agent.ForemanAgent",
    AgentType.WORKER.value: "vetinari.agents.consolidated.worker_agent.WorkerAgent",
    AgentType.INSPECTOR.value: "vetinari.agents.consolidated.quality_agent.InspectorAgent",
}


def _load_agent_capabilities(dotpath: str) -> list[str] | None:
    """Import the class at ``dotpath`` and call ``.get_capabilities()``."""
    module_path, cls_name = dotpath.rsplit(".", 1)
    try:
        mod = importlib.import_module(module_path)
        cls = getattr(mod, cls_name)
        instance = cls()
        return instance.get_capabilities()
    except Exception as exc:
        logger.warning("Could not load %s: %s", dotpath, exc)
        return None


# ---------------------------------------------------------------------------
# Auditor
# ---------------------------------------------------------------------------


class CapabilityAuditor:
    """Thread-safe capability auditor.  Singleton — use.

    ``get_capability_auditor()``.
    """

    _instance: CapabilityAuditor | None = None
    _class_lock = threading.Lock()

    def __new__(cls) -> CapabilityAuditor:
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._setup()
        return cls._instance

    def _setup(self) -> None:
        self._lock = threading.RLock()
        # name → set of documented capabilities
        self._documented: dict[str, set[str]] = {}
        self._baseline_error: str | None = None

    # ------------------------------------------------------------------
    # Documentation registration
    # ------------------------------------------------------------------

    def register_documented(self, agent_name: str, capabilities: list[str]) -> None:
        """Record the *documented* capabilities for an agent.

        Args:
            agent_name: The agent name.
            capabilities: The capabilities.
        """
        with self._lock:
            self._documented[agent_name] = set(capabilities)
            self._baseline_error = None
        logger.debug("Documented caps for %s: %s", agent_name, capabilities)

    def register_all_from_contracts(self) -> None:
        """Populate documented capabilities from the governed baseline file.

        Reads capability expectations from the governed baseline file rather than
        querying live agent classes, ensuring drift detection compares against an
        explicit governed source of truth instead of self-seeding from runtime.

        If the baseline file does not exist, records that no governed source is
        available and registers nothing.  This avoids self-seeding from live
        code and avoids reporting every live capability as undocumented drift.

        Raises:
            json.JSONDecodeError: If the governed baseline file is malformed.
            OSError: If the baseline file cannot be read.
        """
        baseline_path = _capability_baseline_path()
        if not baseline_path.exists():
            with self._lock:
                self._documented.clear()
                self._baseline_error = f"missing baseline at {baseline_path}"
            require_nonempty(str(baseline_path), field_name="capability_baseline_path")
            logger.info(
                "No capability baseline at %s — capability drift detection has no governed source; "
                "run save_baseline() after an intentional capability review",
                baseline_path,
            )
            return
        try:
            data = json.loads(baseline_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("baseline root must be a JSON object")
            documented: dict[str, set[str]] = {}
            for agent_name, caps in data.items():
                if not isinstance(agent_name, str):
                    raise ValueError("baseline agent names must be strings")
                if not isinstance(caps, list) or not all(isinstance(cap, str) for cap in caps):
                    raise ValueError(f"baseline capabilities for {agent_name} must be a list of strings")
                documented[agent_name] = set(caps)
            with self._lock:
                self._documented = documented
                self._baseline_error = None
            logger.info("Loaded capability baseline for %d agents from %s", len(data), baseline_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            with self._lock:
                self._documented.clear()
                self._baseline_error = f"unreadable baseline at {baseline_path}: {exc}"
            logger.warning("Could not load capability baseline from %s — %s", baseline_path, exc)

    def save_baseline(self) -> None:
        """Save current live capabilities as the governed baseline.

        Call this explicitly after intentional capability changes to update
        the baseline file. This is the ONLY path that should write the baseline.
        """
        baseline: dict[str, list[str]] = {}
        for agent_name, dotpath in _AGENT_MODULES.items():
            caps = _load_agent_capabilities(dotpath)
            if caps is not None:
                baseline[agent_name] = sorted(caps)
        path = _capability_baseline_path()
        write_json_atomic(path, baseline)
        with self._lock:
            self._documented = {agent_name: set(caps) for agent_name, caps in baseline.items()}
            self._baseline_error = None
        logger.info("Capability baseline saved for %d agents to %s", len(baseline), path)

    # ------------------------------------------------------------------
    # Auditing
    # ------------------------------------------------------------------

    def audit_agent(self, agent_name: str) -> CapabilityFinding:
        """Audit a single agent by loading its class and comparing.

        ``get_capabilities()`` against the registered documented set.

        Returns:
            A CapabilityFinding describing any extra capabilities present in
            code but not documented, missing capabilities documented but absent
            in code, and whether drift was found.
        """
        with self._lock:
            documented = set(self._documented.get(agent_name, []))

        dotpath = _AGENT_MODULES.get(agent_name)
        if dotpath is None:
            return CapabilityFinding(
                agent_name=agent_name,
                extra_in_code=[],
                missing_in_code=list(documented),
                is_drift=bool(documented),
            )

        live_caps = _load_agent_capabilities(dotpath)
        if live_caps is None:
            logger.warning("Could not load capabilities for %s — treating as drift", agent_name)
            return CapabilityFinding(
                agent_name=agent_name,
                extra_in_code=[],
                missing_in_code=list(documented),
                is_drift=True,
            )

        live_set = set(live_caps)
        extra = sorted(live_set - documented)
        missing = sorted(documented - live_set)
        is_drift = bool(extra or missing)

        return CapabilityFinding(
            agent_name=agent_name,
            extra_in_code=extra,
            missing_in_code=missing,
            is_drift=is_drift,
        )

    def audit_all(self, only_documented: bool = True) -> list[CapabilityFinding]:
        """Audit every agent.

        Args:
            only_documented: If True, only audit agents that have documented
                             capabilities registered.  Set to False to audit
                             all 15 agents even if no docs registered.

        Returns:
            One CapabilityFinding per audited agent, sorted by agent name.
        """
        with self._lock:
            if self._baseline_error is not None and not self._documented:
                if not only_documented:
                    return []
                return [
                    CapabilityFinding(
                        agent_name="capability-baseline",
                        extra_in_code=[],
                        missing_in_code=[self._baseline_error],
                        is_drift=True,
                    )
                ]
            names = list(self._documented.keys()) if only_documented else list(_AGENT_MODULES.keys())

        return [self.audit_agent(name) for name in sorted(names)]

    def get_drift_findings(self) -> list[CapabilityFinding]:
        """Return only the findings that represent drift."""
        return [f for f in self.audit_all(only_documented=False) if f.is_drift]

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Audit all known agents and return aggregate capability health metrics.

        Returns:
            Dictionary with ``agents_audited`` (total agents checked),
            ``agents_with_drift`` (count whose live capabilities diverge from
            documented), and ``documented_agents`` (count with registered
            documentation).
        """
        with self._lock:
            baseline_error = self._baseline_error
            documented_agents = len(self._documented)
        findings = (
            [] if baseline_error is not None and documented_agents == 0 else self.audit_all(only_documented=False)
        )
        return {
            "agents_audited": len(findings),
            "agents_with_drift": sum(1 for f in findings if f.is_drift),
            "documented_agents": documented_agents,
            "baseline_available": baseline_error is None,
            "baseline_error": baseline_error,
        }

    def clear(self) -> None:
        """Clear for the current context."""
        with self._lock:
            self._documented.clear()
            self._baseline_error = None


# ---------------------------------------------------------------------------
# Singleton helpers
# ---------------------------------------------------------------------------


def get_capability_auditor() -> CapabilityAuditor:
    """Return the module-level CapabilityAuditor singleton, creating it on first call.

    Returns:
        The shared CapabilityAuditor instance.
    """
    return CapabilityAuditor()


def reset_capability_auditor() -> None:
    """Reset capability auditor."""
    with CapabilityAuditor._class_lock:
        CapabilityAuditor._instance = None
