"""Task Context Manifest — Work Instructions for Agent Tasks.

Every task dispatched to an agent can include a structured context manifest
containing everything the agent needs: acceptance criteria, applicable rules,
constraints, verification checklist, relevant episodes, defect warnings,
and escalation triggers.

This is the manufacturing equivalent of a **job traveler** — the document
that follows a part through every station.

Usage:
    manifest = get_manifest_builder().build(task, "WORKER", "build")
    task.metadata["manifest"] = manifest.to_dict()
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from vetinari.boundary_guards import require_nonempty
from vetinari.utils.serialization import dataclass_to_dict

logger = logging.getLogger(__name__)

DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
TASK_MANIFEST_WORKFLOW_GUARDS: tuple[str, ...] = (
    "manifest hashes require nonempty rule context",
    "rules loader failures become explicit fallback rules",
    "constraint loader failures mark the manifest unavailable",
    "optional episode-store failures do not block manifest construction",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return task-manifest workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/orchestration/task_manifest.py",
        "guards": TASK_MANIFEST_WORKFLOW_GUARDS,
    }


@dataclass
class TaskManifestContext:
    """Structured work instructions attached to every dispatched task.

    Contains everything an agent needs to know before starting work:
    what to do, what rules apply, what will be checked, what mistakes
    to avoid, and when to stop and ask for help.
    """

    task_spec: str
    acceptance_criteria: list[str] = field(default_factory=list)
    relevant_rules: list[str] = field(default_factory=list)
    constraints: dict[str, Any] = field(default_factory=dict)
    verification_checklist: list[str] = field(default_factory=list)
    relevant_episodes: list[dict[str, Any]] = field(default_factory=list)
    defect_warnings: list[str] = field(default_factory=list)
    escalation_triggers: list[str] = field(default_factory=list)
    loader_failures: list[str] = field(default_factory=list)
    manifest_hash: str = ""

    def compute_hash(self) -> str:
        """Compute SHA-256 hash of this manifest for audit trail.

        Returns:
            Hex-encoded SHA-256 hash string.
        """
        # Stable serialization for hashing
        data = {
            "task_spec": self.task_spec,
            "acceptance_criteria": self.acceptance_criteria,
            "relevant_rules": self.relevant_rules,
            "constraints": self.constraints,
            "verification_checklist": self.verification_checklist,
            "defect_warnings": self.defect_warnings,
            "escalation_triggers": self.escalation_triggers,
            "loader_failures": self.loader_failures,
        }
        require_nonempty("\n".join(self.relevant_rules or []), field_name="manifest_rules")
        content = json.dumps(data, sort_keys=True)
        self.manifest_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return self.manifest_hash

    def __repr__(self) -> str:
        return (
            f"TaskContextManifest(manifest_hash={self.manifest_hash!r}, "
            f"acceptance_criteria={len(self.acceptance_criteria)}, "
            f"relevant_rules={len(self.relevant_rules)})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Converts the manifest to a plain dictionary for task metadata storage."""
        return dataclass_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskManifestContext:
        """Deserialize from dictionary.

        Args:
            data: Dictionary with manifest fields.

        Returns:
            TaskContextManifest instance.
        """
        return cls(
            task_spec=data.get("task_spec", ""),
            acceptance_criteria=data.get("acceptance_criteria", []),
            relevant_rules=data.get("relevant_rules", []),
            constraints=data.get("constraints", {}),
            verification_checklist=data.get("verification_checklist", []),
            relevant_episodes=data.get("relevant_episodes", []),
            defect_warnings=data.get("defect_warnings", []),
            escalation_triggers=data.get("escalation_triggers", []),
            loader_failures=data.get("loader_failures", []),
            manifest_hash=data.get("manifest_hash", ""),
        )

    def format_for_prompt(self) -> str:
        """Format manifest as structured text for prompt injection.

        Only includes non-empty sections. Used for Tier 3 (high-stakes)
        tasks where the full manifest is injected into the agent prompt.

        Returns:
            Formatted work instructions string.
        """
        parts: list[str] = ["## Your Work Instructions for This Task"]

        if self.acceptance_criteria:
            parts.append("### Acceptance Criteria (your work must satisfy ALL):")
            for i, criterion in enumerate(self.acceptance_criteria, 1):
                parts.append(f"{i}. {criterion}")

        if self.verification_checklist:
            parts.append("\n### What Quality Will Check After You Finish:")
            for i, check in enumerate(self.verification_checklist, 1):
                parts.append(f"{i}. {check}")

        if self.defect_warnings:
            parts.append("\n### Common Mistakes to Avoid:")
            for i, warning in enumerate(self.defect_warnings, 1):
                parts.append(f"{i}. {warning}")

        if self.escalation_triggers:
            parts.append("\n### When to Stop and Ask for Help:")
            parts.extend(f"- {trigger}" for trigger in self.escalation_triggers)

        if self.loader_failures:
            parts.append("\n### Manifest Input Failures:")
            parts.extend(f"- {failure}" for failure in self.loader_failures)

        if self.constraints:
            tokens = self.constraints.get("max_tokens", "unknown")
            timeout = self.constraints.get("timeout_seconds", "unknown")
            retries = self.constraints.get("max_retries", "unknown")
            parts.append(
                f"\n### Resource Budget: {tokens} tokens, {timeout}s timeout, {retries} retries",
            )

        return "\n".join(parts)


class ManifestBuilder:
    """Assembles a TaskContextManifest from all available sources.

    Combines data from:
    - StandardsLoader (constraints, verification, defect warnings)
    - RulesManager (applicable rules)
    - UnifiedMemoryStore (relevant episodes)

    All dependencies are accessed lazily to avoid import cycles.
    """

    def build(
        self,
        task_description: str,
        agent_type: str,
        mode: str,
        model_name: str | None = None,
        acceptance_criteria: list[str] | None = None,
    ) -> TaskManifestContext:
        """Build complete work instructions for a task.

        Args:
            task_description: What the agent should do.
            agent_type: Agent type value (e.g., "WORKER").
            mode: Agent mode (e.g., "build").
            model_name: Optional model identifier for model-specific rules.
            acceptance_criteria: Optional pre-defined acceptance criteria.

        Returns:
            Assembled TaskContextManifest with hash computed.
        """
        manifest = TaskManifestContext(task_spec=task_description)

        if acceptance_criteria:
            manifest.acceptance_criteria = list(acceptance_criteria)

        # 1. Get applicable rules from RulesManager
        manifest.relevant_rules, rules_failure = self._get_rules(agent_type, model_name)
        if rules_failure:
            manifest.loader_failures.append(rules_failure)

        # 2. Get resource constraints from StandardsLoader
        manifest.constraints, constraints_failure = self._get_constraints(agent_type)
        if constraints_failure:
            manifest.loader_failures.append(constraints_failure)

        # 3. Get verification checklist from verification.yaml
        manifest.verification_checklist, verification_failure = self._get_verification(mode)
        if verification_failure:
            manifest.loader_failures.append(verification_failure)

        # 4. Get defect warnings from defect_catalog.md
        manifest.defect_warnings, warnings_failure = self._get_defect_warnings(agent_type)
        if warnings_failure:
            manifest.loader_failures.append(warnings_failure)

        # 5. Get escalation triggers from escalation.md (static list)
        manifest.escalation_triggers = self._get_escalation_triggers()

        # 6. Query episode memory for similar past tasks (top 3)
        manifest.relevant_episodes = self._get_episodes(task_description)

        # 7. Compute hash for audit trail
        manifest.compute_hash()

        return manifest

    def _get_rules(self, agent_type: str, model_name: str | None) -> tuple[list[str], str]:
        """Fetch applicable rules from RulesManager."""
        try:
            from vetinari.rules_manager import get_rules_manager

            rules = get_rules_manager().get_rules_for_context(
                agent_type,
                model_name,
            )
            return rules, ""
        except (ImportError, AttributeError, KeyError, ValueError):
            logger.warning("Failed to load rules for %s — task runs without rules", agent_type, exc_info=True)
            failure = f"rules unavailable for {agent_type}; do not execute without fallback review"
            return [failure], failure

    @staticmethod
    def _get_constraints(agent_type: str) -> tuple[dict[str, Any], str]:
        """Fetch resource constraints from StandardsLoader."""
        try:
            from vetinari.config.standards_loader import get_standards_loader

            return get_standards_loader().get_constraints(agent_type), ""
        except (ImportError, AttributeError, KeyError, ValueError):
            logger.warning("Failed to load constraints for %s — task runs unconstrained", agent_type, exc_info=True)
            failure = f"constraints unavailable for {agent_type}; fail closed until standards load"
            return {"unavailable": True, "reason": failure}, failure

    @staticmethod
    def _get_verification(mode: str) -> tuple[list[str], str]:
        """Fetch verification checklist from StandardsLoader."""
        try:
            from vetinari.config.standards_loader import get_standards_loader

            return get_standards_loader().get_verification_checklist(mode), ""
        except (ImportError, AttributeError, KeyError, ValueError):
            logger.warning("Failed to load verification checklist for mode %s", mode, exc_info=True)
            failure = f"verification checklist unavailable for {mode}; require manual verification"
            return [failure], failure

    @staticmethod
    def _get_defect_warnings(agent_type: str) -> tuple[list[str], str]:
        """Fetch defect warnings from StandardsLoader."""
        try:
            from vetinari.config.standards_loader import get_standards_loader

            return get_standards_loader().get_defect_warnings(agent_type), ""
        except (ImportError, AttributeError, KeyError, ValueError):
            logger.warning("Failed to load defect warnings for %s", agent_type, exc_info=True)
            failure = f"defect warnings unavailable for {agent_type}; require extra defect review"
            return [failure], failure

    @staticmethod
    def _get_escalation_triggers() -> list[str]:
        """Return standard escalation triggers.

        Returns a static list derived from escalation.md rather than
        parsing the full document each time.
        """
        return [
            "Ambiguous requirements with 2+ mutually exclusive interpretations",
            "Conflicting constraints that cannot both be satisfied",
            "Scope exceeds estimate by 2x or more",
            "Missing credentials or configuration values",
            "External service unreachable after 3 retry attempts",
            "Same issue recurs across 2+ rework cycles",
        ]

    @staticmethod
    def _get_episodes(task_description: str) -> list[dict[str, Any]]:
        """Query episode memory for similar past tasks."""
        try:
            from vetinari.memory.unified import get_unified_memory_store

            store = get_unified_memory_store()
            episodes = store.recall_episodes(task_description, limit=3)
            return [
                {
                    "task": getattr(ep, "task_description", str(ep)),
                    "outcome": getattr(ep, "outcome", "unknown"),
                    "learnings": getattr(ep, "learnings", ""),
                }
                for ep in episodes
            ]
        except (
            Exception
        ):  # Broad: episode store is optional; any runtime failure (DB, network, etc.) must not block planning
            logger.warning("Failed to load episodes for task", exc_info=True)
            return []


# ── Singleton ──────────────────────────────────────────────────────────

_instance: ManifestBuilder | None = None
_instance_lock = threading.Lock()


def get_manifest_builder() -> ManifestBuilder:
    """Return the singleton ManifestBuilder instance.

    Returns:
        The singleton ManifestBuilder.
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = ManifestBuilder()
    return _instance
