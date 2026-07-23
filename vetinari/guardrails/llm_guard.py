"""Prompt guardrail scanning entry point."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from vetinari.guardrails.prompt_security import scan_prompt_security
from vetinari.safety.llm_guard_scanner import get_llm_guard_scanner

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "runtime" / "llm_guard.yaml"


@dataclass(frozen=True, slots=True)
class PromptScanResult:
    """Prompt scan result."""

    allowed: bool
    findings: tuple[str, ...] = ()


def _load_config(config_path: str | Path | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path is not None else _DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    return raw if isinstance(raw, dict) else {}


def _scanner_enabled(config: dict[str, Any], name: str) -> bool:
    scanner = config.get("input_scanners", {}).get(name, {})
    if not isinstance(scanner, dict):
        return True
    return bool(scanner.get("enabled", True))


def scan_prompt(prompt: str, *, config_path: str | Path | None = None) -> PromptScanResult:
    """Scan a prompt for blocked content.

    Args:
        prompt: Prompt text to inspect.
        config_path: Optional path to an llm_guard YAML config.

    Returns:
        Prompt scan result.
    """
    config = _load_config(config_path)
    if not _scanner_enabled(config, "prompt_injection"):
        return PromptScanResult(allowed=True)

    deterministic_findings = scan_prompt_security(prompt)
    if deterministic_findings:
        return PromptScanResult(
            allowed=False,
            findings=tuple(finding.code for finding in deterministic_findings),
        )

    result = get_llm_guard_scanner().scan_input(prompt, context="prompt_guardrail")
    findings = tuple(finding.scanner_name for finding in result.findings if not finding.is_safe)
    return PromptScanResult(allowed=result.is_safe, findings=findings)


__all__ = ["PromptScanResult", "scan_prompt"]
