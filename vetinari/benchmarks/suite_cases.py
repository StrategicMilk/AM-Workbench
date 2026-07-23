"""Default offline benchmark case definitions for the benchmark suite."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vetinari.benchmarks.benchmark_types import BenchmarkCase
from vetinari.types import AgentType


@dataclass(frozen=True, slots=True)
class _CaseSpec:
    """Compact immutable spec for one default benchmark case."""

    case_id: str
    agent_type: str
    task_type: str
    description: str
    input_text: str
    expected_keys: tuple[str, ...]

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"case_id={self.case_id!r}, "
            f"agent_type={self.agent_type!r}, "
            f"task_type={self.task_type!r}, "
            f"description={self.description!r}"
            ")"
        )


_DEFAULT_CASE_SPECS = (
    _CaseSpec(
        "planner_decompose_001",
        AgentType.FOREMAN.value,
        "planning",
        "Decompose: build a REST API",
        "Build a REST API for a todo list application with authentication",
        ("tasks", "dependencies"),
    ),
    _CaseSpec(
        "builder_scaffold_001",
        AgentType.WORKER.value,
        "coding",
        "Scaffold a Python class",
        "Generate a UserRepository class with CRUD operations for SQLite",
        ("scaffold_code", "tests"),
    ),
    _CaseSpec(
        "evaluator_review_001",
        AgentType.INSPECTOR.value,
        "review",
        "Review code with eval/exec",
        "Review: def run(code): eval(code)",
        ("issues", "score"),
    ),
    _CaseSpec(
        "researcher_query_001",
        AgentType.WORKER.value,
        "research",
        "Research exponential backoff",
        "Research best practices for implementing exponential backoff in Python",
        ("findings", "recommendations"),
    ),
    _CaseSpec(
        "security_audit_001",
        AgentType.INSPECTOR.value,
        "analysis",
        "Audit SQL injection pattern",
        "Review: query = f'SELECT * FROM users WHERE id = {user_id}'",
        ("vulnerabilities", "remediation"),
    ),
    _CaseSpec(
        "test_gen_001",
        AgentType.WORKER.value,
        "testing",
        "Generate tests for add function",
        "Generate pytest tests for: def add(a, b): return a + b",
        ("test_scripts", "test_files"),
    ),
    _CaseSpec(
        "docs_gen_001",
        AgentType.WORKER.value,
        "documentation",
        "Generate API docs",
        "Document this API endpoint: POST /api/users (creates a user)",
        ("documentation", "examples"),
    ),
    _CaseSpec(
        "devops_ci_001",
        AgentType.WORKER.value,
        "coding",
        "Design GitHub Actions CI pipeline",
        "Design a GitHub Actions CI/CD pipeline for a Python FastAPI application",
        ("pipeline", "stages"),
    ),
    _CaseSpec(
        "vc_commit_001",
        AgentType.WORKER.value,
        "general",
        "Generate commit messages",
        "Generate conventional commit messages for: added user authentication",
        ("commit_messages", "recommendations"),
    ),
    _CaseSpec(
        "error_recovery_001",
        AgentType.WORKER.value,
        "analysis",
        "Analyse ConnectionRefusedError",
        "Error: ConnectionRefusedError: [Errno 111] Connection refused on port 5432",
        ("root_cause", "recovery_strategies"),
    ),
    _CaseSpec(
        "ctx_mgr_001",
        AgentType.WORKER.value,
        "general",
        "Consolidate session context",
        'Consolidate these entries: [\'{"task": "build API", "result": "done"}\']',
        ("summary", "key_facts"),
    ),
)


def _build_default_cases(score_by_keys: Callable[[Any, list[str]], float]) -> list[BenchmarkCase]:
    """Build the default offline benchmark cases with the provided scorer."""
    return [
        BenchmarkCase(
            case_id=spec.case_id,
            agent_type=spec.agent_type,
            task_type=spec.task_type,
            description=spec.description,
            input=spec.input_text,
            evaluator=lambda output, keys=spec.expected_keys: score_by_keys(output, list(keys)),
            expected_keys=list(spec.expected_keys),
        )
        for spec in _DEFAULT_CASE_SPECS
    ]
