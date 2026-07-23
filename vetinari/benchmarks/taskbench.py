"""
TaskBench Decomposition Quality Adapter
=========================================

Layer 1 (Agent) benchmark: task decomposition quality.

TaskBench evaluates an agent's ability to break down complex goals into
well-structured, dependency-aware subtask graphs. It measures:

  - Decomposition completeness: all necessary subtasks present
  - Dependency correctness: proper ordering and dependencies
  - Granularity: appropriate level of detail (not too coarse, not too fine)
  - Executability: each subtask is actionable

Metrics: decomposition score, dependency accuracy, granularity score.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

from vetinari.benchmarks.runner import (
    BenchmarkCase,
    BenchmarkLayer,
    BenchmarkResult,
    BenchmarkSuiteAdapter,
    BenchmarkTier,
)
from vetinari.context import count_tokens

logger = logging.getLogger(__name__)

DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
TASKBENCH_WORKFLOW_GUARDS: tuple[str, ...] = (
    "planner execution failures produce unavailable benchmark output",
    "unavailable benchmark output scores zero",
    "dependency scoring checks expected edges explicitly",
    "DAG scoring rejects missing dependency nodes and cycles",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return TaskBench workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/benchmarks/taskbench.py",
        "guards": TASKBENCH_WORKFLOW_GUARDS,
    }


# -- Sample TaskBench cases --

_SAMPLE_CASES: list[dict[str, Any]] = [
    {
        "task_id": "tb-decomp-001",
        "goal": "Build a user authentication system with JWT tokens",
        "expected_subtasks": [
            "design_user_schema",
            "implement_password_hashing",
            "create_user_registration_endpoint",
            "create_login_endpoint",
            "implement_jwt_token_generation",
            "implement_jwt_token_validation",
            "create_auth_middleware",
            "write_tests",
        ],
        "expected_dependencies": {
            "implement_password_hashing": ["design_user_schema"],
            "create_user_registration_endpoint": ["design_user_schema", "implement_password_hashing"],
            "create_login_endpoint": [
                "design_user_schema",
                "implement_password_hashing",
                "implement_jwt_token_generation",
            ],
            "implement_jwt_token_validation": ["implement_jwt_token_generation"],
            "create_auth_middleware": ["implement_jwt_token_validation"],
            "write_tests": [
                "create_user_registration_endpoint",
                "create_login_endpoint",
                "create_auth_middleware",
            ],
        },
        "min_subtasks": 5,
        "max_subtasks": 15,
        "tags": ["web", "auth", "backend"],
    },
    {
        "task_id": "tb-decomp-002",
        "goal": "Create a data pipeline that ingests CSV files, validates them, transforms data, and loads into PostgreSQL",
        "expected_subtasks": [
            "setup_file_watcher",
            "implement_csv_parser",
            "define_validation_schema",
            "implement_data_validation",
            "implement_data_transformation",
            "setup_database_connection",
            "create_database_schema",
            "implement_data_loader",
            "implement_error_handling",
            "write_tests",
        ],
        "expected_dependencies": {
            "implement_csv_parser": ["setup_file_watcher"],
            "implement_data_validation": ["implement_csv_parser", "define_validation_schema"],
            "implement_data_transformation": ["implement_data_validation"],
            "create_database_schema": ["setup_database_connection"],
            "implement_data_loader": ["implement_data_transformation", "create_database_schema"],
            "implement_error_handling": ["implement_data_loader"],
            "write_tests": ["implement_data_loader", "implement_error_handling"],
        },
        "min_subtasks": 6,
        "max_subtasks": 18,
        "tags": ["data", "etl", "pipeline"],
    },
    {
        "task_id": "tb-decomp-003",
        "goal": "Deploy a containerized microservice with CI/CD, health checks, and monitoring",
        "expected_subtasks": [
            "write_dockerfile",
            "create_docker_compose",
            "setup_ci_pipeline",
            "implement_health_endpoint",
            "configure_monitoring",
            "setup_alerting",
            "write_deployment_script",
            "configure_load_balancer",
        ],
        "expected_dependencies": {
            "create_docker_compose": ["write_dockerfile"],
            "setup_ci_pipeline": ["write_dockerfile"],
            "configure_monitoring": ["implement_health_endpoint"],
            "setup_alerting": ["configure_monitoring"],
            "write_deployment_script": ["create_docker_compose", "setup_ci_pipeline"],
            "configure_load_balancer": ["write_deployment_script"],
        },
        "min_subtasks": 5,
        "max_subtasks": 14,
        "tags": ["devops", "docker", "cicd"],
    },
    {
        "task_id": "tb-decomp-004",
        "goal": "Implement a real-time chat application with WebSockets",
        "expected_subtasks": [
            "design_message_schema",
            "setup_websocket_server",
            "implement_connection_manager",
            "implement_message_broadcasting",
            "implement_room_system",
            "add_user_presence",
            "implement_message_persistence",
            "write_client_library",
            "write_tests",
        ],
        "expected_dependencies": {
            "implement_connection_manager": ["setup_websocket_server"],
            "implement_message_broadcasting": ["implement_connection_manager", "design_message_schema"],
            "implement_room_system": ["implement_connection_manager"],
            "add_user_presence": ["implement_connection_manager"],
            "implement_message_persistence": ["design_message_schema", "implement_message_broadcasting"],
            "write_client_library": ["setup_websocket_server", "implement_message_broadcasting"],
            "write_tests": ["implement_message_broadcasting", "implement_room_system"],
        },
        "min_subtasks": 5,
        "max_subtasks": 15,
        "tags": ["realtime", "websocket", "chat"],
    },
    {
        "task_id": "tb-decomp-005",
        "goal": "Build a REST API with rate limiting, caching, and pagination",
        "expected_subtasks": [
            "design_api_schema",
            "implement_base_endpoints",
            "implement_pagination",
            "implement_rate_limiter",
            "implement_response_caching",
            "add_error_handling",
            "write_api_documentation",
            "write_tests",
        ],
        "expected_dependencies": {
            "implement_base_endpoints": ["design_api_schema"],
            "implement_pagination": ["implement_base_endpoints"],
            "implement_rate_limiter": ["implement_base_endpoints"],
            "implement_response_caching": ["implement_base_endpoints"],
            "add_error_handling": ["implement_base_endpoints"],
            "write_api_documentation": [
                "implement_pagination",
                "implement_rate_limiter",
                "implement_response_caching",
            ],
            "write_tests": [
                "implement_pagination",
                "implement_rate_limiter",
                "implement_response_caching",
                "add_error_handling",
            ],
        },
        "min_subtasks": 5,
        "max_subtasks": 14,
        "tags": ["api", "rest", "backend"],
    },
]


class TaskBenchAdapter(BenchmarkSuiteAdapter):
    """TaskBench adapter for decomposition quality evaluation."""

    name = "taskbench"
    layer = BenchmarkLayer.AGENT
    tier = BenchmarkTier.FAST

    def load_cases(self, limit: int | None = None) -> list[BenchmarkCase]:
        """Load cases.

        Returns:
            List of results.
        """
        items = _SAMPLE_CASES[:limit] if limit else _SAMPLE_CASES
        return [
            BenchmarkCase(
                case_id=item["task_id"],
                suite_name=self.name,
                description=item["goal"],
                input_data={"goal": item["goal"]},
                expected={
                    "expected_subtasks": item["expected_subtasks"],
                    "expected_dependencies": item["expected_dependencies"],
                    "min_subtasks": item["min_subtasks"],
                    "max_subtasks": item["max_subtasks"],
                },
                tags=item.get("tags", []),
            )
            for item in items
        ]

    def run_case(self, case: BenchmarkCase, run_id: str) -> BenchmarkResult:
        """Run a TaskBench decomposition case.

        Args:
            case: The case.
            run_id: The run id.

        Returns:
            The BenchmarkResult result.
        """
        start = time.monotonic()

        error: str | None = None
        try:
            result_data = self._run_via_planner(case)
        except Exception as exc:
            logger.warning("TaskBench case %r could not execute planner path: %s", case.case_id, exc)
            error = str(exc)
            result_data = {
                "error": error,
                "subtasks": [],
                "dependencies": {},
                "benchmark_mode": "unavailable",
            }

        latency = (time.monotonic() - start) * 1000

        return BenchmarkResult(
            case_id=case.case_id,
            suite_name=self.name,
            run_id=run_id,
            passed=False,
            score=0.0,
            latency_ms=round(latency, 2),
            tokens_consumed=count_tokens(str(case.input_data.get("goal", ""))),
            output=result_data,
            error=error,
        )

    def evaluate(self, result: BenchmarkResult) -> float:
        """
        Score decomposition quality.

        Scoring breakdown:
          - 0.30: Completeness — expected subtask coverage
          - 0.25: Dependency correctness
          - 0.25: Granularity — within acceptable range
          - 0.20: DAG validity — no cycles, all deps exist

        Returns:
            float value produced by evaluate().
        """
        if not result.output:
            return 0.0
        if result.output.get("benchmark_mode") == "unavailable" or result.error:
            return 0.0

        expected = None
        for item in _SAMPLE_CASES:
            if item["task_id"] == result.case_id:
                expected = item
                break

        if expected is None:
            return 0.0

        subtasks = result.output.get("subtasks", [])
        dependencies = result.output.get("dependencies", {})
        subtask_names: set[str] = set(subtasks)

        score = 0.0

        # Completeness (0.30): what fraction of expected subtasks are present
        expected_subtasks = set(expected["expected_subtasks"])
        if expected_subtasks:
            found = len(expected_subtasks & subtask_names)
            completeness = found / len(expected_subtasks)
            score += 0.30 * completeness

        # Dependency correctness (0.25)
        expected_deps = expected["expected_dependencies"]
        if expected_deps:
            correct_deps = 0
            total_dep_edges = 0
            for task, deps in expected_deps.items():
                total_dep_edges += len(deps)
                actual_deps = set(dependencies.get(task, []))
                for d in deps:
                    if d in actual_deps:
                        correct_deps += 1
            dep_score = correct_deps / max(total_dep_edges, 1)
            score += 0.25 * dep_score

        # Granularity (0.25): count within [min, max] range
        min_st = expected.get("min_subtasks", 3)
        max_st = expected.get("max_subtasks", 20)
        n = len(subtasks)
        if min_st <= n <= max_st:
            granularity = 1.0
        elif n < min_st:
            granularity = max(0.0, n / min_st)
        else:
            granularity = max(0.0, 1.0 - (n - max_st) / max_st)
        score += 0.25 * granularity

        # DAG validity (0.20): no cycles, all deps reference existing tasks
        dag_valid = self._check_dag(subtask_names, dependencies)
        score += 0.20 * (1.0 if dag_valid else 0.0)

        return round(min(score, 1.0), 4)

    @staticmethod
    def _check_dag(subtask_names: set[str], dependencies: dict[str, list[str]]) -> bool:
        """Check if the dependency graph is a valid DAG."""
        # All referenced deps must exist in subtask set
        for task, deps in dependencies.items():
            if task not in subtask_names:
                return False
            for d in deps:
                if d not in subtask_names:
                    return False

        # Cycle detection via topological sort (Kahn's algorithm)
        in_degree: dict[str, int] = dict.fromkeys(subtask_names, 0)
        dependents_by_task: dict[str, list[str]] = {task: [] for task in subtask_names}
        for task, deps in dependencies.items():
            in_degree.setdefault(task, 0)
            for dep in deps:
                in_degree[task] += 1
                dependents_by_task.setdefault(dep, []).append(task)

        queue = deque(s for s, d in in_degree.items() if d == 0)
        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for task in dependents_by_task.get(node, []):
                in_degree[task] -= 1
                if in_degree[task] == 0:
                    queue.append(task)

        return visited == len(subtask_names)

    @staticmethod
    def _run_via_planner(case: BenchmarkCase) -> dict[str, Any]:
        """Attempt decomposition via Vetinari planner."""
        from vetinari.planning.plan_mode import PlanModeEngine
        from vetinari.planning.plan_types import PlanGenerationRequest

        engine = PlanModeEngine()
        plan = engine.generate_plan(PlanGenerationRequest(goal=case.input_data["goal"]))
        return {
            "subtasks": [s.subtask_id for s in plan.subtasks],
            "dependencies": plan.dependencies or {},
        }
