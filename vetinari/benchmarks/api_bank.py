"""
API-Bank Multi-Step Tool Calling Adapter
==========================================

Layer 2 (Orchestration) benchmark: multi-step API calling.

API-Bank evaluates an agent's ability to:
  1. Understand user intent and map to API calls
  2. Chain multiple API calls where outputs feed into subsequent calls
  3. Handle authentication, pagination, and error recovery
  4. Produce correct final answers from multi-step API interactions

Level 3 cases require 3+ chained API calls with data dependencies.

Metrics: API selection accuracy, parameter extraction, chain completion,
         final answer correctness.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from vetinari.benchmarks.runner import (
    BenchmarkCase,
    BenchmarkLayer,
    BenchmarkResult,
    BenchmarkSuiteAdapter,
    BenchmarkTier,
)
from vetinari.boundary_guards import require_nonempty
from vetinari.context import count_tokens
from vetinari.security.fail_closed import assert_closed_schema, sanitize_untrusted_text
from vetinari.types import StatusEnum

logger = logging.getLogger(__name__)


def _future_mock_expiry_iso(*, days: int = 30) -> str:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return (base + timedelta(days=days)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# -- Mock API definitions --

_API_CATALOG: list[dict[str, Any]] = [
    {
        "name": "UserService.get_user",
        "description": "Get user profile by user ID",
        "params": {"user_id": "int"},
        "returns": {"name": "str", "email": "str", "plan": "str"},
    },
    {
        "name": "UserService.search_users",
        "description": "Search users by name or email",
        "params": {"query": "str", "limit": "int"},
        "returns": {"users": "list[{id, name, email}]"},
    },
    {
        "name": "OrderService.get_orders",
        "description": "Get orders for a user",
        "params": {"user_id": "int", "status": "str (optional)"},
        "returns": {"orders": "list[{order_id, total, status, items}]"},
    },
    {
        "name": "OrderService.get_order_details",
        "description": "Get detailed info for a specific order",
        "params": {"order_id": "int"},
        "returns": {"order_id": "int", "items": "list", "total": "float", "address": "str"},
    },
    {
        "name": "PaymentService.get_payment_status",
        "description": "Check payment status for an order",
        "params": {"order_id": "int"},
        "returns": {"status": "str", "method": "str", "amount": "float"},
    },
    {
        "name": "PaymentService.process_refund",
        "description": "Process a refund for an order",
        "params": {"order_id": "int", "amount": "float", "reason": "str"},
        "returns": {"refund_id": "str", "status": "str"},
    },
    {
        "name": "InventoryService.check_stock",
        "description": "Check stock level for a product",
        "params": {"product_id": "int"},
        "returns": {"product_id": "int", "available": "int", "warehouse": "str"},
    },
    {
        "name": "InventoryService.reserve_stock",
        "description": "Reserve stock for an order",
        "params": {"product_id": "int", "quantity": "int"},
        "returns": {"reservation_id": "str", "expires_at": "datetime"},
    },
    {
        "name": "NotificationService.send_email",
        "description": "Send an email notification",
        "params": {"to": "str", "subject": "str", "body": "str"},
        "returns": {"message_id": "str", "sent": "bool"},
    },
    {
        "name": "AnalyticsService.get_user_stats",
        "description": "Get analytics for a user's activity",
        "params": {"user_id": "int", "period": "str"},
        "returns": {"orders_count": "int", "total_spent": "float", "avg_order": "float"},
    },
]


# -- Sample cases --

_SAMPLE_CASES: list[dict[str, Any]] = [
    {
        "case_id": "ab-l1-001",
        "level": 1,
        "description": "Single API call: look up a user",
        "user_query": "Find the email address for user ID 42",
        "expected_api_chain": [
            {"api": "UserService.get_user", "params": {"user_id": 42}},
        ],
        "expected_answer": {"email": "user42@example.com"},
        "mock_responses": [
            {"name": "User 42", "email": "user42@example.com", "plan": "premium"},
        ],
        "tags": ["level-1", "single-api"],
    },
    {
        "case_id": "ab-l2-001",
        "level": 2,
        "description": "Two-step: find user then get their orders",
        "user_query": "How many orders does user john@acme.com have?",
        "expected_api_chain": [
            {"api": "UserService.search_users", "params": {"query": "john@acme.com", "limit": 1}},
            {"api": "OrderService.get_orders", "params": {"user_id": 101}},
        ],
        "expected_answer": {"orders_count": 5},
        "mock_responses": [
            {"users": [{"id": 101, "name": "John", "email": "john@acme.com"}]},
            {"orders": [{"order_id": i} for i in range(5)]},
        ],
        "tags": ["level-2", "two-step"],
    },
    {
        "case_id": "ab-l3-001",
        "level": 3,
        "description": "Three-step: find user, get order, process refund",
        "user_query": (
            "User john@acme.com wants a full refund on their most recent order. "
            "Process the refund and email them a confirmation."
        ),
        "expected_api_chain": [
            {"api": "UserService.search_users", "params": {"query": "john@acme.com", "limit": 1}},
            {"api": "OrderService.get_orders", "params": {"user_id": 101, "status": StatusEnum.COMPLETED.value}},
            {
                "api": "PaymentService.process_refund",
                "params": {"order_id": 5001, "amount": 89.99, "reason": "customer request"},
            },
            {
                "api": "NotificationService.send_email",
                "params": {
                    "to": "john@acme.com",
                    "subject": "Refund Confirmation",
                },
            },
        ],
        "expected_answer": {
            "refund_id": "ref-001",
            "refund_amount": 89.99,
            "notification_sent": True,
        },
        "mock_responses": [
            {"users": [{"id": 101, "name": "John", "email": "john@acme.com"}]},
            {"orders": [{"order_id": 5001, "total": 89.99, "status": StatusEnum.COMPLETED.value}]},
            {"refund_id": "ref-001", "status": "processed"},
            {"message_id": "msg-001", "sent": True},
        ],
        "tags": ["level-3", "multi-step", "refund"],
    },
    {
        "case_id": "ab-l3-002",
        "level": 3,
        "description": "Three-step: check stock, reserve, and notify",
        "user_query": (
            "Check if product 777 is in stock. If available, reserve 3 units "
            "and email warehouse@company.com about the reservation."
        ),
        "expected_api_chain": [
            {"api": "InventoryService.check_stock", "params": {"product_id": 777}},
            {"api": "InventoryService.reserve_stock", "params": {"product_id": 777, "quantity": 3}},
            {
                "api": "NotificationService.send_email",
                "params": {
                    "to": "warehouse@company.com",
                },
            },
        ],
        "expected_answer": {
            "available": True,
            "reserved": 3,
            "reservation_id": "rsv-001",
            "notification_sent": True,
        },
        "mock_responses": [
            {"product_id": 777, "available": 50, "warehouse": "WH-East"},
            {"reservation_id": "rsv-001", "expires_at": _future_mock_expiry_iso()},
            {"message_id": "msg-002", "sent": True},
        ],
        "tags": ["level-3", "multi-step", "inventory"],
    },
    {
        "case_id": "ab-l3-003",
        "level": 3,
        "description": "Analytics chain: user lookup, stats, order details",
        "user_query": (
            "Get spending analytics for user 42 over the last month, "
            "then find their largest order and show its details."
        ),
        "expected_api_chain": [
            {"api": "AnalyticsService.get_user_stats", "params": {"user_id": 42, "period": "month"}},
            {"api": "OrderService.get_orders", "params": {"user_id": 42}},
            {"api": "OrderService.get_order_details", "params": {"order_id": 3001}},
        ],
        "expected_answer": {
            "total_spent": 450.00,
            "largest_order_id": 3001,
            "largest_order_total": 189.99,
        },
        "mock_responses": [
            {"orders_count": 8, "total_spent": 450.00, "avg_order": 56.25},
            {
                "orders": [
                    {"order_id": 3001, "total": 189.99, "status": StatusEnum.COMPLETED.value},
                    {"order_id": 3002, "total": 45.00, "status": StatusEnum.COMPLETED.value},
                ],
            },
            {"order_id": 3001, "items": ["widget-a", "gadget-b"], "total": 189.99, "address": "123 Main St"},
        ],
        "tags": ["level-3", "multi-step", "analytics"],
    },
]


class APIBankAdapter(BenchmarkSuiteAdapter):
    """API-Bank adapter for multi-step tool calling evaluation."""

    name = "api_bank"
    layer = BenchmarkLayer.ORCHESTRATION
    tier = BenchmarkTier.MEDIUM

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
                description=item["description"],
                input_data={
                    "user_query": sanitize_untrusted_text(item["user_query"], max_length=4_000),
                    "level": item["level"],
                    "api_catalog": _API_CATALOG,
                    "mock_responses": item["mock_responses"],
                },
                expected={
                    "expected_api_chain": item["expected_api_chain"],
                    "expected_answer": item["expected_answer"],
                },
                tags=item.get("tags", []),
            )
            for item in items
        ]

    def run_case(self, case: BenchmarkCase, run_id: str) -> BenchmarkResult:
        """Run an API-Bank case.

        Args:
            case: The case.
            run_id: The run id.

        Returns:
            The BenchmarkResult result.
        """
        run_id = sanitize_untrusted_text(run_id, max_length=200)
        assert_closed_schema(case.input_data, allowed_keys={"user_query", "level", "api_catalog", "mock_responses"})
        start = time.monotonic()

        error: str | None = None
        try:
            result_data = self._run_via_orchestrator(case)
        except Exception as exc:
            logger.warning("API-Bank case %r could not execute orchestrator path: %s", case.case_id, exc)
            error = str(exc)
            result_data = {
                "error": error,
                "api_chain": [],
                "answer": {},
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
            tokens_consumed=count_tokens(str(case.input_data.get("user_query", ""))),
            output=result_data,
            error=error,
        )

    def evaluate(self, result: BenchmarkResult) -> float:
        """
        Score multi-step API calling accuracy.

        Scoring:
          - 0.35: API selection chain correctness (right APIs in right order)
          - 0.30: Parameter extraction accuracy
          - 0.20: Final answer correctness
          - 0.15: Chain completeness (all steps executed)

        Returns:
            float value produced by evaluate().

        Raises:
            TypeError: If the answer payload cannot be scored safely.
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

        case_id = require_nonempty(result.case_id, field_name="case_id")
        if expected is None:
            return 0.0

        score = 0.0
        expected_chain = expected["expected_api_chain"]
        actual_chain = result.output.get("api_chain", [])
        expected_answer = expected["expected_answer"]
        actual_answer = result.output.get("answer", {})
        if not isinstance(actual_answer, dict):
            raise TypeError(f"api_bank: unsupported answer type {type(actual_answer).__name__!r} for case {case_id!r}")

        # API selection chain (0.35)
        if expected_chain:
            correct_apis = 0
            for i, exp_call in enumerate(expected_chain):
                if i < len(actual_chain) and actual_chain[i].get("api") == exp_call["api"]:
                    correct_apis += 1
            api_score = correct_apis / len(expected_chain)
            score += 0.35 * api_score

        # Parameter accuracy (0.30)
        if expected_chain:
            param_score_sum = 0.0
            for i, exp_call in enumerate(expected_chain):
                if i < len(actual_chain):
                    exp_params = exp_call.get("params", {})
                    act_params = actual_chain[i].get("params", {})
                    if exp_params:
                        matching = sum(
                            1 for k, v in exp_params.items() if k in act_params and str(act_params[k]) == str(v)
                        )
                        param_score_sum += matching / len(exp_params)
            param_score = param_score_sum / len(expected_chain)
            score += 0.30 * param_score

        # Final answer correctness (0.20)
        if expected_answer:
            matching_fields = 0
            for key, exp_val in expected_answer.items():
                act_val = actual_answer.get(key)
                if act_val is not None:
                    if isinstance(act_val, bool) and not isinstance(exp_val, bool):
                        raise TypeError(
                            f"api_bank: unsupported answer type {type(act_val).__name__!r} for case {case_id!r}"
                        )
                    if isinstance(exp_val, bool):
                        if _bool_answer_matches(act_val, exp_val):
                            matching_fields += 1
                    elif isinstance(exp_val, (int, float)):
                        if abs(float(act_val) - float(exp_val)) < 0.02:
                            matching_fields += 1
                    elif str(act_val) == str(exp_val):
                        matching_fields += 1
            answer_score = matching_fields / len(expected_answer)
            score += 0.20 * answer_score

        # Chain completeness (0.15)
        if expected_chain:
            completion = min(len(actual_chain), len(expected_chain)) / len(expected_chain)
            score += 0.15 * completion

        return round(min(score, 1.0), 4)

    def _run_via_orchestrator(self, case: BenchmarkCase) -> dict[str, Any]:
        """Attempt execution via Vetinari orchestrator."""
        try:
            from vetinari.orchestration.two_layer import get_two_layer_orchestrator
        except ImportError as exc:
            raise RuntimeError("API-Bank live orchestrator path is unavailable") from exc

        orch = get_two_layer_orchestrator()
        result = orch.generate_and_execute(goal=case.input_data["user_query"])
        return {
            "api_chain": result.get("api_calls", []),
            "answer": result.get("final_output", {}),
        }


def _bool_answer_matches(actual: Any, expected: bool) -> bool:
    if isinstance(actual, bool):
        return actual is expected
    if isinstance(actual, str) and actual.strip().lower() in {"true", "false"}:
        return (actual.strip().lower() == "true") is expected
    return False
