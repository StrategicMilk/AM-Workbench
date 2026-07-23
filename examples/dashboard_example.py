#!/usr/bin/env python3
"""
Example: Vetinari Dashboard — Python API Usage  (Phase 4)

Demonstrates:
  1. Seeding telemetry data so the dashboard has something to show
  2. Reading metrics via DashboardAPI
  3. Registering alert thresholds and evaluating them
  4. Ingesting log records and searching the aggregator buffer
  5. Adding traces and querying them

Run from the project root:
    python.cmd examples/dashboard_example.py
"""

import logging

from vetinari.telemetry import get_telemetry_collector
from vetinari.dashboard.api import get_dashboard_api, TraceDetail
from vetinari.dashboard.alerts import (
    get_alert_engine,
    AlertThreshold,
    AlertCondition,
    AlertSeverity,
)
from vetinari.dashboard.log_aggregator import (
    get_log_aggregator,
    LogRecord,
    AggregatorHandler,
)

logging.basicConfig(level=logging.WARNING)   # keep output tidy


# ─── helpers ──────────────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def kv(key: str, value) -> None:
    print(f"  {key:<35} {value}")


# ─── 1. Seed telemetry ─────────────────────────────────────────────────────

section("1. Seeding telemetry data")

telemetry = get_telemetry_collector()

# Simulate several adapter calls
for i in range(20):
    latency = 100.0 + (i * 15)
    success = (i % 5 != 0)
    tokens  = 200 + i * 10 if success else 0
    telemetry.record_adapter_latency("openai", "gpt-4", latency,
                                     success=success, tokens_used=tokens)

for i in range(10):
    telemetry.record_adapter_latency("lmstudio", "llama-3", 50.0 + i * 5,
                                     success=True, tokens_used=150)

# Memory operations
for i in range(15):
    telemetry.record_memory_write("oc",    latency_ms=3.0 + i * 0.2)
    telemetry.record_memory_read("oc",     latency_ms=1.5)
    telemetry.record_memory_search("oc",   latency_ms=5.0)

# Plan decisions
for i in range(8):
    telemetry.record_plan_decision("approve", risk_score=0.1 + i * 0.05,
                                   approval_time_ms=80.0)
telemetry.record_plan_decision("reject",  risk_score=0.85, approval_time_ms=120.0)
telemetry.record_plan_decision("approve", risk_score=0.2,  approval_time_ms=50.0,
                               auto_approved=True)

print("  Telemetry seeded.")


# ─── 2. DashboardAPI ──────────────────────────────────────────────────────

section("2. DashboardAPI — latest metrics")

api = get_dashboard_api()
snap = api.get_latest_metrics()

a = snap.adapter_summary
p = snap.plan_summary

kv("Total requests:",        a.get("total_requests", 0))
kv("Total successful:",      a.get("total_successful", 0))
kv("Total failed:",          a.get("total_failed", 0))
kv("Average latency (ms):",  f"{a.get('average_latency_ms', 0):.1f}")
kv("Total tokens used:",     a.get("total_tokens_used", 0))
kv("Plan approval rate (%):", f"{p.get('approval_rate', 0):.1f}")
kv("Avg risk score:",        f"{p.get('average_risk_score', 0):.3f}")

section("2b. DashboardAPI — time-series (latency)")

ts = api.get_timeseries_data("latency")
if ts and ts.points:
    kv("Points returned:",   len(ts.points))
    kv("Min latency (ms):",  f"{ts.min_value:.1f}")
    kv("Max latency (ms):",  f"{ts.max_value:.1f}")
    kv("Avg latency (ms):",  f"{ts.avg_value:.1f}")
else:
    print("  (no latency data yet)")


# ─── 3. Traces ────────────────────────────────────────────────────────────

section("3. DashboardAPI — traces")

# Add a few synthetic traces
for i in range(5):
    trace = TraceDetail(
        trace_id=f"example-trace-{i:03d}",
        start_time="2026-03-03T21:00:00+00:00",
        end_time="2026-03-03T21:00:00.500+00:00",
        duration_ms=200.0 + i * 30,
        status="success" if i % 4 != 0 else "error",
        spans=[
            {"span_id": f"s{i}-0", "operation": "plan_eval",  "duration_ms": 100.0},
            {"span_id": f"s{i}-1", "operation": "model_call", "duration_ms": 100.0 + i * 30},
        ],
    )
    api.add_trace(trace)

traces = api.search_traces(limit=10)
kv("Traces stored:", len(traces))
for t in traces[:3]:
    print(f"    {t.trace_id}  status={t.status}  {t.duration_ms:.0f} ms")

detail = api.get_trace_detail("example-trace-002")
if detail:
    kv("Trace detail spans:", len(detail.spans))


# ─── 4. Alert Engine ──────────────────────────────────────────────────────

section("4. AlertEngine — threshold evaluation")

engine = get_alert_engine()
engine.clear_thresholds()

engine.register_threshold(AlertThreshold(
    name="high-latency",
    metric_key="adapters.average_latency_ms",
    condition=AlertCondition.GREATER_THAN,
    threshold_value=50.0,        # almost certainly exceeded with seeded data
    severity=AlertSeverity.HIGH,
    channels=["log"],
))

engine.register_threshold(AlertThreshold(
    name="low-approval-rate",
    metric_key="plan.approval_rate",
    condition=AlertCondition.LESS_THAN,
    threshold_value=10.0,        # very low threshold — should NOT fire
    severity=AlertSeverity.MEDIUM,
    channels=["log"],
))

engine.register_threshold(AlertThreshold(
    name="high-risk",
    metric_key="plan.average_risk_score",
    condition=AlertCondition.GREATER_THAN,
    threshold_value=0.01,        # should fire
    severity=AlertSeverity.LOW,
    channels=["log"],
))

fired = engine.evaluate_all()
kv("Thresholds registered:", len(engine.list_thresholds()))
kv("Alerts fired this cycle:", len(fired))
for a in fired:
    print(f"    [{a.threshold.severity.value.upper()}] {a.threshold.name}"
          f"  value={a.current_value:.3g}")

stats = engine.get_stats()
kv("Active alerts:", stats["active_alerts"])
kv("History total:", stats["total_fired"])


# ─── 5. Log Aggregator ────────────────────────────────────────────────────

section("5. LogAggregator — ingest and search")

agg = get_log_aggregator()

# Ingest structured records
trace_id = "example-trace-001"
records = [
    LogRecord(message="Plan evaluation started", level="INFO",
              trace_id=trace_id, span_id="s1-0",
              logger_name="vetinari.planner",
              extra={"plan_id": "plan_007"}),
    LogRecord(message="Risk score computed",     level="INFO",
              trace_id=trace_id, span_id="s1-0",
              logger_name="vetinari.planner",
              extra={"risk_score": 0.23}),
    LogRecord(message="Model call succeeded",    level="INFO",
              trace_id=trace_id, span_id="s1-1",
              logger_name="vetinari.adapter",
              extra={"latency_ms": 145.2, "tokens": 320}),
    LogRecord(message="Timeout on retry",        level="WARNING",
              trace_id="other-trace-999",        span_id="s0",
              logger_name="vetinari.adapter"),
]
agg.ingest_many(records)

# Search by trace
trace_records = agg.get_trace_records(trace_id)
kv("Records for trace-001:", len(trace_records))

# Span correlation
span_records = agg.correlate_span(trace_id, "s1-0")
kv("Records for span s1-0:", len(span_records))

# Full search
warnings = agg.search(level="WARNING")
kv("WARNING-level records:", len(warnings))

buffer_stats = agg.get_stats()
kv("Buffer size:", buffer_stats["buffer_size"])

# Attach stdlib logging bridge
handler = AggregatorHandler()
stdlib_logger = logging.getLogger("dashboard_example")
stdlib_logger.addHandler(handler)
stdlib_logger.setLevel(logging.DEBUG)
stdlib_logger.info("AggregatorHandler bridge active")
stdlib_logger.removeHandler(handler)

after_bridge = agg.search(message_contains="AggregatorHandler bridge")
kv("Bridge-captured records:", len(after_bridge))


# ─── Summary ──────────────────────────────────────────────────────────────

section("Summary")
print("  All dashboard components exercised successfully.")
print("  Start the REST API to access via browser:")
print("    .venv312/Scripts/python.exe -m vetinari.dashboard.api")
print("    http://localhost:5000/dashboard")
