"""Project automation package — autonomous scanning, fixing, and maintenance.

This package implements the scan-prioritize-fix pipeline that lets Vetinari
autonomously improve a target codebase. Each module handles one step:
scanner finds issues, sandbox tests fixes safely, and git_integration manages
branches and PRs.

Pipeline role: Post-execution tooling. After the Foreman->Worker->Inspector
pipeline produces code, this package validates and improves the output in the
target project before it is committed.
"""

from __future__ import annotations

from vetinari.project.git_integration import (
    commit_changes,
    create_pr_description,
    has_uncommitted_changes,
)
from vetinari.project.sandbox import (
    ProjectSandboxResult,
    execute_in_sandbox,
)
from vetinari.project.scanner import (
    ScanFinding,
    ScanResult,
    prioritize_findings,
    scan_project,
)

__all__ = [
    "ProjectSandboxResult",
    "ScanFinding",
    "ScanResult",
    "commit_changes",
    "create_pr_description",
    "execute_in_sandbox",
    "has_uncommitted_changes",
    "prioritize_findings",
    "scan_project",
]
