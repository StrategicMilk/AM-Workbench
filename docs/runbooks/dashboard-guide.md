# AM Workbench Monitoring Dashboard — User Guide

---

## Table of Contents

1. [Overview](#overview)
2. [Quick Start](#quick-start)
3. [Starting the Server](#starting-the-server)
4. [Runtime Status And Mounted Surfaces](#runtime-status-and-mounted-surfaces)
5. [Alert System](#alert-system)
6. [Log Aggregation](#log-aggregation)
7. [REST API Usage](#rest-api-usage)
8. [Configuration Reference](#configuration-reference)
9. [Troubleshooting](#troubleshooting)

---

## Overview

The AM Workbench Monitoring Dashboard gives you real-time visibility into your
multi-agent orchestration system. It surfaces the telemetry data collected by
Phase 3 (adapters, memory, plan-gate) through a browser-based UI, a REST API,
a threshold-based alert engine, and a pluggable log-aggregation layer.

**Components**

| Component | Module | Purpose |
|---|---|---|
| Dashboard API | `vetinari.dashboard.api` | In-process metrics & trace query engine |
| Web API | `crates/amw-kernel/src/api/routes` | Native Rust kernel route surface for migrated Workbench API domains; the browser `/dashboard` UI is not mounted |
| Alert Engine | `vetinari.dashboard.alerts` | Threshold evaluation & dispatch |
| Log Aggregator | `vetinari.dashboard.log_aggregator` | Structured-log fan-out (file / ES / Splunk / Datadog) |
| Dashboard UI | `ui/templates/dashboard.html` | Legacy dormant source retained outside the mounted native route table |

---

## Quick Start

```bash
# 1. Install dependencies
pip install -e ".[dev]"

# 2. Launch the server
python -m vetinari serve --port 5000

# 3. Query the mounted API routes; no browser dashboard is mounted at /dashboard
#    curl http://localhost:5000/api/v1/health
```

---

## Starting the Server

### CLI (recommended)

```bash
python -m vetinari serve --port 5000              # default: 127.0.0.1:5000
python -m vetinari start                          # start the local app/API
python -m vetinari start --goal "..."             # start with goal
```

### Programmatic

```bash
cargo run -p amw-kernel --bin amw-kernel-server -- --host 127.0.0.1 --port 5000
```

The native Rust kernel server registers the migrated Workbench API-domain routes.
Legacy static dashboard files are retained as dormant source and are not mounted
by the current API host.

---

## Runtime Status And Mounted Surfaces

The current native API host does not mount the legacy `/dashboard` browser route
from `ui/templates/dashboard.html`. Treat that template as dormant source. Do
not tell operators to use its sidebar, auto-refresh controls, trace modal, or
client-side alert history unless a route-mounting change restores it and tests
prove the route is live.

Use the mounted JSON endpoints and Workbench app views instead:

| Surface | Current status | Operator action |
|---|---|---|
| `/dashboard` | Not mounted in the current native API server. | A 404 is expected; use API endpoints below. |
| `/api/v1/dashboard` | Mounted dashboard aggregate API. | Query for current agent and system summary. |
| `/api/v1/dashboard/health` | Mounted dashboard health API. | Query for dashboard-specific health. |
| `/api/v1/metrics/latest` | Mounted metrics snapshot API. | Query for latest adapter, memory, and plan metrics. |
| `/api/v1/metrics/timeseries` | Mounted time-series API. | Query by metric name and timerange. |
| `/api/v1/traces` | Mounted trace search API. | Query by trace id or list recent traces. |

If a browser view needs these values, verify the Svelte route that consumes the
endpoint. This runbook covers the API/helper-level telemetry surface, not the
legacy HTML template.

---

## Alert System

### Concepts

| Term | Description |
|---|---|
| `AlertThreshold` | A named rule: metric key + condition + value + severity + channels |
| `AlertEngine` | Evaluates all thresholds against the live snapshot; fires alerts |
| `AlertRecord` | An alert instance that was fired (metric, value, time) |
| Dispatcher | Function that routes a fired alert to a channel (log / email / webhook) |

### Supported metric keys (dot-notation into `MetricsSnapshot.to_dict()`)

```
adapters.total_requests
adapters.total_failed
adapters.average_latency_ms
adapters.total_tokens_used
plan.total_decisions
plan.approval_rate
plan.average_risk_score
plan.average_approval_time_ms
```

### Basic usage

```python
from vetinari.dashboard.alerts import (
    get_alert_engine,
    AlertThreshold,
    AlertCondition,
    AlertSeverity,
)

engine = get_alert_engine()

# Fire if average adapter latency exceeds 500 ms
engine.register_threshold(
    AlertThreshold(
        name="high-latency",
        metric_key="adapters.average_latency_ms",
        condition=AlertCondition.GREATER_THAN,
        threshold_value=500.0,
        severity=AlertSeverity.HIGH,
        channels=["log"],
    )
)

# Fire if plan approval rate drops below 70 %
engine.register_threshold(
    AlertThreshold(
        name="low-approval-rate",
        metric_key="plan.approval_rate",
        condition=AlertCondition.LESS_THAN,
        threshold_value=70.0,
        severity=AlertSeverity.MEDIUM,
        channels=["log", "webhook"],
    )
)

# Evaluate (call this on a schedule, e.g. every 30 s)
fired = engine.evaluate_all()
for alert in fired:
    print(f"Alert fired: {alert.threshold.name} = {alert.current_value}")
```

### Duration-based alerts

Set `duration_seconds > 0` to require the condition to hold continuously before
firing. This prevents alert storms from transient spikes:

```python
AlertThreshold(
    name="sustained-latency",
    metric_key="adapters.average_latency_ms",
    condition=AlertCondition.GREATER_THAN,
    threshold_value=300.0,
    duration_seconds=60,  # only fires after 60 s above threshold
    severity=AlertSeverity.MEDIUM,
    channels=["log"],
)
```

### Suppression & re-fire

Once an alert fires it is suppressed until the condition clears. When the metric
returns within bounds the alert is removed from `get_active_alerts()`. If the
condition re-triggers, it fires again.

### Custom channels

Add your own dispatcher to `DISPATCHERS`:

```python
from vetinari.dashboard.alerts import DISPATCHERS, AlertRecord


def my_slack_dispatcher(alert: AlertRecord) -> None:
    import requests

    requests.post(
        "https://hooks.slack.com/services/...",
        json={"text": f":warning: {alert.threshold.name}: {alert.current_value}"},
    )


DISPATCHERS["slack"] = my_slack_dispatcher

# Then reference "slack" in any AlertThreshold.channels list
```

---

## Log Aggregation

### In-process buffer & search

Records ingested through `LogAggregator` are held in a circular buffer
(default 5 000 entries) and can be searched without any external service:

```python
from vetinari.dashboard.log_aggregator import get_log_aggregator, LogRecord

agg = get_log_aggregator()

agg.ingest(
    LogRecord(
        message="Plan approved",
        level="INFO",
        trace_id="abc-123",
        span_id="span-001",
        extra={"plan_id": "plan_007", "risk_score": 0.12},
    )
)

# Search by trace
records = agg.get_trace_records("abc-123")

# Cross-filter search
records = agg.search(
    level="ERROR",
    message_contains="timeout",
    since=time.time() - 3600,  # last hour
    limit=50,
)
```

### Configuring a backend

```python
# File — no external dependencies
agg.configure_backend("file", path="logs/vetinari_audit.jsonl")

# Elasticsearch
agg.configure_backend(
    "elasticsearch",
    url="http://localhost:9200",
    index="vetinari-logs",
    api_key="my_api_key",  # optional
)

# Splunk HEC
agg.configure_backend(
    "splunk",
    url="http://splunk-hec:8088",
    token="your-hec-token",
    source="vetinari",
    sourcetype="_json",
)

# Datadog
agg.configure_backend(
    "datadog",
    api_key="your-dd-api-key",
    service="vetinari",
    ddsource="python",
    ddtags="env:prod,team:ml",
)
```

Multiple backends can be active simultaneously; records are fanned out to all
of them on each `flush()`.

### Attaching to Python logging

`AggregatorHandler` bridges stdlib `logging` into the aggregator automatically:

```python
import logging
from vetinari.dashboard.log_aggregator import AggregatorHandler

logging.getLogger().addHandler(AggregatorHandler())
# All log output from this point is captured in the aggregator buffer
```

### Batch flushing

Records are queued and flushed automatically when the batch reaches
`_batch_size` (default 100). Call `agg.flush()` to force an immediate send —
useful at application shutdown:

```python
import atexit

atexit.register(get_log_aggregator().flush)
```

---

## REST API Usage

Base URL: `http://localhost:5000`

| Method | Endpoint | Description |
|---|---|---|
| GET | `/dashboard` | Not mounted in the current native API server |
| GET | `/api/v1/health` | Health check |
| GET | `/api/v1/stats` | Dashboard statistics |
| GET | `/api/v1/metrics/latest` | Latest MetricsSnapshot |
| GET | `/api/v1/metrics/timeseries` | Time-series data |
| GET | `/api/v1/traces` | List / search traces |
| GET | `/api/v1/traces/<trace_id>` | Trace detail with spans |

### Query parameters — `/api/v1/metrics/timeseries`

| Parameter | Values | Default |
|---|---|---|
| `metric` | `latency`, `success_rate`, `token_usage`, `memory_latency` | `latency` |
| `timerange` | `1h`, `24h`, `7d` | `24h` |
| `provider` | any provider key | (all) |

### Query parameters — `/api/v1/traces`

| Parameter | Values | Default |
|---|---|---|
| `trace_id` | any string | (all) |
| `limit` | 1–1000 | `100` |

---

## Configuration Reference

### AlertEngine

```python
engine = get_alert_engine()
engine.register_threshold(AlertThreshold(...))  # add / replace by name
engine.unregister_threshold("name")  # remove
engine.clear_thresholds()  # remove all
engine.list_thresholds()  # List[AlertThreshold]
engine.get_active_alerts()  # List[AlertRecord] — currently firing
engine.get_history()  # List[AlertRecord] — all that ever fired
engine.get_stats()  # dict
```

### LogAggregator

```python
agg = get_log_aggregator()
agg.configure_backend(name, **kwargs)  # add backend
agg.remove_backend(name)  # remove backend
agg.list_backends()  # List[str]
agg.ingest(record)  # single record
agg.ingest_many(records)  # batch
agg.flush()  # force dispatch
agg.search(...)  # in-process filter
agg.get_trace_records(trace_id)  # ordered by timestamp
agg.correlate_span(trace_id, span_id)  # narrow to one span
agg.get_stats()  # dict
agg.clear_buffer()  # discard in-memory records
```

---

## Troubleshooting

### Dashboard page returns 404

This is expected in the current native API host: `/dashboard` is not mounted.
Use the dashboard API routes for telemetry, or restore a browser UI only through
an explicit route-mounting change owned by the frontend/runtime pack.

### Charts show no data

The time-series endpoints derive data from the `TelemetryCollector` singleton.
If no adapter calls have been made yet, the charts will be empty. Generate some
traffic via `telemetry.record_adapter_latency(...)` or by running actual tasks.

### Alerts never fire

1. Verify `metric_key` matches the exact dot-notation path shown in the
   [Alert System](#alert-system) section.
2. Call `engine.evaluate_all()` explicitly — the engine does **not** poll
   automatically. Wrap it in a background thread or scheduled job.
3. Check `engine.get_stats()` to confirm the threshold is registered.

### File backend writes 0 records

Call `agg.flush()` after ingestion. Records are batched; without a flush call
they remain in `_pending` until the batch fills to `_batch_size` (default 100).

### Elasticsearch returns 4xx

- Confirm the index exists or that `auto_create_index` is enabled.
- Check the `api_key` is base64-encoded as `id:api_key` (standard ES format).
- Inspect the raw error with `logger.setLevel(logging.DEBUG)`.

### `requests` not installed

The Elasticsearch, Splunk, and Datadog backends require `requests`, which is installed through the project metadata:

```bash
python -m pip install -e ".[dev]"
```

The file backend and in-process search work without it.
