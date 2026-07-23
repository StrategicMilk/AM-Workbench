"""Subtask Tree module.

Hierarchical subtask storage with JSON file-backed persistence.
Uses the unified ``Subtask`` from ``plan_types`` (M4 ontology unification).
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any

from vetinari.constants import get_user_dir
from vetinari.planning.plan_types import Subtask

logger = logging.getLogger(__name__)


class SubtaskTree:
    """Hierarchical subtask storage with JSON file-backed persistence."""

    _instance = None
    _instance_lock = threading.RLock()

    @classmethod
    def get_instance(cls, storage_path: str | None = None) -> SubtaskTree:
        """Return the singleton SubtaskTree instance, creating it if needed.

        Args:
            storage_path: Optional filesystem path for subtask JSON storage.

        Returns:
            The shared SubtaskTree singleton.
        """
        with cls._instance_lock:
            requested_path = Path(storage_path).resolve() if storage_path is not None else None
            current_path = None
            if cls._instance is not None:
                current_path = Path(cls._instance.storage_path).resolve()
            if cls._instance is None or (requested_path is not None and current_path != requested_path):
                cls._instance = cls(storage_path)
            return cls._instance

    @classmethod
    def reset_instance_for_test(cls) -> None:
        """Reset the process-wide SubtaskTree singleton for isolated tests."""
        with cls._instance_lock:
            cls._instance = None

    def __init__(self, storage_path: str | None = None):
        if storage_path is None:
            storage_path = get_user_dir() / "subtasks"

        self.storage_path = Path(storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.trees: dict[str, dict[str, Subtask]] = {}
        self._load_trees()

    def _load_trees(self) -> None:
        for file in self.storage_path.glob("*.json"):
            try:
                with Path(file).open(encoding="utf-8") as f:
                    data = json.load(f)
                    plan_id = file.stem
                    self.trees[plan_id] = {}
                    for st_data in data.get("subtasks", []):
                        subtask = Subtask.from_dict(st_data)
                        self.trees[plan_id][subtask.subtask_id] = subtask
            except Exception as e:
                logger.error("Error loading subtask tree %s: %s", file, e)

    def _save_tree(self, plan_id: str) -> None:
        """Persist the subtask tree for a plan to disk.

        Args:
            plan_id: Identifier of the plan whose tree is being saved.

        Raises:
            ValueError: If the plan ID contains path traversal sequences that
                would place the file outside the configured storage directory.
        """
        target = (self.storage_path / f"{plan_id}.json").resolve()
        if not target.is_relative_to(self.storage_path.resolve()):
            raise ValueError(f"Plan ID contains path traversal: {plan_id}")
        subtasks = [st.to_dict() for st in self.trees.get(plan_id, {}).values()]
        with target.open("w", encoding="utf-8") as f:
            json.dump({"plan_id": plan_id, "subtasks": subtasks}, f, indent=2)

    def create_subtask(
        self,
        plan_id: str,
        parent_id: str,
        depth: int,
        description: str,
        prompt: str,
        agent_type: str,
        max_depth: int = 14,
        dod_level: str = "Standard",
        dor_level: str = "Standard",
        estimated_effort: float = 1.0,
        max_depth_override: int = 0,
        inputs: list[str] | None = None,
        outputs: list[str] | None = None,
        decomposition_seed: str = "",
    ) -> Subtask:
        """Create a new subtask, persist it to disk, and return it.

        Args:
            plan_id: Identifier of the plan this subtask belongs to.
            parent_id: Identifier of the parent subtask (empty or ``"root"`` for top-level).
            depth: Nesting depth of this subtask in the tree.
            description: Human-readable description of the work.
            prompt: Prompt text sent to the assigned agent.
            agent_type: Type of agent that should execute this subtask.
            max_depth: Maximum allowed decomposition depth for the tree.
            dod_level: Definition of Done quality level.
            dor_level: Definition of Ready quality level.
            estimated_effort: Estimated effort units for scheduling.
            max_depth_override: Per-subtask override for max decomposition depth.
            inputs: List of input artifact identifiers required by this subtask.
            outputs: List of output artifact identifiers produced by this subtask.
            decomposition_seed: Seed text guiding further decomposition of this subtask.

        Returns:
            The newly created and persisted Subtask instance.
        """
        if plan_id not in self.trees:
            self.trees[plan_id] = {}

        subtask_id = f"st_{uuid.uuid4().hex[:8]}"

        subtask = Subtask(
            subtask_id=subtask_id,
            plan_id=plan_id,
            parent_subtask_id=parent_id,
            depth=depth,
            max_depth=max_depth,
            description=description,
            prompt=prompt,
            agent_type=agent_type,
            dod_level=dod_level,
            dor_level=dor_level,
            estimated_effort=estimated_effort,
            max_depth_override=max_depth_override,
            inputs=inputs or [],
            outputs=outputs or [],
            decomposition_seed=decomposition_seed,
        )

        # Validate depth against the subtask's own effective maximum before
        # persisting.  get_effective_max_depth() applies the per-subtask
        # override and clamps to the 12-16 range so over-deep decompositions
        # are caught at creation time rather than at inference time.
        self._clamp_depth_to_effective_max(subtask)

        self.trees[plan_id][subtask_id] = subtask
        self._save_tree(plan_id)

        return subtask

    @staticmethod
    def _clamp_depth_to_effective_max(subtask: Subtask) -> None:
        """Clamp a newly created subtask to its effective max depth."""
        effective_max = subtask.get_effective_max_depth()
        if subtask.depth > effective_max:
            logger.warning(
                "Subtask %s in plan %s has depth %d which exceeds its effective "
                "maximum of %d; clamping to %d to prevent runaway decomposition",
                subtask.subtask_id,
                subtask.plan_id,
                subtask.depth,
                effective_max,
                effective_max,
            )
            subtask.depth = effective_max

    def get_subtask(self, plan_id: str, subtask_id: str) -> Subtask | None:
        """Retrieve a single subtask by plan and subtask identifiers.

        Args:
            plan_id: The plan that owns the subtask tree.
            subtask_id: The unique identifier of the subtask to retrieve.

        Returns:
            The matching Subtask, or None if not found.
        """
        return self.trees.get(plan_id, {}).get(subtask_id)

    def get_subtasks_by_parent(self, plan_id: str, parent_id: str) -> list[Subtask]:
        """Return all direct children of a given parent subtask.

        Args:
            plan_id: The plan that owns the subtask tree.
            parent_id: The parent subtask identifier to filter by.

        Returns:
            List of Subtask instances whose parent matches the given identifier.
        """
        return [st for st in self.trees.get(plan_id, {}).values() if st.parent_subtask_id == parent_id]

    def get_root_subtasks(self, plan_id: str) -> list[Subtask]:
        """Return all top-level subtasks that have no parent.

        Args:
            plan_id: The plan that owns the subtask tree.

        Returns:
            List of root-level Subtask instances (parent is empty, None, or ``"root"``).
        """
        return [
            st
            for st in self.trees.get(plan_id, {}).values()
            if not st.parent_subtask_id or st.parent_subtask_id == "root"
        ]

    def get_all_subtasks(self, plan_id: str) -> list[Subtask]:
        """Return every subtask in a plan's tree regardless of depth or parent.

        Args:
            plan_id: The plan that owns the subtask tree.

        Returns:
            List of all Subtask instances belonging to the plan.
        """
        return list(self.trees.get(plan_id, {}).values())

    def update_subtask(self, plan_id: str, subtask_id: str, updates: dict[str, Any]) -> Subtask | None:
        """Apply field updates to an existing subtask and persist the change.

        Args:
            plan_id: Identifier of the plan containing the subtask.
            subtask_id: Identifier of the subtask to update.
            updates: Dictionary mapping field names to their new values.

        Returns:
            The updated Subtask, or None if the subtask was not found.
        """
        subtask = self.trees.get(plan_id, {}).get(subtask_id)
        if not subtask:
            return None

        for key, value in updates.items():
            if hasattr(subtask, key):
                setattr(subtask, key, value)

        self._save_tree(plan_id)
        return subtask

    def delete_tree(self, plan_id: str) -> bool:
        """Remove an entire subtask tree and its backing JSON file from disk.

        Args:
            plan_id: Identifier of the plan whose subtask tree should be deleted.

        Returns:
            True if the tree existed and was deleted, False if not found.

        Raises:
            ValueError: If plan_id contains path-traversal characters.
        """
        if plan_id in self.trees:
            del self.trees[plan_id]
            # Guard against path traversal — same check as _save_tree.
            file_path = (self.storage_path / f"{plan_id}.json").resolve()
            if not file_path.is_relative_to(self.storage_path.resolve()):
                raise ValueError(f"Plan ID contains path traversal: {plan_id}")
            if file_path.exists():
                file_path.unlink()
            return True
        return False

    def get_tree_depth(self, plan_id: str) -> int:
        """Return the maximum nesting depth across all subtasks in a plan's tree.

        Args:
            plan_id: Identifier of the plan to inspect.

        Returns:
            The deepest depth value, or 0 if the tree is empty.
        """
        subtasks = self.trees.get(plan_id, {}).values()
        if not subtasks:
            return 0
        return max(st.depth for st in subtasks)


class _SubtaskTreeProxy:
    """Lazy module-level compatibility facade for the SubtaskTree singleton."""

    @classmethod
    def get_all_subtasks(cls, plan_id: str) -> list[Subtask]:
        return SubtaskTree.get_instance().get_all_subtasks(plan_id)

    @classmethod
    def update_subtask(cls, plan_id: str, subtask_id: str, updates: dict[str, Any]) -> Subtask | None:
        return SubtaskTree.get_instance().update_subtask(plan_id, subtask_id, updates)

    @classmethod
    def delete_tree(cls, plan_id: str) -> bool:
        return SubtaskTree.get_instance().delete_tree(plan_id)


subtask_tree = _SubtaskTreeProxy
