"""
Tau-bench Reliability Adapter
==============================

Layer 3 (Pipeline) benchmark: consistency and reliability testing.

Tau-bench measures pass^k — the probability that a system produces a
correct result on ALL of k independent trials. This tests reliability
rather than peak performance.

Metrics: pass^k (k=1..5), consistency score, variance across trials.
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
from vetinari.boundary_guards import require_nonempty
from vetinari.context import count_tokens

logger = logging.getLogger(__name__)


# -- Sample Tau-bench-style cases --

_SAMPLE_CASES: list[dict[str, Any]] = [
    {
        "task_id": "tau-retail-001",
        "domain": "retail",
        "description": "Process a customer return with restocking fee calculation",
        "user_instruction": (
            "Customer wants to return a $149.99 laptop bag purchased 25 days ago. "
            "Store policy: full refund within 14 days, 15% restocking fee within 30 days, "
            "no returns after 30 days."
        ),
        "expected_actions": ["calculate_restocking_fee", "process_refund"],
        "expected_output": {
            "refund_amount": 127.49,
            "restocking_fee": 22.50,
            "policy_applied": "15% restocking fee (15-30 day window)",
        },
        "tools_available": [
            "lookup_order",
            "calculate_restocking_fee",
            "process_refund",
            "send_confirmation",
        ],
    },
    {
        "task_id": "tau-retail-002",
        "domain": "retail",
        "description": "Apply stacking discount coupons correctly",
        "user_instruction": (
            "Customer has two coupons: 20% off (SAVE20) and $10 off orders over $50 "
            "(TENOFF). Cart total is $89.99. Store policy: percentage discounts apply "
            "first, then fixed-amount coupons."
        ),
        "expected_actions": ["apply_percent_discount", "apply_fixed_discount"],
        "expected_output": {
            "original_total": 89.99,
            "after_percent": 71.99,
            "after_fixed": 61.99,
            "final_total": 61.99,
            "discounts_applied": ["SAVE20 (20%)", "TENOFF ($10)"],
        },
        "tools_available": [
            "get_cart",
            "validate_coupon",
            "apply_percent_discount",
            "apply_fixed_discount",
            "update_total",
        ],
    },
    {
        "task_id": "tau-airline-001",
        "domain": "airline",
        "description": "Rebook passenger after missed connection",
        "user_instruction": (
            "Passenger John Smith missed connecting flight AA234 at DFW due to "
            "incoming flight AA567 arriving 2 hours late. Find next available "
            "connection to LAX. Priority: same airline, then codeshare, then any."
        ),
        "expected_actions": [
            "lookup_passenger",
            "check_flight_status",
            "search_alternatives",
            "rebook_passenger",
        ],
        "expected_output": {
            "original_flight": "AA234",
            "delay_cause": "incoming flight AA567 delayed",
            "rebooked_to": "AA890",
            "passenger": "John Smith",
            "compensation": "meal voucher",
        },
        "tools_available": [
            "lookup_passenger",
            "check_flight_status",
            "search_alternatives",
            "rebook_passenger",
            "issue_compensation",
            "send_notification",
        ],
    },
    {
        "task_id": "tau-airline-002",
        "domain": "airline",
        "description": "Upgrade passenger using miles with fare difference",
        "user_instruction": (
            "Passenger wants to upgrade from Economy to Business on flight UA100 "
            "JFK-LHR. They have 45,000 miles. Upgrade costs 35,000 miles plus $250 "
            "fare difference. Verify sufficient miles and process upgrade."
        ),
        "expected_actions": [
            "check_miles_balance",
            "calculate_upgrade_cost",
            "process_upgrade",
        ],
        "expected_output": {
            "miles_required": 35000,
            "miles_available": 45000,
            "fare_difference": 250.00,
            "upgrade_approved": True,
            "remaining_miles": 10000,
        },
        "tools_available": [
            "check_miles_balance",
            "check_seat_availability",
            "calculate_upgrade_cost",
            "process_upgrade",
            "charge_fare_difference",
            "send_confirmation",
        ],
    },
    {
        "task_id": "tau-finance-001",
        "domain": "finance",
        "description": "Detect and flag suspicious wire transfer",
        "user_instruction": (
            "Customer requests $9,500 wire transfer to a new beneficiary in a "
            "high-risk jurisdiction (Country X). Account was opened 30 days ago. "
            "Apply AML screening rules: flag if new account + high-risk country + "
            "amount over $5,000."
        ),
        "expected_actions": [
            "verify_beneficiary",
            "check_jurisdiction_risk",
            "apply_aml_rules",
            "flag_for_review",
        ],
        "expected_output": {
            "transfer_amount": 9500,
            "risk_flags": ["new_account", "high_risk_jurisdiction", "large_amount"],
            "decision": "flagged_for_manual_review",
            "aml_rule_matched": "NEW_ACCT_HIGH_RISK_LARGE",
        },
        "tools_available": [
            "verify_beneficiary",
            "check_jurisdiction_risk",
            "check_account_age",
            "apply_aml_rules",
            "flag_for_review",
            "process_transfer",
        ],
    },
]


class TauBenchAdapter(BenchmarkSuiteAdapter):
    """Tau-bench adapter for reliability/consistency evaluation."""

    name = "tau_bench"
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
                case_id=item["task_id"],
                suite_name=self.name,
                description=item["description"],
                input_data={
                    "domain": item["domain"],
                    "user_instruction": item["user_instruction"],
                    "tools_available": item["tools_available"],
                },
                expected={
                    "expected_actions": item["expected_actions"],
                    "expected_output": item["expected_output"],
                },
                tags=[item["domain"]],
            )
            for item in items
        ]

    def run_case(self, case: BenchmarkCase, run_id: str) -> BenchmarkResult:
        """
        Run a Tau-bench case.

        Mock mode simulates tool-calling sequences. Production mode
        would invoke the full Vetinari orchestrator.

        Args:
            case: The case.
            run_id: The run id.

        Returns:
            The BenchmarkResult result.
        """
        start = time.monotonic()

        error: str | None = None
        try:
            result_data = self._run_via_orchestrator(case)
        except Exception as exc:
            logger.warning("Tau-bench case %r could not execute orchestrator path: %s", case.case_id, exc)
            error = str(exc)
            result_data = {
                "error": error,
                "actions_taken": [],
                "output": {},
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
            tokens_consumed=count_tokens(str(case.input_data.get("user_instruction", ""))),
            output=result_data,
            error=error,
        )

    def evaluate(self, result: BenchmarkResult) -> float:
        """
        Score based on action sequence correctness and output accuracy.

        Scoring:
          - 0.4 weight: correct actions taken (order-independent)
          - 0.4 weight: output field accuracy
          - 0.2 weight: no extraneous/harmful actions

        Returns:
            float value produced by evaluate().

        Raises:
            TypeError: If a typed output field cannot be scored safely.
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

        require_nonempty(result.case_id, field_name="case_id")
        if expected is None:
            return 0.0

        score = 0.0

        # Action correctness (0.4)
        expected_actions = set(expected["expected_actions"])
        actual_actions = set(result.output.get("actions_taken", []))
        if expected_actions:
            action_overlap = len(expected_actions & actual_actions)
            action_score = action_overlap / len(expected_actions)
            score += 0.4 * action_score

        expected_out = expected["expected_output"]
        actual_out = result.output.get("output", {})
        if not isinstance(actual_out, dict):
            return 0.0

        # Output field accuracy (0.4)
        if expected_out:
            matching_fields = 0
            for key, expected_val in expected_out.items():
                field = require_nonempty(str(key), field_name="field")
                actual_val = actual_out.get(key)
                if actual_val is not None:
                    if isinstance(actual_val, bool):
                        raise TypeError(
                            f"tau_bench: unsupported output type {type(actual_val).__name__!r} for field {field!r}"
                        )
                    if isinstance(expected_val, (int, float)):
                        if abs(float(actual_val) - float(expected_val)) < 0.02:
                            matching_fields += 1
                    elif str(actual_val) == str(expected_val):
                        matching_fields += 1
            field_score = matching_fields / len(expected_out)
            score += 0.4 * field_score

        # No harmful actions (0.2): penalise if wrong tools were called
        harmful_actions = actual_actions - expected_actions - set(expected.get("tools_available", []))
        if not harmful_actions:
            score += 0.2

        return round(min(score, 1.0), 4)

    def _run_via_orchestrator(self, case: BenchmarkCase) -> dict[str, Any]:
        """Attempt execution via Vetinari pipeline."""
        from vetinari.orchestration.two_layer import get_two_layer_orchestrator

        orch = get_two_layer_orchestrator()
        result = orch.generate_and_execute(goal=case.input_data["user_instruction"])
        return {
            "actions_taken": result.get("tools_called", []),
            "output": result.get("final_output", {}),
        }
