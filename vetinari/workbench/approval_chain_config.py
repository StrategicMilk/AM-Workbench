"""Approval-chain configuration loading and validation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from vetinari.workbench.approval_chain_models import SCHEMA_VERSION, ApprovalChainError

_DEFAULT_CONFIG_PATH = Path("config") / "workbench" / "approval_chain.yaml"


def load_approval_chain_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load conservative approval-chain YAML defaults, failing closed on errors.

    Returns:
        Resolved approval chain config value.

    Raises:
        Exception: Propagates validation or runtime failures from the underlying operation.
    """
    config_path = Path(path) if path is not None else _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise ApprovalChainError(f"approval-chain config not found: {config_path}")
    try:
        doc = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ApprovalChainError(f"approval-chain config unreadable: {exc}") from exc
    return _prepare_approval_chain_config(doc)


def _prepare_approval_chain_config(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict):
        raise ApprovalChainError("approval-chain config root must be a mapping")
    prepared = deepcopy(doc)
    if prepared.get("schema_version") != SCHEMA_VERSION:
        raise ApprovalChainError(f"approval-chain config schema_version must be {SCHEMA_VERSION}")
    steps = prepared.get("ordered_steps")
    if not isinstance(steps, list) or not steps:
        raise ApprovalChainError("approval-chain ordered_steps must be non-empty")
    required_steps = {
        "capability_classification",
        "hard_deny",
        "protected_path",
        "destructive_action",
        "dlp_risk",
        "tool_pin_unverified",
        "readiness_gate",
        "governance_gate",
        "session_allow_list",
        "human_approval_fallback",
        "deny_by_default",
    }
    missing = required_steps.difference(str(step.get("name", "")) for step in steps if isinstance(step, dict))
    if missing:
        raise ApprovalChainError(f"approval-chain config missing ordered step(s): {', '.join(sorted(missing))}")
    names = [str(step.get("name", "")) for step in steps if isinstance(step, dict)]
    if len(set(names)) != len(names):
        raise ApprovalChainError("approval-chain ordered_steps must not contain duplicates")
    _validate_safe_ordered_steps(names)
    session_allow = prepared.get("session_allow")
    if not isinstance(session_allow, dict):
        raise ApprovalChainError("approval-chain session_allow must be a mapping")
    prepared.setdefault("protected_path_prefixes", [])
    prepared.setdefault("hard_deny_indicators", [])
    prepared.setdefault("destructive_indicators", [])
    prepared.setdefault("dlp_indicators", [])
    prepared.setdefault("tool_pin_indicators", [])
    prepared.setdefault("fallback_text", "deny-by-default when no trusted approval source is available")
    return prepared


def _validate_safe_ordered_steps(names: list[str]) -> None:
    positions = {name: index for index, name in enumerate(names)}
    if positions.get("deny_by_default") != len(names) - 1:
        raise ApprovalChainError("approval-chain ordered_steps must end with deny_by_default")
    if positions.get("capability_classification") != 0:
        raise ApprovalChainError("approval-chain ordered_steps must start with capability_classification")
    before_session_allow = (
        "hard_deny",
        "protected_path",
        "destructive_action",
        "dlp_risk",
        "tool_pin_unverified",
        "readiness_gate",
        "governance_gate",
    )
    session_index = positions["session_allow_list"]
    for step_name in before_session_allow:
        if positions[step_name] > session_index:
            raise ApprovalChainError(
                f"approval-chain unsafe ordered_steps: {step_name} must precede session_allow_list"
            )
