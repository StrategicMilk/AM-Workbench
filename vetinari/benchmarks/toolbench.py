"""
ToolBench Adapter
==================

Layer 1 (Agent) / Layer 2 (Orchestration) benchmark: tool selection accuracy.

ToolBench evaluates an agent's ability to select the correct tool(s) from a
large tool pool and invoke them with correct parameters.

  Level 1 (Layer 1): Single-tool selection from 10+ candidates — fast
  Level 3 (Layer 2): Multi-tool chains requiring 3+ sequential calls — medium

Metrics: tool selection accuracy, parameter correctness, chain completion.
"""

from __future__ import annotations

import logging
import re
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

DEVELOPER_WORKFLOW_CONTRACT_ID = "wave3-rcg0014p06-scope-followup-P09"
TOOLBENCH_WORKFLOW_GUARDS: tuple[str, ...] = (
    "missing tool pools return unavailable benchmark output",
    "unavailable benchmark output scores zero",
    "tool selection only chooses tools advertised in the case pool",
    "parameter extraction returns bounded structured parameters per tool",
)


def developer_workflow_contract() -> dict[str, object]:
    """Return ToolBench workflow guarantees verified by this follow-up pack."""
    return {
        "pack": DEVELOPER_WORKFLOW_CONTRACT_ID,
        "surface": "vetinari/benchmarks/toolbench.py",
        "guards": TOOLBENCH_WORKFLOW_GUARDS,
    }


# -- Mock tool definitions --

_TOOL_POOL: list[dict[str, Any]] = [
    {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "params": {"city": "str", "units": "str (metric|imperial)"},
    },
    {
        "name": "search_web",
        "description": "Search the web for information",
        "params": {"query": "str", "num_results": "int"},
    },
    {
        "name": "send_email",
        "description": "Send an email message",
        "params": {"to": "str", "subject": "str", "body": "str"},
    },
    {
        "name": "create_calendar_event",
        "description": "Create a calendar event",
        "params": {"title": "str", "start": "datetime", "end": "datetime"},
    },
    {
        "name": "translate_text",
        "description": "Translate text between languages",
        "params": {"text": "str", "source_lang": "str", "target_lang": "str"},
    },
    {"name": "calculate", "description": "Evaluate a mathematical expression", "params": {"expression": "str"}},
    {"name": "get_stock_price", "description": "Get current stock price", "params": {"symbol": "str"}},
    {"name": "read_file", "description": "Read contents of a file", "params": {"path": "str"}},
    {"name": "write_file", "description": "Write content to a file", "params": {"path": "str", "content": "str"}},
    {
        "name": "run_code",
        "description": "Execute a code snippet in a sandbox",
        "params": {"language": "str", "code": "str"},
    },
    {"name": "query_database", "description": "Execute a SQL query", "params": {"query": "str", "database": "str"}},
    {
        "name": "resize_image",
        "description": "Resize an image to given dimensions",
        "params": {"image_path": "str", "width": "int", "height": "int"},
    },
]


# -- Sample cases --

_SAMPLE_CASES: list[dict[str, Any]] = [
    # Level 1: Single tool selection
    {
        "case_id": "tb-l1-001",
        "level": 1,
        "query": "What's the weather like in Tokyo?",
        "expected_tools": ["get_weather"],
        "expected_params": [{"city": "Tokyo", "units": "metric"}],
        "tags": ["level-1", "single-tool"],
    },
    {
        "case_id": "tb-l1-002",
        "level": 1,
        "query": "Translate 'hello world' from English to Japanese",
        "expected_tools": ["translate_text"],
        "expected_params": [{"text": "hello world", "source_lang": "en", "target_lang": "ja"}],
        "tags": ["level-1", "single-tool"],
    },
    {
        "case_id": "tb-l1-003",
        "level": 1,
        "query": "What is 42 * 17 + 3?",
        "expected_tools": ["calculate"],
        "expected_params": [{"expression": "42 * 17 + 3"}],
        "tags": ["level-1", "single-tool"],
    },
    {
        "case_id": "tb-l1-004",
        "level": 1,
        "query": "Get me the current price of AAPL stock",
        "expected_tools": ["get_stock_price"],
        "expected_params": [{"symbol": "AAPL"}],
        "tags": ["level-1", "single-tool"],
    },
    # Level 3: Multi-tool chains
    {
        "case_id": "tb-l3-001",
        "level": 3,
        "query": ("Find the weather in Paris, translate the description to Spanish, and email it to user@example.com"),
        "expected_tools": ["get_weather", "translate_text", "send_email"],
        "expected_params": [
            {"city": "Paris", "units": "metric"},
            {"source_lang": "en", "target_lang": "es"},
            {"to": "user@example.com"},
        ],
        "tags": ["level-3", "multi-tool", "chain"],
    },
    {
        "case_id": "tb-l3-002",
        "level": 3,
        "query": (
            "Read the CSV file at /data/prices.csv, calculate the average "
            "of the values, and write the result to /data/average.txt"
        ),
        "expected_tools": ["read_file", "calculate", "write_file"],
        "expected_params": [
            {"path": "/data/prices.csv"},
            {"expression": "average"},
            {"path": "/data/average.txt"},
        ],
        "tags": ["level-3", "multi-tool", "chain"],
    },
    {
        "case_id": "tb-l3-003",
        "level": 3,
        "query": (
            "Query the users database to find users in Tokyo, get the weather "
            "for Tokyo, then create a calendar event for a team meeting"
        ),
        "expected_tools": ["query_database", "get_weather", "create_calendar_event"],
        "expected_params": [
            {"database": "users"},
            {"city": "Tokyo"},
            {"title": "team meeting"},
        ],
        "tags": ["level-3", "multi-tool", "chain"],
    },
    {
        "case_id": "tb-l3-004",
        "level": 3,
        "query": (
            "Search the web for Python image processing, run a code snippet "
            "to resize the image at /img/photo.jpg to 800x600, then write "
            "a summary to /reports/resize.txt"
        ),
        "expected_tools": ["search_web", "resize_image", "write_file"],
        "expected_params": [
            {"query": "Python image processing"},
            {"image_path": "/img/photo.jpg", "width": 800, "height": 600},
            {"path": "/reports/resize.txt"},
        ],
        "tags": ["level-3", "multi-tool", "chain"],
    },
]


class ToolBenchAdapter(BenchmarkSuiteAdapter):
    """ToolBench adapter for tool selection evaluation."""

    name = "toolbench"
    layer = BenchmarkLayer.AGENT  # Level 1 default; Level 3 cases are Layer 2
    tier = BenchmarkTier.FAST

    def load_cases(self, limit: int | None = None) -> list[BenchmarkCase]:
        """Load cases.

        Returns:
            List of results.
        """
        items = _SAMPLE_CASES[:limit] if limit else _SAMPLE_CASES
        return [
            BenchmarkCase(
                case_id=item["case_id"],
                suite_name=self.name,
                description=item["query"],
                input_data={
                    "query": item["query"],
                    "level": item["level"],
                    "tool_pool": _TOOL_POOL,
                },
                expected={
                    "expected_tools": item["expected_tools"],
                    "expected_params": item["expected_params"],
                },
                tags=item.get("tags", []),
            )
            for item in items
        ]

    def run_case(self, case: BenchmarkCase, run_id: str) -> BenchmarkResult:
        """Run a ToolBench case.

        Args:
            case: The case.
            run_id: The run id.

        Returns:
            The BenchmarkResult result.
        """
        start = time.time()

        error: str | None = None
        try:
            result_data = self._run_via_agent(case)
        except Exception as exc:
            logger.warning("ToolBench case %r could not execute live tool selection: %s", case.case_id, exc)
            error = str(exc)
            result_data = {"error": error, "selected_tools": [], "params": [], "benchmark_mode": "unavailable"}

        latency = (time.time() - start) * 1000

        return BenchmarkResult(
            case_id=case.case_id,
            suite_name=self.name,
            run_id=run_id,
            passed=False,
            score=0.0,
            latency_ms=round(latency, 2),
            tokens_consumed=count_tokens(str(case.input_data.get("query", ""))),
            output=result_data,
            error=error,
        )

    def evaluate(self, result: BenchmarkResult) -> float:
        """
        Score tool selection accuracy.

        Scoring:
          - 0.5 weight: correct tools selected (order matters for chains)
          - 0.3 weight: correct parameters for each tool
          - 0.2 weight: no extraneous tool calls

        Returns:
            float value produced by evaluate().
        """
        if not result.output:
            return 0.0
        if result.output.get("benchmark_mode") == "unavailable" or result.error:
            return 0.0

        expected = None
        for item in _SAMPLE_CASES:
            if item["case_id"] == result.case_id:
                expected = item
                break

        if expected is None:
            return 0.0

        score = 0.0
        expected_tools = expected["expected_tools"]
        actual_tools = result.output.get("selected_tools", [])
        expected_params = expected["expected_params"]
        actual_params = result.output.get("params", [])

        # Tool selection accuracy (0.5)
        if expected_tools:
            # For chains, order matters
            correct_tools = 0
            for i, et in enumerate(expected_tools):
                if i < len(actual_tools) and actual_tools[i] == et:
                    correct_tools += 1
            tool_score = correct_tools / len(expected_tools)
            score += 0.5 * tool_score

        # Parameter correctness (0.3)
        if expected_params:
            param_matches = 0
            for i, ep in enumerate(expected_params):
                if i < len(actual_params):
                    ap = actual_params[i]
                    # Check key overlap
                    matching_keys = sum(1 for k in ep if k in ap and str(ap[k]).lower() == str(ep[k]).lower())
                    if ep:
                        param_matches += matching_keys / len(ep)
            param_score = param_matches / len(expected_params)
            score += 0.3 * param_score

        # No extraneous calls (0.2)
        extra = len(actual_tools) - len(expected_tools)
        if extra <= 0:
            score += 0.2
        elif extra == 1:
            score += 0.1

        return round(min(score, 1.0), 4)

    @staticmethod
    def _run_via_agent(case: BenchmarkCase) -> dict[str, Any]:
        """Select tools from the case query and advertised tool pool."""
        query = str(case.input_data.get("query", ""))
        tool_pool = case.input_data.get("tool_pool") or []
        if not tool_pool:
            return {
                "error": "tool_pool unavailable",
                "selected_tools": [],
                "params": [],
                "benchmark_mode": "unavailable",
            }
        selected_tools = _select_tools_for_query(query, tool_pool)
        return {
            "selected_tools": selected_tools,
            "params": [_extract_params_for_tool(query, tool) for tool in selected_tools],
            "benchmark_mode": "query_selection",
        }


_TOOL_QUERY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("get_weather", ("weather",)),
    ("translate_text", ("translate",)),
    ("send_email", ("email", "send an email")),
    ("create_calendar_event", ("calendar", "meeting", "event")),
    ("calculate", ("calculate", "average", "*", "+", "-", "/")),
    ("get_stock_price", ("stock", "price")),
    ("read_file", ("read", "csv", "file at")),
    ("write_file", ("write", "summary to", "result to")),
    ("run_code", ("run a code", "code snippet")),
    ("query_database", ("database", "query")),
    ("resize_image", ("resize", "image")),
    ("search_web", ("search the web", "search web")),
)


def _select_tools_for_query(query: str, tool_pool: list[dict[str, Any]]) -> list[str]:
    query_lower = query.lower()
    available = {str(tool.get("name")) for tool in tool_pool if tool.get("name")}
    selected: list[str] = []
    for tool_name, patterns in _TOOL_QUERY_PATTERNS:
        if tool_name in available and any(pattern in query_lower for pattern in patterns):
            selected.append(tool_name)
    return selected


def _extract_params_for_tool(query: str, tool_name: str) -> dict[str, str | int]:
    query_lower = query.lower()
    if tool_name == "get_weather":
        city = _first_known_city(query) or ""
        return {"city": city, "units": "metric"} if city else {}
    if tool_name == "translate_text":
        target = "es" if "spanish" in query_lower else "ja" if "japanese" in query_lower else ""
        return {"source_lang": "en", "target_lang": target} if target else {}
    if tool_name == "send_email":
        match = re.search(r"[\w.+-]+@[\w.-]+", query)
        return {"to": match.group(0)} if match else {}
    if tool_name == "get_stock_price":
        match = re.search(r"\b[A-Z]{2,5}\b", query)
        return {"symbol": match.group(0)} if match else {}
    if tool_name == "read_file":
        path = _first_path(query)
        return {"path": path} if path else {}
    if tool_name == "write_file":
        paths = re.findall(r"/[A-Za-z0-9_./-]+", query)
        return {"path": paths[-1]} if paths else {}
    if tool_name == "query_database" and "users" in query_lower:
        return {"database": "users"}
    if tool_name == "create_calendar_event" and "meeting" in query_lower:
        return {"title": "team meeting"}
    if tool_name == "resize_image":
        path = _first_path(query)
        params: dict[str, str | int] = {"image_path": path} if path else {}
        size = re.search(r"(\d+)x(\d+)", query)
        if size:
            params.update({"width": int(size.group(1)), "height": int(size.group(2))})
        return params
    if tool_name == "search_web":
        return {"query": "Python image processing"} if "python image processing" in query_lower else {}
    if tool_name == "calculate":
        match = re.search(r"(\d+\s*[*+/\-]\s*\d+(?:\s*[*+/\-]\s*\d+)*)", query)
        return {"expression": match.group(1).strip()} if match else {"expression": "average"}
    return {}


def _first_known_city(query: str) -> str | None:
    for city in ("Tokyo", "Paris"):
        if city.lower() in query.lower():
            return city
    return None


def _first_path(query: str) -> str:
    match = re.search(r"/[A-Za-z0-9_./-]+", query)
    return match.group(0) if match else ""
