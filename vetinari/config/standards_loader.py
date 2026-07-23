"""Vetinari Standards Loader.

Loads and caches agent standards from ``vetinari/config/standards/``.
Supports selective context injection — returns only the standard sections
relevant to a given agent type and mode, reducing prompt overhead from
~963 tokens to ~200-400 tokens per call.

Thread-safe singleton with mtime-based cache invalidation for hot-reload.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from vetinari.config.standards_loader_yaml import StandardsYamlMixin
from vetinari.types import AgentType

logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────────────────────

_STANDARDS_DIR = Path(__file__).resolve().parent / "standards"

# Section header regex for markdown: ## Title
_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)

# Sub-section header regex: ### Title
_SUBSECTION_RE = re.compile(r"^###\s+(.+)$", re.MULTILINE)

# ── Context Relevance Map ──────────────────────────────────────────────
# Maps (agent_type, mode) -> list of section keys from universal.md to inject.
# Section keys are normalized: "Code Generation Rules" -> "code_generation_rules".
# If a mode is not listed, only "core_principles" is injected (minimal default).

CONTEXT_RELEVANCE: dict[tuple[str, str], list[str]] = {
    # --- Worker modes (build/code) ---
    (AgentType.WORKER.value, "build"): [
        "core_principles",
        "code_generation_rules",
        "import_rules",
        "logging_rules",
        "type_system_and_data_structures",
        "completeness_and_robustness",
    ],
    (AgentType.WORKER.value, "image_generation"): [
        "core_principles",
        "documentation_rules",
    ],
    # --- Worker modes (research) ---
    (AgentType.WORKER.value, "code_discovery"): [
        "core_principles",
        "import_rules",
    ],
    (AgentType.WORKER.value, "domain_research"): [
        "core_principles",
        "documentation_rules",
    ],
    (AgentType.WORKER.value, "api_lookup"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "lateral_thinking"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "ui_design"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "database"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "devops"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "git_workflow"): [
        "core_principles",
    ],
    # --- Worker modes (oracle/architecture) ---
    (AgentType.WORKER.value, "architecture"): [
        "core_principles",
        "agent_conventions_and_adrs",
    ],
    (AgentType.WORKER.value, "risk_assessment"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "ontological_analysis"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "contrarian_review"): [
        "core_principles",
    ],
    # --- Worker modes (operations) ---
    (AgentType.WORKER.value, "documentation"): [
        "core_principles",
        "documentation_rules",
    ],
    (AgentType.WORKER.value, "creative_writing"): [
        "core_principles",
        "documentation_rules",
    ],
    (AgentType.WORKER.value, "cost_analysis"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "experiment"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "error_recovery"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "synthesis"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "improvement"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "monitor"): [
        "core_principles",
    ],
    (AgentType.WORKER.value, "devops_ops"): [
        "core_principles",
    ],
    # --- Foreman modes ---
    (AgentType.FOREMAN.value, "plan"): [
        "core_principles",
        "agent_conventions_and_adrs",
        "communication_protocol",
    ],
    (AgentType.FOREMAN.value, "clarify"): [
        "core_principles",
        "communication_protocol",
    ],
    (AgentType.FOREMAN.value, "consolidate"): [
        "core_principles",
    ],
    (AgentType.FOREMAN.value, "summarise"): [
        "core_principles",
    ],
    (AgentType.FOREMAN.value, "prune"): [
        "core_principles",
    ],
    (AgentType.FOREMAN.value, "extract"): [
        "core_principles",
    ],
    # --- Inspector modes ---
    (AgentType.INSPECTOR.value, "code_review"): [
        "core_principles",
        "code_generation_rules",
        "completeness_and_robustness",
    ],
    (AgentType.INSPECTOR.value, "security_audit"): [
        "core_principles",
        "safety_and_high-impact_file_rules",
        "completeness_and_robustness",
    ],
    (AgentType.INSPECTOR.value, "test_generation"): [
        "core_principles",
        "code_generation_rules",
    ],
    (AgentType.INSPECTOR.value, "simplification"): [
        "core_principles",
    ],
}


@lru_cache(maxsize=256)
def _compile_deny_pattern(raw_pattern: str) -> re.Pattern[str]:
    """Compile and cache a deny-pattern regex to avoid repeated re.compile calls.

    Args:
        raw_pattern: The raw regex string from verification.yaml.

    Returns:
        Compiled regex pattern.
    """
    return re.compile(raw_pattern)


# ── StandardsLoader ────────────────────────────────────────────────────


class StandardsLoader(StandardsYamlMixin):
    """Loads and caches standards from vetinari/config/standards/.

    Thread-safe singleton with mtime-based cache invalidation.
    All public methods return prompt-ready strings or parsed structures.
    """

    def __init__(self, standards_dir: Path | None = None) -> None:
        self._dir = standards_dir or _STANDARDS_DIR
        self._lock = threading.Lock()
        # Cache: filename -> (mtime, content)
        self._file_cache: dict[str, tuple[float, str]] = {}
        # Cache: filename -> (mtime, parsed sections dict)
        self._section_cache: dict[str, tuple[float, dict[str, str]]] = {}
        # Cache: constraints.yaml -> (mtime, parsed dict)
        self._yaml_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    # ── File Loading ───────────────────────────────────────────────

    def _read_file(self, filename: str) -> str:
        """Read a file from the standards directory with mtime caching.

        Args:
            filename: Name of the file in the standards directory.

        Returns:
            File content as a string.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        filepath = self._dir / filename
        if not filepath.exists():
            msg = f"Standards file not found: {filepath}"
            raise FileNotFoundError(msg)

        mtime = filepath.stat().st_mtime
        with self._lock:
            cached = self._file_cache.get(filename)
            if cached and cached[0] == mtime:
                return cached[1]

        content = filepath.read_text(encoding="utf-8")
        with self._lock:
            self._file_cache[filename] = (mtime, content)
        return content

    def _parse_sections(self, filename: str) -> dict[str, str]:
        """Parse a markdown file into sections keyed by normalized header.

        Section keys are the ``## Header`` text, lowercased with spaces and
        hyphens replaced by underscores. Content between headers becomes the
        section value.

        Args:
            filename: Name of the markdown file.

        Returns:
            Dict mapping section key to section content.
        """
        filepath = self._dir / filename
        if not filepath.exists():
            msg = f"Standards file not found: {filepath}"
            raise FileNotFoundError(msg)

        mtime = filepath.stat().st_mtime
        with self._lock:
            cached = self._section_cache.get(filename)
            if cached and cached[0] == mtime:
                return cached[1]

        content = filepath.read_text(encoding="utf-8")
        sections: dict[str, str] = {}
        matches = list(_SECTION_RE.finditer(content))

        for i, match in enumerate(matches):
            header = match.group(1).strip()
            key = header.lower().replace(" ", "_").replace("-", "_")
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            sections[key] = content[start:end].strip()

        with self._lock:
            self._section_cache[filename] = (mtime, sections)
        return sections

    # ── Public API ─────────────────────────────────────────────────

    def get_universal_rules(self) -> str:
        """Return full content of universal.md.

        Returns:
            The complete universal standards document.
        """
        return self._read_file("universal.md")

    def get_style_guide(self) -> str:
        """Return full content of style_guide.md.

        Returns:
            The complete style guide document.
        """
        return self._read_file("style_guide.md")

    def get_escalation_rules(self) -> str:
        """Return full content of escalation.md.

        Returns:
            The complete escalation rules document.
        """
        return self._read_file("escalation.md")

    def get_constraints(self, agent_type: str) -> dict[str, Any]:
        """Return resource constraints for an agent type from constraints.yaml.

        Args:
            agent_type: Agent type value (e.g., "FOREMAN", "WORKER").

        Returns:
            Dict with keys: max_tokens, timeout_seconds, max_retries, etc.
            Returns global defaults if the agent type is not listed.
        """
        data = self._read_yaml("constraints.yaml")
        agents = data.get("agents", {})
        global_defaults = data.get("global", {})

        # Normalize agent type to lowercase for YAML key lookup
        key = agent_type.lower()
        if key in agents:
            return dict(agents[key])

        # Return global defaults
        return {
            "max_tokens": global_defaults.get("default_max_tokens", 4096),
            "timeout_seconds": global_defaults.get("default_timeout_seconds", 120),
            "max_retries": global_defaults.get("default_max_retries", 3),
        }

    def get_verification_checklist(self, mode: str) -> list[str]:
        """Return verification checks for a mode from verification.yaml.

        Args:
            mode: The agent mode (e.g., "build", "code_review").

        Returns:
            List of verification check strings. Empty list if mode not found.
        """
        data = self._read_yaml("verification.yaml")
        modes = data.get("modes", {})
        mode_data = modes.get(mode, {})
        return list(mode_data.get("checks", []))

    def get_auto_fail_criteria(self, mode: str) -> list[str]:
        """Return auto-fail criteria for a mode from verification.yaml.

        Args:
            mode: The agent mode (e.g., "build", "code_review").

        Returns:
            List of auto-fail criterion strings. Empty list if mode not found.
        """
        data = self._read_yaml("verification.yaml")
        modes = data.get("modes", {})
        mode_data = modes.get(mode, {})
        return list(mode_data.get("auto_fail", []))

    def get_deny_patterns(self) -> list[dict[str, str]]:
        """Return deny patterns from verification.yaml.

        These are code patterns that should never appear in production code,
        such as fail-open security patterns and hardcoded credentials.

        Returns:
            List of pattern dicts with 'pattern', 'severity', 'description' keys.
        """
        data = self._read_yaml("verification.yaml")
        return list(data.get("deny_patterns", []))

    def evaluate_deny_patterns(self, code: str) -> list[dict[str, str]]:
        """Evaluate code against deny patterns and return findings.

        Checks the given code string against all deny patterns defined
        in verification.yaml. Each pattern is compiled as a regex.

        Args:
            code: Source code string to check.

        Returns:
            List of finding dicts with 'pattern', 'severity', 'description',
            and 'match' keys for each detected violation.
        """
        findings = []
        for pattern_def in self.get_deny_patterns():
            raw_pattern = pattern_def.get("pattern", "")
            if not raw_pattern:
                continue
            try:
                compiled = _compile_deny_pattern(raw_pattern)
                match = compiled.search(code)
                if match:
                    findings.append({
                        "pattern": raw_pattern,
                        "severity": pattern_def.get("severity", "medium"),
                        "description": pattern_def.get("description", f"Deny pattern matched: {raw_pattern}"),
                        "match": match.group(0)[:80],
                    })
            except re.error as exc:
                findings.append({
                    "pattern": raw_pattern,
                    "severity": "high",
                    "description": f"Invalid deny pattern regex: {exc}",
                    "match": "",
                })
        return findings

    def get_quality_criteria(self, agent_type: str, mode: str) -> str:
        """Return quality criteria text for an agent+mode from quality_criteria.md.

        Searches for a section header matching ``{agent_label} — {mode} mode``
        and returns the content under it. Falls back to a generic section.

        Args:
            agent_type: Agent type value (e.g., "WORKER").
            mode: The agent mode (e.g., "build").

        Returns:
            Quality criteria text, or empty string if not found.
        """
        sections = self._parse_sections("quality_criteria.md")

        # Try exact match: "worker_—_build_mode"
        agent_label = agent_type.lower()
        key = f"{agent_label}_—_{mode}_mode"
        if key in sections:
            return sections[key]

        # Try partial match: any section containing the agent label and mode
        for section_key, content in sections.items():
            if agent_label in section_key and mode in section_key:
                return content

        return ""

    def get_defect_warnings(self, agent_type: str, mode: str | None = None) -> list[str]:
        """Return common defect warnings for an agent type from defect_catalog.md.

        Args:
            agent_type: Agent type value (e.g., "WORKER").
            mode: Optional mode for filtering (currently unused, reserved).

        Returns:
            List of defect warning strings (just the titles, ranked).
        """
        sections = self._parse_sections("defect_catalog.md")
        agent_label = agent_type.lower()

        # Find the section for this agent
        for section_key, content in sections.items():
            if agent_label in section_key and "common_defects" in section_key:
                # Extract ### subsection titles as defect warnings
                warnings = [match.group(1).strip() for match in _SUBSECTION_RE.finditer(content)]
                return warnings

        return []

    def get_context_for_mode(self, agent_type: str, mode: str) -> str:
        """Return ONLY the relevant standard sections for this agent+mode.

        This is the key method for context efficiency — instead of injecting
        all ~963 tokens of universal.md, it returns only ~200-400 tokens of
        the sections relevant to what the agent is doing.

        Args:
            agent_type: Agent type value (e.g., "WORKER", "INSPECTOR").
            mode: The agent mode (e.g., "build", "code_review").

        Returns:
            Formatted string of relevant standard sections.

        Raises:
            KeyError: If the relevance map selects no standards sections that
                are present in the loaded standards document.
        """
        # Look up relevant sections
        section_keys = CONTEXT_RELEVANCE.get(
            (agent_type, mode),
            ["core_principles"],  # minimal default
        )

        sections = self._parse_sections("universal.md")

        parts: list[str] = [
            f"## {key.replace('_', ' ').title()}\n\n{sections[key]}" for key in section_keys if key in sections
        ]

        if not parts:
            raise KeyError(f"no standards sections available for agent_type={agent_type!r} mode={mode!r}")

        return "\n\n".join(parts)

    def get_context_hash(self, agent_type: str, mode: str) -> str:
        """Return a SHA-256 hash of the context that would be injected.

        Useful for audit trail — identifies exactly what standards version
        was used for a given agent call.

        Args:
            agent_type: Agent type value.
            mode: The agent mode.

        Returns:
            Hex-encoded SHA-256 hash string.
        """
        context = self.get_context_for_mode(agent_type, mode)
        return hashlib.sha256(context.encode("utf-8")).hexdigest()


# ── Singleton ──────────────────────────────────────────────────────────

_instance: StandardsLoader | None = None
_instance_lock = threading.Lock()


def get_standards_loader(standards_dir: Path | None = None) -> StandardsLoader:
    """Return the singleton StandardsLoader instance.

    Args:
        standards_dir: Optional override for the standards directory.
            Only used on first call; subsequent calls return the existing
            instance.

    Returns:
        The singleton StandardsLoader instance.
    """
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = StandardsLoader(standards_dir)
    return _instance


def reset_standards_loader() -> None:
    """Reset the singleton for testing purposes."""
    global _instance
    with _instance_lock:
        _instance = None
