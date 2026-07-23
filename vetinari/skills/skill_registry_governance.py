"""Progressive disclosure and governance behavior for SkillRegistry."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from jsonschema import ValidationError, validate

from vetinari.skills import catalog_loader
from vetinari.skills.skill_definitions import SKILL_REGISTRY
from vetinari.skills.skill_registry_paths import _AGENT_SKILL_MAP, _CENTRAL_REGISTRY, _CONTEXT_REGISTRY
from vetinari.utils.bounded_collections import BoundedList

logger = logging.getLogger(__name__)

_MAX_PENDING_SKILL_PROPOSALS = 500
_PROPOSAL_INIT_LOCK = threading.Lock()


class SkillRegistryGovernanceMixin:
    """Support skill summaries, validation, proposals, and trust checks."""

    if TYPE_CHECKING:
        _loading_levels: Any
        _pending_proposals: Any
        _pending_proposals_lock: Any
        get_skill: Any

    @staticmethod
    def _summarize_disk_entry(entry: Any) -> dict[str, str] | None:
        """Convert a disk-loaded catalog entry into a Level 1 summary."""
        skill_id = getattr(entry, "skill_id", None)
        if not skill_id and isinstance(entry, dict):
            skill_id = entry.get("skill_id") or entry.get("id")
        if not skill_id:
            return None
        if isinstance(entry, dict):
            return {
                "id": str(skill_id),
                "name": str(entry.get("name", "")),
                "description": str(entry.get("description", "")),
                "trust_tier": str(entry.get("trust_tier", "t4_core")),
            }
        return {
            "id": str(skill_id),
            "name": str(getattr(entry, "name", "")),
            "description": str(getattr(entry, "description", "")),
            "trust_tier": str(getattr(entry, "trust_tier", "t4_core")),
        }

    @staticmethod
    def _load_disk_catalog_entries() -> list[Any]:
        """Load disk catalog entries without making governance callers fail."""
        try:
            if hasattr(catalog_loader, "load_all"):
                entries = catalog_loader.load_all()
            else:
                entries = catalog_loader.load_catalog()
        except Exception as exc:
            logger.warning("Could not load disk skill catalog entries: %s", exc)
            return []
        if entries is None:
            return []
        if isinstance(entries, dict):
            return list(entries.values())
        return list(entries)

    def get_skill_summary(self, skill_id: str) -> dict[str, str] | None:
        """Level 1 metadata: id, name, description, and trust_tier.

        Args:
            skill_id: The skill identifier.

        Returns:
            Dict with id, name, description, and trust_tier, or None if not
            found.
        """
        spec = SKILL_REGISTRY.get(skill_id)
        if spec:
            self._loading_levels.setdefault(skill_id, 1)
            return {
                "id": spec.skill_id,
                "name": spec.name,
                "description": spec.description,
                "trust_tier": spec.trust_tier,
            }
        for entry in self._load_disk_catalog_entries():
            summary = self._summarize_disk_entry(entry)
            if summary and summary["id"] == skill_id:
                self._loading_levels.setdefault(skill_id, 1)
                return summary
        return None

    def activate_skill(self, skill_id: str) -> dict[str, Any] | None:
        """Elevate to Level 2 (full spec) and return complete metadata.

        Args:
            skill_id: The skill identifier.

        Returns:
            Full skill metadata dict, or None if not found.
        """
        result = self.get_skill(skill_id)
        if result is not None:
            self._loading_levels[skill_id] = 2
        return result

    def get_loading_level(self, skill_id: str) -> int:
        """Return the current progressive disclosure level (0/1/2/3).

        Args:
            skill_id: The skill identifier.

        Returns:
            Integer level: 0 = not loaded, 1 = summary, 2 = full spec.
        """
        return self._loading_levels.get(skill_id, 0)

    def list_skill_summaries(self) -> list[dict[str, str]]:
        """Level 1 summaries for all skills.

        Returns:
            List of summary dicts with id, name, description, trust_tier.
        """
        summaries = []
        seen: set[str] = set()
        for spec in SKILL_REGISTRY.values():
            summaries.append({
                "id": spec.skill_id,
                "name": spec.name,
                "description": spec.description,
                "trust_tier": spec.trust_tier,
            })
            seen.add(spec.skill_id)
            self._loading_levels.setdefault(spec.skill_id, 1)
        for entry in self._load_disk_catalog_entries():
            summary = self._summarize_disk_entry(entry)
            if not summary or summary["id"] in seen:
                continue
            summaries.append(summary)
            seen.add(summary["id"])
            self._loading_levels.setdefault(summary["id"], 1)
        return summaries

    def validate_skill_output(self, skill_id: str, output: Any) -> tuple[bool, list[str]]:
        """Run output validators for a skill against produced output.

        Args:
            skill_id: The skill identifier whose validators to run.
            output: The output produced by the skill to validate.

        Returns:
            Tuple of (all_passed, list_of_failure_messages).
        """
        failures = []
        spec = SKILL_REGISTRY.get(skill_id)
        if not spec:
            return False, [f"Skill {skill_id!r} not found"]
        if spec.require_schema_validation:
            if not spec.output_schema:
                failures.append("Skill requires schema validation but declares no output_schema")
            else:
                try:
                    validate(output, spec.output_schema)
                except ValidationError as exc:
                    failures.append(f"Output schema validation failed: {exc.message}")
        if not spec.output_validators:
            if failures:
                logger.warning("Skill %s output validation failed: %s", skill_id, "; ".join(failures))
                return False, failures
            return True, []
        for validator in spec.output_validators:
            try:
                if not validator(output):
                    failures.append(f"Validator {validator.__name__} rejected output")
            except Exception as exc:
                failures.append(f"Validator raised {type(exc).__name__}: {exc}")
        if failures:
            logger.warning("Skill %s output validation failed: %s", skill_id, "; ".join(failures))
        return len(failures) == 0, failures

    def check_for_changes(self) -> bool:
        """Check skill files for changes and reset loaded state when modified.

        Returns:
            True if the registry file has been modified since last load.
        """
        paths = (_CENTRAL_REGISTRY, _AGENT_SKILL_MAP, _CONTEXT_REGISTRY)
        if not any(path.exists() for path in paths):
            return False
        try:
            current_mtime = max(path.stat().st_mtime for path in paths if path.exists())
            last_mtime = getattr(self, "_last_registry_bundle_mtime", 0.0)
            if current_mtime > last_mtime:
                self.is_loaded = False
                self._last_registry_bundle_mtime = current_mtime
                return True
        except OSError:
            logger.warning("Could not stat registry bundle files; treating as unchanged")
        return False

    def propose_skill(
        self,
        skill_id: str,
        name: str,
        description: str,
        capabilities: list[str],
        proposed_by: str = "agent",
    ) -> dict[str, Any]:
        """Propose a new skill for human review at T1 trust tier.

        Args:
            skill_id: Proposed identifier for the new skill.
            name: Human-readable skill name.
            description: What the skill does.
            capabilities: List of capability strings the skill will declare.
            proposed_by: Source of the proposal, such as agent name or human.

        Returns:
            Dict with ``status`` and proposal details or rejection reason.
        """
        if skill_id in SKILL_REGISTRY:
            return {"status": "rejected", "reason": f"Skill '{skill_id}' already exists"}
        proposal: dict[str, Any] = {
            "skill_id": skill_id,
            "name": name,
            "description": description,
            "capabilities": capabilities,
            "trust_tier": "t1_untrusted",
            "proposed_by": proposed_by,
            "status": "pending_review",
        }
        proposals, proposals_lock = self._proposal_storage()
        with proposals_lock:
            proposals.append(proposal)
        logger.info("Skill proposal received: %s (from %s)", skill_id, proposed_by)
        return {"status": "pending_review", "proposal": proposal}

    def _proposal_storage(self) -> tuple[BoundedList[dict[str, Any]], threading.Lock]:
        """Return bounded, thread-safe pending proposal storage for this registry."""
        if hasattr(self, "_pending_proposals") and hasattr(self, "_pending_proposals_lock"):
            return self._pending_proposals, self._pending_proposals_lock

        with _PROPOSAL_INIT_LOCK:
            if not hasattr(self, "_pending_proposals_lock"):
                self._pending_proposals_lock = threading.Lock()
            if not hasattr(self, "_pending_proposals"):
                self._pending_proposals = BoundedList[dict[str, Any]](_MAX_PENDING_SKILL_PROPOSALS)
            elif not isinstance(self._pending_proposals, BoundedList):
                self._pending_proposals = BoundedList[dict[str, Any]](
                    _MAX_PENDING_SKILL_PROPOSALS,
                    self._pending_proposals,
                )
            return self._pending_proposals, self._pending_proposals_lock

    def verify_trust_elevation(self, skill_id: str) -> dict[str, Any]:
        """Run the 4-gate verification chain for trust tier elevation.

        Args:
            skill_id: The skill identifier to verify.

        Returns:
            Dict with ``overall_pass``, ``gate_results``, and ``current_tier``.
        """
        spec = SKILL_REGISTRY.get(skill_id)
        if not spec:
            return {"overall_pass": False, "error": f"Skill {skill_id} not found"}
        gates = {
            "g1_static": bool(spec.skill_id and spec.name and spec.description and spec.modes),
            "g2_semantic": len(spec.capabilities) > 0,
            "g3_behavioral": bool(spec.output_schema),
            "g4_permissions": spec.max_tokens > 0 and spec.timeout_seconds > 0,
        }
        return {
            "overall_pass": all(gates.values()),
            "gate_results": gates,
            "current_tier": spec.trust_tier,
        }
