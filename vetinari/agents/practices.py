"""Vetinari Universal Agent Practices.

Model-agnostic best practices injected into all agent prompts. These
practices replace any Claude-specific or provider-specific language with
universal imperatives that work across any LLM backend.

Also contains code generation standards and mode verification requirements
for structured agent output validation.

Practices are runtime-configurable via ``vetinari/config/practices.yaml``.
When the YAML file is present and valid, it overrides the hardcoded
``AGENT_PRACTICES`` defaults. Use :func:`load_practices` to retrieve the
active set and :func:`reset_practices_cache` to clear the cache in tests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Default path for the practices YAML config relative to this file's package root.
_DEFAULT_PRACTICES_YAML: Path = Path(__file__).parent.parent / "config" / "practices.yaml"

# Module-level cache; None means not yet loaded.
_practices_cache: PracticesLoadResult | None = None


# ---------------------------------------------------------------------------
# Universal agent practices (Item 54)
# ---------------------------------------------------------------------------

AGENT_PRACTICES: dict[str, str] = {
    "plan_before_act": (
        "Before implementing, outline your approach. Identify what you know, "
        "what you need to find out, and what could go wrong."
    ),
    "explore_before_modify": (
        "Before changing code, read existing implementations. Search for "
        "utilities and patterns that already exist. Never duplicate functionality."
    ),
    "verify_before_report": (
        "Before reporting completion, verify your output: run tests, check for "
        "logical contradictions, confirm all requirements are addressed. Ask "
        "'would a domain expert find obvious errors?'"
    ),
    "context_discipline": (
        "Track your token budget. Summarize large findings. Drop stale context. "
        "Focus on what is relevant to the current task."
    ),
    "evidence_over_assumption": (
        "Every claim must be backed by evidence: file path + line number, test "
        "result, URL, or explicit reasoning chain. Never guess."
    ),
    "escalate_uncertainty": (
        "If confidence is below MEDIUM, surface your uncertainty explicitly. "
        "Request additional research or human review rather than guessing."
    ),
    "minimal_scope": (
        "Only modify what is directly required. Do not refactor surrounding "
        "code, add unrequested features, or 'improve' things outside your task."
    ),
    "delegation_depth": (
        "Maximum 3 levels of delegation. No recursive self-delegation. If a "
        "subtask needs a different specialist, route through the Planner."
    ),
    "checkpoint_frequently": (
        "After completing each significant step, persist your progress. Enable resumption if interrupted."
    ),
    "fail_informatively": (
        "On failure, report: what was attempted, what went wrong, what was "
        "tried to fix it, and what the next agent should know."
    ),
}


@dataclass(frozen=True, slots=True)
class PracticesLoadResult:
    """Result of loading agent practices, including partial-load diagnostics."""

    rules: dict[str, str]
    skipped_count: int
    partial: bool

    def __getitem__(self, key: str) -> str:
        return self.rules[key]

    def __iter__(self):
        return iter(self.rules)

    def __len__(self) -> int:
        return len(self.rules)

    def __bool__(self) -> bool:
        return bool(self.rules)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PracticesLoadResult):
            return (
                self.rules == other.rules
                and self.skipped_count == other.skipped_count
                and self.partial == other.partial
            )
        if isinstance(other, dict):
            return self.rules == other
        return NotImplemented

    def keys(self):
        return self.rules.keys()

    def items(self):
        return self.rules.items()

    def values(self):
        return self.rules.values()

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.rules.get(key, default)


def load_practices(config_path: Path | str | None = None) -> PracticesLoadResult:
    """Load agent practices from YAML, falling back to hardcoded defaults.

    On first call the result is cached. Subsequent calls return the cached
    value regardless of ``config_path``. Use :func:`reset_practices_cache`
    to clear the cache between calls (e.g. in tests).

    Args:
        config_path: Path to the practices YAML file. Defaults to
            ``vetinari/config/practices.yaml`` relative to this package.
            If the file is missing or cannot be parsed, the hardcoded
            ``AGENT_PRACTICES`` dict is used instead.

    Returns:
        A dict mapping practice key to practice text string.
    """
    global _practices_cache
    if _practices_cache is not None:
        return _practices_cache

    path = Path(config_path) if config_path is not None else _DEFAULT_PRACTICES_YAML

    loaded: dict[str, str] | None = None
    skipped = 0
    if path.is_file():
        try:
            import yaml  # local import — optional dependency

            with path.open(encoding="utf-8") as fh:
                data: Any = yaml.safe_load(fh)

            if isinstance(data, dict) and isinstance(data.get("practices"), dict):
                raw = data["practices"]
                parsed: dict[str, str] = {}
                for key, entry in raw.items():
                    if isinstance(entry, dict) and "text" in entry:
                        parsed[key] = str(entry["text"])
                    elif isinstance(entry, str):
                        parsed[key] = entry
                    else:
                        skipped += 1
                        logger.warning(
                            "Skipping malformed practice entry '%s' in %s",
                            key,
                            path,
                        )
                if parsed:
                    loaded = parsed
                    logger.info("Loaded %d practices from %s", len(parsed), path)
                else:
                    logger.warning(
                        "practices.yaml at %s contained no valid entries; using defaults",
                        path,
                    )
            else:
                logger.warning(
                    "practices.yaml at %s has unexpected structure; using defaults",
                    path,
                )
        except Exception:
            logger.exception(
                "Failed to parse practices YAML at %s; using hardcoded defaults",
                path,
            )
    else:
        logger.debug("practices.yaml not found at %s; using hardcoded defaults", path)

    rules = loaded if loaded is not None else dict(AGENT_PRACTICES)
    _practices_cache = PracticesLoadResult(rules=rules, skipped_count=skipped, partial=skipped > 0)
    return _practices_cache


def reset_practices_cache() -> None:
    """Clear the practices cache so the next :func:`load_practices` call re-reads the file.

    Intended for use in tests that need to exercise different YAML configurations
    without restarting the interpreter.
    """
    global _practices_cache
    _practices_cache = None


def format_practices() -> str:
    """Format AGENT_PRACTICES into prompt-ready text.

    Returns:
        A formatted string of all practices, each as a labeled imperative.
    """
    lines = ["## Agent Best Practices\n"]
    for key, value in AGENT_PRACTICES.items():
        label = key.upper().replace("_", " ")
        lines.append(f"**{label}**: {value}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Code generation standards (Item 56)
# ---------------------------------------------------------------------------

AGENT_CODE_STANDARDS: str = (
    "## Code Generation Standards\n\n"
    "These rules are mandatory when generating Python code for the Vetinari project.\n\n"
    "- **Future annotations**: `from __future__ import annotations` at the top of every file\n"
    "- **Canonical imports**: Enums from `vetinari.types`, specs from `vetinari.agents.contracts`, "
    "interfaces from `vetinari.agents.interfaces`. Never redefine locally.\n"
    "- **Modern typing**: `list[str]`, `dict[str, Any]`, `X | None` — never `List`, `Dict`, `Optional`\n"
    "- **Logging**: `logger = logging.getLogger(__name__)` per module; %-style formatting; never `print()`\n"
    "- **File I/O**: Always `encoding='utf-8'` on `open()`; use `pathlib.Path` not `os.path`\n"
    "- **Error handling**: Never bare `except:`; always chain with `from exc`; never swallow silently\n"
    "- **Completeness**: No `TODO`, `FIXME`, `pass` bodies, `NotImplementedError`, `print()`, "
    "commented-out code, or magic numbers\n"
    "- **Docstrings**: Google-style for all public APIs with Args/Returns/Raises sections\n"
    "- **String formatting**: f-strings for general use; %-style ONLY in logger calls; never `.format()`\n"
    "- **Tests**: Every new public function needs at least one test\n"
    "- **Type hints**: All function signatures fully annotated\n"
)

# Modes that generate code and should receive AGENT_CODE_STANDARDS
_CODE_GENERATING_MODES: frozenset[str] = frozenset({
    "build",
    "simplification",
    "test_generation",
})


def get_code_standards(mode: str | None = None) -> str:
    """Return code generation standards if the mode generates code.

    Args:
        mode: The current agent mode. If None or not a code-generating
            mode, returns empty string.

    Returns:
        Code standards text for code-generating modes, empty string otherwise.
    """
    if mode and mode in _CODE_GENERATING_MODES:
        return AGENT_CODE_STANDARDS
    return ""


# ---------------------------------------------------------------------------
# Mode verification requirements (Item 57)
# ---------------------------------------------------------------------------

MODE_VERIFICATION_REQUIREMENTS: dict[str, list[str]] = {
    "WORKER:build": [
        "pytest_passes",
        "type_hints_present",
        "no_print_statements",
        "imports_canonical",
    ],
    "WORKER:image_generation": [
        "file_exists",
        "format_valid",
    ],
    "INSPECTOR:code_review": [
        "findings_have_file_line_refs",
        "score_calculated",
        "gate_decision_binary",
    ],
    "INSPECTOR:security_audit": [
        "cwe_references_included",
        "severity_classified",
        "attack_vectors_described",
    ],
    "INSPECTOR:test_generation": [
        "pytest_passes",
        "coverage_reported",
    ],
    "INSPECTOR:simplification": [
        "no_behavior_change",
        "tests_still_pass",
    ],
    "WORKER:code_discovery": [
        "all_paths_verified_exist",
        "confidence_scores_included",
    ],
    "WORKER:domain_research": [
        "sources_cited",
        "evidence_chain_complete",
    ],
    "WORKER:architecture": [
        "min_3_alternatives_evaluated",
        "adr_created",
        "trade_offs_documented",
    ],
    "WORKER:risk_assessment": [
        "likelihood_x_impact_scored",
        "mitigations_proposed",
    ],
    "WORKER:documentation": [
        "no_empty_sections",
        "examples_included",
        "cross_refs_valid",
    ],
    "WORKER:creative_writing": [
        "tone_consistent",
        "audience_appropriate",
    ],
    "WORKER:cost_analysis": [
        "cost_breakdown_included",
        "recommendations_actionable",
    ],
    "FOREMAN:plan": [
        "task_dag_valid",
        "dependencies_acyclic",
        "all_tasks_assigned",
    ],
}


# ---------------------------------------------------------------------------
# Mode-relevant practices (Dept 2.6 — selective injection)
# ---------------------------------------------------------------------------

MODE_RELEVANT_PRACTICES: dict[str, list[str]] = {
    # Builder modes
    "build": [
        "plan_before_act",
        "verify_before_report",
        "evidence_over_assumption",
        "minimal_scope",
    ],
    "image_generation": [
        "plan_before_act",
        "verify_before_report",
    ],
    # Planner modes
    "plan": [
        "plan_before_act",
        "delegation_depth",
        "checkpoint_frequently",
    ],
    "clarify": [
        "evidence_over_assumption",
        "escalate_uncertainty",
    ],
    "consolidate": [
        "verify_before_report",
        "checkpoint_frequently",
    ],
    "summarise": [
        "context_discipline",
        "verify_before_report",
    ],
    "prune": [
        "minimal_scope",
        "verify_before_report",
    ],
    "extract": [
        "evidence_over_assumption",
        "verify_before_report",
    ],
    # Researcher modes
    "code_discovery": [
        "explore_before_modify",
        "evidence_over_assumption",
        "verify_before_report",
    ],
    "domain_research": [
        "evidence_over_assumption",
        "explore_before_modify",
        "context_discipline",
    ],
    "api_lookup": [
        "evidence_over_assumption",
        "verify_before_report",
    ],
    "lateral_thinking": [
        "plan_before_act",
        "evidence_over_assumption",
    ],
    "ui_design": [
        "plan_before_act",
        "verify_before_report",
    ],
    "database": [
        "plan_before_act",
        "verify_before_report",
    ],
    "devops": [
        "plan_before_act",
        "verify_before_report",
        "checkpoint_frequently",
    ],
    "git_workflow": [
        "explore_before_modify",
        "verify_before_report",
    ],
    # Oracle modes
    "architecture": [
        "plan_before_act",
        "evidence_over_assumption",
        "explore_before_modify",
    ],
    "risk_assessment": [
        "evidence_over_assumption",
        "escalate_uncertainty",
    ],
    "ontological_analysis": [
        "evidence_over_assumption",
        "verify_before_report",
    ],
    "contrarian_review": [
        "evidence_over_assumption",
        "verify_before_report",
    ],
    "suggest": [
        "evidence_over_assumption",
        "plan_before_act",
    ],
    # Quality modes
    "code_review": [
        "explore_before_modify",
        "verify_before_report",
        "evidence_over_assumption",
    ],
    "security_audit": [
        "explore_before_modify",
        "verify_before_report",
        "evidence_over_assumption",
    ],
    "test_generation": [
        "plan_before_act",
        "verify_before_report",
        "evidence_over_assumption",
    ],
    "simplification": [
        "explore_before_modify",
        "verify_before_report",
        "minimal_scope",
    ],
    # Operations modes
    "documentation": [
        "verify_before_report",
        "evidence_over_assumption",
    ],
    "creative_writing": [
        "plan_before_act",
        "verify_before_report",
    ],
    "cost_analysis": [
        "evidence_over_assumption",
        "verify_before_report",
    ],
    "experiment": [
        "plan_before_act",
        "evidence_over_assumption",
        "checkpoint_frequently",
    ],
    "error_recovery": [
        "explore_before_modify",
        "verify_before_report",
        "fail_informatively",
    ],
    "synthesis": [
        "verify_before_report",
        "context_discipline",
    ],
    "improvement": [
        "explore_before_modify",
        "evidence_over_assumption",
    ],
    "monitor": [
        "verify_before_report",
        "evidence_over_assumption",
    ],
    "devops_ops": [
        "plan_before_act",
        "verify_before_report",
        "checkpoint_frequently",
    ],
}


@lru_cache(maxsize=256)
def get_practices_for_mode(mode: str) -> str:
    """Return only the practices relevant to a given mode.

    Instead of injecting all 10 practices (~963 tokens), this returns
    only the 2-4 practices relevant to the current mode (~200-350 tokens).

    Args:
        mode: The agent mode (e.g., "build", "code_review").

    Returns:
        Formatted string of relevant practices. Returns a minimal default
        (plan_before_act + verify_before_report) for unknown modes.
    """
    relevant_keys = MODE_RELEVANT_PRACTICES.get(
        mode,
        ["plan_before_act", "verify_before_report"],  # minimal default
    )
    practices = load_practices().rules
    relevant = {k: v for k, v in practices.items() if k in relevant_keys}
    if not relevant:
        return ""
    lines = ["## Agent Best Practices\n"]
    for key, value in relevant.items():
        label = key.upper().replace("_", " ")
        lines.append(f"**{label}**: {value}")
    return "\n".join(lines)


def get_verification_requirements(
    agent_type: str,
    mode: str,
) -> list[str]:
    """Return verification requirements for an agent type and mode.

    Args:
        agent_type: The agent type value (e.g. "WORKER", "FOREMAN", "INSPECTOR").
        mode: The mode name (e.g. "build").

    Returns:
        List of verification requirement identifiers, or empty list if
        no requirements are defined for this agent/mode combination.
    """
    key = f"{agent_type}:{mode}"
    return list(MODE_VERIFICATION_REQUIREMENTS.get(key, []))
