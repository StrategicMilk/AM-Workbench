"""Deterministic RequestFrame parser backed by the intake decision tree YAML."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from vetinari.intake.request_frame import RequestFrame

logger = logging.getLogger(__name__)


_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_TREE_PATH = _REPO_ROOT / "config" / "intake_tree.yaml"
_VALID_WORKER_MODES = frozenset({"code", "analysis", "plan", "refactor", "test", "infra", "docs", "spike"})
_VALID_MODEL_TIERS = frozenset({"standard", "fast", "quality"})
_VALID_URGENCY = frozenset({"low", "medium", "high"})
_PROMPT_MATCH_PREFIX_CHARS = 500


class IntakeParser:
    """Parse raw prompts into RequestFrame instances using deterministic token matching."""

    def __init__(self, tree_path: Path | None = None) -> None:
        """Load and validate the intake tree once.

        Args:
            tree_path: Optional path to an intake tree YAML file. When omitted,
                ``config/intake_tree.yaml`` under the repository root is used.

        Raises:
            ValueError: If the YAML tree is malformed.
        """
        self._tree_path = tree_path or _DEFAULT_TREE_PATH
        self._tree = self._load_tree(self._tree_path)

    def parse(self, raw_prompt: str, persona_name: str | None = None) -> RequestFrame:
        """Resolve a raw prompt into a RequestFrame.

        Args:
            raw_prompt: User prompt to classify through the deterministic tree.
            persona_name: Optional persona selected upstream by the caller.

        Returns:
            RequestFrame with fields resolved from the matched leaf.
        """
        prompt = raw_prompt
        lower_prompt = prompt[:_PROMPT_MATCH_PREFIX_CHARS].lower()
        leaf_name = self._match_leaf(self._tree["root"], lower_prompt)
        if leaf_name is not None and leaf_name not in self._tree["leaves"]:
            logger.warning(
                "intake_parser: leaf_name %r not in leaves keys; falling back to default",
                leaf_name,
            )
        leaf = self._tree["leaves"].get(leaf_name) or self._tree["default"]
        destructive = any(str(signal).lower() in lower_prompt for signal in leaf["destructive_intent_signals"])
        return RequestFrame(
            goal=prompt.strip() or "General assistance request",
            persona_name=persona_name,
            preferred_worker_mode=leaf["preferred_worker_mode"],
            preferred_model_tier=leaf["preferred_model_tier"],
            urgency=leaf["urgency_default"],
            scope_hint=leaf.get("scope_hint"),
            destructive_intent=destructive,
            raw_prompt=prompt,
        )

    def _load_tree(self, tree_path: Path) -> dict[str, Any]:
        with tree_path.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle)
        if not isinstance(loaded, dict):
            raise ValueError("Intake tree must be a mapping")
        self._validate_tree(loaded)
        return loaded

    def _validate_tree(self, tree: dict[str, Any]) -> None:
        root = tree.get("root")
        leaves = tree.get("leaves")
        default = tree.get("default")
        if not isinstance(root, dict):
            raise ValueError("Intake tree missing root mapping")
        if not isinstance(leaves, dict) or not leaves:
            raise ValueError("Intake tree must define at least one leaf")
        if not isinstance(default, dict):
            raise ValueError("Intake tree missing default leaf")
        self._validate_node(root, leaves)
        for name, leaf in {**leaves, "default": default}.items():
            self._validate_leaf(str(name), leaf)

    def _validate_node(self, node: dict[str, Any], leaves: dict[str, Any]) -> None:
        token_matches = node.get("token_matches", [])
        if not isinstance(token_matches, list) or not all(isinstance(token, str) for token in token_matches):
            raise ValueError("Intake tree node token_matches must be a list[str]")
        leaf_name = node.get("leaf")
        if leaf_name is not None and leaf_name not in leaves:
            raise ValueError(f"Intake tree node references unknown leaf: {leaf_name!r}")
        children = node.get("children", [])
        if not isinstance(children, list):
            raise ValueError("Intake tree node children must be a list")
        for child in children:
            if not isinstance(child, dict):
                raise ValueError("Intake tree child nodes must be mappings")
            self._validate_node(child, leaves)

    @staticmethod
    def _validate_leaf(name: str, leaf: object) -> None:
        if not isinstance(leaf, dict):
            raise ValueError(f"Intake tree leaf {name!r} must be a mapping")
        leaf_dict: dict[str, Any] = leaf
        mode = leaf_dict.get("preferred_worker_mode")
        tier = leaf_dict.get("preferred_model_tier")
        urgency = leaf_dict.get("urgency_default")
        signals = leaf_dict.get("destructive_intent_signals")
        if mode not in _VALID_WORKER_MODES:
            raise ValueError(f"Intake tree leaf {name!r} has unknown preferred_worker_mode: {mode!r}")
        if tier not in _VALID_MODEL_TIERS:
            raise ValueError(f"Intake tree leaf {name!r} has unknown preferred_model_tier: {tier!r}")
        if urgency not in _VALID_URGENCY:
            raise ValueError(f"Intake tree leaf {name!r} has unknown urgency_default: {urgency!r}")
        if not isinstance(signals, list) or not all(isinstance(signal, str) for signal in signals):
            raise ValueError(f"Intake tree leaf {name!r} destructive_intent_signals must be list[str]")

    def _match_leaf(self, node: dict[str, Any], lower_prompt: str) -> str | None:
        for child in node.get("children", []):
            tokens = child.get("token_matches", [])
            if tokens and not any(token.lower() in lower_prompt for token in tokens):
                continue
            nested_leaf = self._match_leaf(child, lower_prompt)
            if nested_leaf:
                return nested_leaf
            child_leaf = child.get("leaf")
            if isinstance(child_leaf, str):
                return child_leaf
        leaf_name = node.get("leaf")
        return leaf_name if isinstance(leaf_name, str) else None


__all__ = ["IntakeParser"]
