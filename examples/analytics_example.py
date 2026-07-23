#!/usr/bin/env python3
"""
Example: Vetinari Analytics — Phase 5

Demonstrates all four analytics modules:
  1. Anomaly detection (Z-score, IQR, EWMA)
  2. Cost attribution per agent / task / model
  3. SLA / SLO tracking and compliance reporting
  4. Forecasting and capacity planning

Run from the project root:
    python.cmd examples/analytics_example.py
"""

import atexit
import math
import os
import tempfile
from pathlib import Path

_EXAMPLE_STATE = tempfile.TemporaryDirectory(prefix="vetinari-analytics-example-")
atexit.register(_EXAMPLE_STATE.cleanup)
_EXAMPLE_STATE_DIR = Path(_EXAMPLE_STATE.name)
_COST_ENTRIES_PATH = _EXAMPLE_STATE_DIR / "cost_entries.jsonl"
_COST_BUDGET_ALERTS_PATH = _EXAMPLE_STATE_DIR / "cost_budget_alerts.jsonl"
for _path in (
    _COST_ENTRIES_PATH,
    _EXAMPLE_STATE_DIR / "cost_entries.jsonl.1",
    _EXAMPLE_STATE_DIR / "cost_entries.jsonl.2",
    _EXAMPLE_STATE_DIR / "cost_entries.jsonl.3",
    _COST_BUDGET_ALERTS_PATH,
):
    _path.touch()
os.environ["VETINARI_USER_DIR"] = str(_EXAMPLE_STATE_DIR / "user")
os.environ["VETINARI_COST_ENTRIES_PATH"] = str(_COST_ENTRIES_PATH)
os.environ["VETINARI_COST_BUDGET_ALERTS_PATH"] = str(_COST_BUDGET_ALERTS_PATH)

from vetinari.analytics import (
    AnomalyConfig, get_anomaly_detector, reset_anomaly_detector,
    CostEntry, ModelPricing, get_cost_tracker, reset_cost_tracker,
    SLOTarget, SLOType, get_sla_tracker, reset_sla_tracker,
    ForecastRequest, get_forecaster, reset_forecaster,
)


def section(title: str) -> None:
    print(f"\n{'='*60}\n  {title}\n{'='*60}")


def kv(key: str, value) -> None:
    print(f"  {key:<38} {value}")


# ── reset all singletons to start clean ───────────────────────────────────
reset_anomaly_detector()
reset_cost_tracker()
reset_sla_tracker()
reset_forecaster()


# ─────────────────────────────────────────────────────────────────────────
# 1. Anomaly Detection
# ─────────────────────────────────────────────────────────────────────────
section("1. Anomaly Detection")

detector = get_anomaly_detector()
detector.configure(AnomalyConfig(
    window_size=30, z_threshold=2.5, iqr_factor=1.5,
    ewma_alpha=0.2, ewma_threshold=3.0, min_samples=8,
))

# Build a realistic latency baseline with slight variation
baseline = [100.0 + 5.0 * math.sin(i * 0.5) for i in range(25)]
for v in baseline:
    detector.detect("adapter.latency", v)

# Inject a normal observation
r_normal = detector.detect("adapter.latency", 103.0)
kv("Normal value (103 ms) flagged?", r_normal.is_anomaly)

# Inject a spike
r_spike = detector.detect("adapter.latency", 850.0)
kv("Spike value (850 ms) flagged?",   r_spike.is_anomaly)
kv("Detection method:",               r_spike.method)
kv("Anomaly score:",                  f"{r_spike.score:.2f}")
kv("Reason:",                         r_spike.reason)

stats = detector.get_stats()
kv("Tracked metrics:",   stats["tracked_metrics"])
kv("Total anomalies:",   stats["total_anomalies"])


# ─────────────────────────────────────────────────────────────────────────
# 2. Cost Attribution
# ─────────────────────────────────────────────────────────────────────────
section("2. Cost Attribution")

tracker = get_cost_tracker()
tracker.set_pricing("openai",   "gpt-4",   ModelPricing(input_per_1k=0.030, output_per_1k=0.060))
tracker.set_pricing("openai",   "gpt-4o",  ModelPricing(input_per_1k=0.005, output_per_1k=0.015))
tracker.set_pricing("lmstudio", "llama-3", ModelPricing())   # free

calls = [
    CostEntry(provider="openai",   model="gpt-4",   input_tokens=500,  output_tokens=200, agent="builder",  task_id="t1"),
    CostEntry(provider="openai",   model="gpt-4",   input_tokens=300,  output_tokens=100, agent="evaluator",task_id="t1"),
    CostEntry(provider="openai",   model="gpt-4o",  input_tokens=800,  output_tokens=400, agent="builder",  task_id="t2"),
    CostEntry(provider="openai",   model="gpt-4o",  input_tokens=200,  output_tokens=80,  agent="explorer", task_id="t2"),
    CostEntry(provider="lmstudio", model="llama-3", input_tokens=1200, output_tokens=600, agent="synthesizer", task_id="t3"),
]
for c in calls:
    tracker.record(c)

report = tracker.get_report()
kv("Total cost (USD):",     f"${report.total_cost_usd:.6f}")
kv("Total tokens:",         report.total_tokens)
kv("Total requests:",       report.total_requests)

print("\n  Cost by agent:")
for agent, cost in sorted(report.by_agent.items(), key=lambda x: -x[1]):
    print(f"    {agent:<20}  ${cost:.6f}")

print("\n  Top models by cost:")
for item in tracker.get_top_models(n=3):
    print(f"    {item['model']:<30}  ${item['cost_usd']:.6f}")

# Filter to task t1
t1_report = tracker.get_report(task_id="t1")
kv("Task t1 cost:",         f"${t1_report.total_cost_usd:.6f}")


# ─────────────────────────────────────────────────────────────────────────
# 3. SLA / SLO Tracking
# ─────────────────────────────────────────────────────────────────────────
section("3. SLA / SLO Tracking")

sla = get_sla_tracker()

sla.register_slo(SLOTarget(
    name="p95-latency",
    slo_type=SLOType.LATENCY_P95,
    budget=500.0,
    window_seconds=3600,
    description="95th-percentile adapter latency must be under 500 ms",
))
sla.register_slo(SLOTarget(
    name="success-rate",
    slo_type=SLOType.SUCCESS_RATE,
    budget=99.0,
    window_seconds=3600,
    description="Adapter success rate must be >= 99%",
))
sla.register_slo(SLOTarget(
    name="plan-approval",
    slo_type=SLOType.APPROVAL_RATE,
    budget=80.0,
    window_seconds=3600,
    description="Plan gate approval rate must be >= 80%",
))

# Feed latency observations
for latency in baseline + [300.0, 450.0, 380.0, 290.0]:
    sla.record_latency("adapter", latency)

# Feed request results (3 failures out of 100)
for i in range(100):
    sla.record_request(success=(i % 34 != 0))

# Feed plan decisions
for i in range(10):
    sla.record_plan_decision(approved=(i != 8))   # 9 of 10 approved

for report in sla.get_all_reports():
    kv(f"SLO '{report.slo.name}':",
       f"p={report.compliance_pct:.1f}%  "
       f"current={report.current_value:.1f}  "
       f"{'OK' if report.is_compliant else 'BREACH'}")

kv("Registered SLOs:", sla.get_stats()["registered_slos"])


# ─────────────────────────────────────────────────────────────────────────
# 4. Forecasting & Capacity Planning
# ─────────────────────────────────────────────────────────────────────────
section("4. Forecasting & Capacity Planning")

fc = get_forecaster()

# Feed a gradually rising latency series
rising = [50.0 + i * 3.5 for i in range(40)]
for v in rising:
    fc.ingest("adapter.latency", v)

print("\n  Method comparison (horizon=5):")
for method in ("sma", "exp_smoothing", "linear_trend", "seasonal"):
    r = fc.forecast(ForecastRequest(
        metric="adapter.latency", horizon=5, method=method, period=7))
    preds_str = "  ".join(f"{p:.1f}" for p in r.predictions)
    print(f"    {method:<16}  RMSE={r.rmse:.2f}  preds=[{preds_str}]")

# Capacity planning
will_breach = fc.will_exceed("adapter.latency", threshold=250.0, horizon=20)
kv("Will breach 250 ms within 20 steps?", will_breach)

steps = fc.steps_until_threshold("adapter.latency", threshold=250.0, horizon=50)
kv("Steps until 250 ms threshold:", steps)

stats = fc.get_stats()
kv("Tracked metrics:", stats["tracked_metrics"])

section("All Phase 5 analytics exercised successfully.")
