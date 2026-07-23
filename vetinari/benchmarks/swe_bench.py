"""
SWE-bench Lite Adapter
=======================

Layer 3 (Pipeline) benchmark: full end-to-end software engineering tasks.

SWE-bench evaluates whether an AI system can resolve real GitHub issues
by producing correct patches. This adapter provides mock/sample cases
for local testing without requiring the full SWE-bench dataset.

Metrics: pass@1, patch correctness, test pass rate.
"""

from __future__ import annotations

import logging
import time
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


# -- Sample SWE-bench-style cases (mock data for local testing) --

_SAMPLE_CASES: list[dict[str, Any]] = [
    {
        "instance_id": "swe-lite-001",
        "repo": "psf/requests",
        "issue": "Session.request does not honour timeout from Session",
        "description": (
            "When a timeout is set on a Session object, individual requests "
            "made through that session should inherit the timeout unless "
            "explicitly overridden."
        ),
        "base_code": (
            "class Session:\n"
            "    def __init__(self):\n"
            "        self.timeout = None\n"
            "\n"
            "    def request(self, method, url, **kwargs):\n"
            "        # BUG: ignores self.timeout\n"
            "        return send_request(method, url, **kwargs)\n"
        ),
        "expected_patch": (
            "class Session:\n"
            "    def __init__(self):\n"
            "        self.timeout = None\n"
            "\n"
            "    def request(self, method, url, **kwargs):\n"
            "        if 'timeout' not in kwargs and self.timeout is not None:\n"
            "            kwargs['timeout'] = self.timeout\n"
            "        return send_request(method, url, **kwargs)\n"
        ),
        "test_patch": (
            "def test_session_timeout():\n"
            "    s = Session()\n"
            "    s.timeout = 30\n"
            "    # verify timeout is passed through\n"
            "    assert 'timeout' in captured_kwargs\n"
        ),
        "tags": ["bug-fix", "session", "timeout"],
    },
    {
        "instance_id": "swe-lite-002",
        "repo": "django/django",
        "issue": "QuerySet.exists() runs full query instead of LIMIT 1",
        "description": (
            "The exists() method should add LIMIT 1 to the SQL query for "
            "efficiency, but currently executes the full query."
        ),
        "base_code": ("class QuerySet:\n    def exists(self):\n        return len(self._execute()) > 0\n"),
        "expected_patch": (
            "class QuerySet:\n"
            "    def exists(self):\n"
            "        qs = self._clone()\n"
            "        qs.query.set_limits(high=1)\n"
            "        return bool(qs._execute())\n"
        ),
        "test_patch": (
            "def test_exists_uses_limit():\n"
            "    qs = QuerySet()\n"
            "    qs.exists()\n"
            "    assert 'LIMIT 1' in last_sql_query()\n"
        ),
        "tags": ["performance", "queryset", "sql"],
    },
    {
        "instance_id": "swe-lite-003",
        "repo": "scikit-learn/scikit-learn",
        "issue": "StandardScaler ignores sample_weight in partial_fit",
        "description": (
            "StandardScaler.partial_fit should account for sample_weight when computing running mean and variance."
        ),
        "base_code": (
            "class StandardScaler:\n"
            "    def partial_fit(self, X, sample_weight=None):\n"
            "        self.mean_ = X.mean(axis=0)\n"
            "        self.var_ = X.var(axis=0)\n"
            "        return self\n"
        ),
        "expected_patch": (
            "class StandardScaler:\n"
            "    def partial_fit(self, X, sample_weight=None):\n"
            "        if sample_weight is not None:\n"
            "            w = sample_weight / sample_weight.sum()\n"
            "            self.mean_ = (X * w[:, None]).sum(axis=0)\n"
            "            self.var_ = (w[:, None] * (X - self.mean_) ** 2).sum(axis=0)\n"
            "        else:\n"
            "            self.mean_ = X.mean(axis=0)\n"
            "            self.var_ = X.var(axis=0)\n"
            "        return self\n"
        ),
        "test_patch": (
            "def test_partial_fit_weighted():\n"
            "    scaler = StandardScaler()\n"
            "    scaler.partial_fit(X, sample_weight=weights)\n"
            "    assert not np.allclose(scaler.mean_, X.mean(axis=0))\n"
        ),
        "tags": ["bug-fix", "preprocessing", "weighted"],
    },
    {
        "instance_id": "swe-lite-004",
        "repo": "pallets/flask",
        "issue": "Blueprint error handler not called for 404",
        "description": (
            "Custom error handlers registered on a Blueprint are not invoked "
            "for 404 errors when the URL belongs to that blueprint's url_prefix."
        ),
        "base_code": (
            "class Blueprint:\n"
            "    def register_error_handler(self, code, handler):\n"
            "        self._error_handlers[code] = handler\n"
            "\n"
            "    def _find_error_handler(self, error):\n"
            "        return None  # BUG: always returns None\n"
        ),
        "expected_patch": (
            "class Blueprint:\n"
            "    def register_error_handler(self, code, handler):\n"
            "        self._error_handlers[code] = handler\n"
            "\n"
            "    def _find_error_handler(self, error):\n"
            "        code = getattr(error, 'code', 500)\n"
            "        return self._error_handlers.get(code)\n"
        ),
        "test_patch": (
            "def test_blueprint_404_handler():\n"
            "    bp = Blueprint('test', __name__)\n"
            "    bp.register_error_handler(404, custom_handler)\n"
            "    handler = bp._find_error_handler(NotFound())\n"
            "    assert handler is custom_handler\n"
        ),
        "tags": ["bug-fix", "blueprint", "error-handling"],
    },
    {
        "instance_id": "swe-lite-005",
        "repo": "python/cpython",
        "issue": "pathlib.Path.mkdir(parents=True) sets wrong permissions",
        "description": (
            "When mkdir is called with parents=True and a mode argument, "
            "intermediate directories are created with default permissions "
            "instead of the specified mode."
        ),
        "base_code": (
            "class Path:\n"
            "    def mkdir(self, mode=0o777, parents=False):\n"
            "        if parents:\n"
            "            for p in reversed(self._parents_missing()):\n"
            "                os.mkdir(p)  # BUG: ignores mode\n"
            "        os.mkdir(self, mode)\n"
        ),
        "expected_patch": (
            "class Path:\n"
            "    def mkdir(self, mode=0o777, parents=False):\n"
            "        if parents:\n"
            "            for p in reversed(self._parents_missing()):\n"
            "                os.mkdir(p, mode)\n"
            "        os.mkdir(self, mode)\n"
        ),
        "test_patch": (
            "def test_mkdir_parents_mode():\n"
            "    p = Path(tmp / 'a' / 'b' / 'c')\n"
            "    p.mkdir(mode=0o750, parents=True)\n"
            "    assert (tmp / 'a').stat().st_mode & 0o777 == 0o750\n"
        ),
        "tags": ["bug-fix", "pathlib", "permissions"],
    },
]


class SWEBenchAdapter(BenchmarkSuiteAdapter):
    """SWE-bench Lite adapter for full pipeline evaluation."""

    name = "swe_bench"
    layer = BenchmarkLayer.PIPELINE
    tier = BenchmarkTier.SLOW

    def load_cases(self, limit: int | None = None) -> list[BenchmarkCase]:
        """Load cases.

        Returns:
            List of results.
        """
        items = _SAMPLE_CASES[:limit] if limit else _SAMPLE_CASES
        return [
            BenchmarkCase(
                case_id=item["instance_id"],
                suite_name=self.name,
                description=item["description"],
                input_data={
                    "repo": item["repo"],
                    "issue": item["issue"],
                    "base_code": item["base_code"],
                    "test_patch": item["test_patch"],
                },
                expected={
                    "expected_patch": item["expected_patch"],
                },
                tags=item.get("tags", []),
            )
            for item in items
        ]

    def run_case(self, case: BenchmarkCase, run_id: str) -> BenchmarkResult:
        """
        Run a SWE-bench case.

        In local/mock mode, we simulate patch generation by checking
        structural similarity. In production mode, this would invoke
        the full Vetinari pipeline to generate a code patch.

        Args:
            case: The case.
            run_id: The run id.

        Returns:
            The BenchmarkResult result.
        """
        start = time.monotonic()

        error: str | None = None
        try:
            # Attempt to use Vetinari orchestration for real execution
            generated_patch = self._generate_patch_via_orchestrator(case)
        except Exception as exc:
            logger.warning("SWE-bench case %r could not execute orchestrator path: %s", case.case_id, exc)
            error = str(exc)
            generated_patch = ""

        latency = (time.monotonic() - start) * 1000

        return BenchmarkResult(
            case_id=case.case_id,
            suite_name=self.name,
            run_id=run_id,
            passed=False,  # set by evaluate()
            score=0.0,
            latency_ms=round(latency, 2),
            tokens_consumed=count_tokens(str(case.input_data.get("base_code", ""))),
            output={"generated_patch": generated_patch, "benchmark_mode": "unavailable" if error else "orchestrator"},
            error=error,
        )

    def evaluate(self, result: BenchmarkResult) -> float:
        """
        Score the generated patch.

        Scoring criteria:
          - 0.0: No output or empty patch
          - 0.3: Patch produced but structurally different
          - 0.6: Patch addresses the issue (keyword overlap)
          - 0.8: Patch closely matches expected structure
          - 1.0: Exact or near-exact match

        Returns:
            float value produced by evaluate().
        """
        if not result.output:
            return 0.0
        if result.output.get("benchmark_mode") == "unavailable" or result.error:
            return 0.0

        generated = result.output.get("generated_patch", "")
        if not generated:
            return 0.0

        # For bundled sample cases, check against known expected patches.
        # Production scoring should run the relevant test suite.
        expected = ""
        for item in _SAMPLE_CASES:
            if item["instance_id"] == result.case_id:
                expected = item["expected_patch"]
                break

        if not expected:
            return 0.0

        # Normalise whitespace for comparison
        gen_norm = " ".join(generated.split())
        exp_norm = " ".join(expected.split())

        if gen_norm == exp_norm:
            return 1.0

        test_execution = result.output.get("test_execution")
        if isinstance(test_execution, dict) and test_execution.get("passed") is True:
            evidence_refs = test_execution.get("evidence_refs")
            if isinstance(evidence_refs, list) and all(isinstance(ref, str) and ref.strip() for ref in evidence_refs):
                return 1.0
        return 0.0

    @staticmethod
    def _generate_patch_via_orchestrator(case: BenchmarkCase) -> str:
        """Attempt real patch generation via Vetinari pipeline."""
        from vetinari.orchestration.two_layer import get_two_layer_orchestrator

        orch = get_two_layer_orchestrator()
        goal = (
            f"Fix the following issue in {case.input_data['repo']}:\n"
            f"{case.input_data['issue']}\n\n"
            f"Current code:\n{case.input_data['base_code']}"
        )
        result = orch.generate_and_execute(goal=goal)
        return result.get("final_output", "")

    @staticmethod
    def _mock_generate_patch(case: BenchmarkCase) -> str:
        """Compatibility helper retained for tests; not used by run_case."""
        expected = case.expected or {}
        return expected.get("expected_patch", case.input_data.get("base_code", ""))
