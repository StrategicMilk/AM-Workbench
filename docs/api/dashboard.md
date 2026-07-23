# AM Workbench Dashboard â€” API Reference

# AM Workbench Dashboard - API Reference

**Phase 4 | Last Updated: March 2026**

---

## Table of Contents

1. [Python API](#python-api)
   - [DashboardAPI](#dashboardapi)
   - [AlertEngine](#alertengine)
   - [LogAggregator](#logaggregator)
2. [REST API](#rest-api)
   - Current Rust kernel route boundary and retired Python route note
3. [Data Schemas](#data-schemas)

---

## Python API

### DashboardAPI

```python
from vetinari.dashboard.api import get_dashboard_api, reset_dashboard
```

**Singleton access**

| Function | Returns | Description |
|---|---|---|
| `get_dashboard_api()` | `DashboardAPI` | Return (or create) the global singleton |
| `reset_dashboard()` | `None` | Destroy the singleton (mainly for testing) |

---

#### `DashboardAPI.get_latest_metrics() â†’ MetricsSnapshot`

Return the current snapshot of all telemetry metrics.

**Returns** â€” `MetricsSnapshot`

| Field | Type | Description |
|---|---|---|
| `timestamp` | `str` | ISO-8601 UTC timestamp |
| `uptime_ms` | `float` | Milliseconds since API initialisation |
| `adapter_summary` | `dict` | Aggregated adapter metrics (see below) |
| `memory_summary` | `dict` | Per-backend memory metrics |
| `plan_summary` | `dict` | Plan-gate decision metrics |

`adapter_summary` keys:

```
total_providers       int
total_requests        int
total_successful      int
total_failed          int
average_latency_ms    float
total_tokens_used     int
providers             dict[str, ProviderDetail]
```

`plan_summary` keys:

```
total_decisions             int
approved                    int
rejected                    int
auto_approved               int
approval_rate               float  (%)
average_risk_score          float  (0.0â€“1.0)
average_approval_time_ms    float
```

---

#### `DashboardAPI.get_timeseries_data(metric, timerange, provider) â†’ TimeSeriesData | None`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `metric` | `str` | required | `latency`, `success_rate`, `token_usage`, `memory_latency` |
| `timerange` | `str` | `"24h"` | `"1h"`, `"24h"`, `"7d"` |
| `provider` | `str \| None` | `None` | Filter to a specific adapter provider key |

**Returns** â€” `TimeSeriesData`

| Field | Type | Description |
|---|---|---|
| `metric` | `str` | Metric name |
| `unit` | `str` | `ms`, `%`, `tokens` |
| `points` | `List[TimeSeriesPoint]` | Data points, bounded by requested range and service retention |
| `min_value` | `float` | Minimum value across points |
| `max_value` | `float` | Maximum value across points |
| `avg_value` | `float` | Average value across points |

`TimeSeriesPoint` fields: `timestamp` (ISO str), `value` (float), `metadata` (dict).

---

#### `DashboardAPI.search_traces(trace_id, limit) â†’ List[TraceInfo]`

| Parameter | Type | Default | Description |
|---|---|---|---|
| `trace_id` | `str \| None` | `None` | Exact trace ID to search; `None` = list all |
| `limit` | `int` | `100` | Maximum results (hard cap: 1 000) |

**Returns** â€” `List[TraceInfo]`, sorted by `start_time` descending.

`TraceInfo` fields: `trace_id`, `start_time`, `duration_ms`, `span_count`, `status`, `root_operation`.

---

#### `DashboardAPI.get_trace_detail(trace_id) â†’ TraceDetail | None`

Returns the full `TraceDetail` (including all spans) for the given ID, or
`None` if not found.

`TraceDetail` fields: `trace_id`, `start_time`, `end_time`, `duration_ms`, `status`, `spans` (`List[dict]`, bounded by trace ingestion limits).

---

#### `DashboardAPI.add_trace(trace_detail) â†’ bool`

Store a `TraceDetail`. The circular buffer holds at most **1 000** traces;
the oldest is evicted when the limit is exceeded. Returns `True` on success.

---

#### `DashboardAPI.clear_traces() â†’ None`

Discard all stored traces in the in-process dashboard API. HTTP mutation authorization belongs to the native kernel route layer, not this Python dashboard helper.

---

#### `DashboardAPI.get_stats() â†’ dict`

```json
{
  "total_traces_stored": 42,
  "trace_list_size": 42,
  "timestamp": "2026-03-03T21:00:00+00:00"
}
```

---

### AlertEngine

```python
from vetinari.dashboard.alerts import (
    get_alert_engine,
    reset_alert_engine,
    AlertThreshold,
    AlertCondition,
    AlertSeverity,
    AlertRecord,
)
```

**Singleton access**

| Function | Returns | Description |
|---|---|---|
| `get_alert_engine()` | `AlertEngine` | Return (or create) the global singleton |
| `reset_alert_engine()` | `None` | Destroy the singleton |

---

#### `AlertThreshold` dataclass

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | required | Unique rule name |
| `metric_key` | `str` | required | Dot-notation path into `MetricsSnapshot.to_dict()` |
| `condition` | `AlertCondition` | required | `GREATER_THAN`, `LESS_THAN`, `EQUALS` |
| `threshold_value` | `float` | required | Comparison value |
| `severity` | `AlertSeverity` | `MEDIUM` | `LOW`, `MEDIUM`, `HIGH` |
| `channels` | `List[str]` | `["log"]` | `log`, `email`, `webhook` (or custom) |
| `duration_seconds` | `int` | `0` | Seconds condition must hold before firing (0 = immediate) |

---

#### `AlertEngine` methods

| Method | Returns | Description |
|---|---|---|
| `register_threshold(t)` | `None` | Add or replace by `t.name` |
| `unregister_threshold(name)` | `bool` | Remove; returns `True` if existed |
| `clear_thresholds()` | `None` | Remove all thresholds and reset state |
| `list_thresholds()` | `List[AlertThreshold]` | All registered thresholds |
| `evaluate_all(api=None)` | `List[AlertRecord]` | Evaluate and return newly-fired alerts |
| `get_active_alerts()` | `List[AlertRecord]` | Currently firing alerts |
| `get_history()` | `List[AlertRecord]` | All alerts ever fired this session |
| `get_stats()` | `dict` | `registered_thresholds`, `active_alerts`, `total_fired` |

---

#### `AlertRecord` dataclass

| Field | Type | Description |
|---|---|---|
| `threshold` | `AlertThreshold` | The rule that fired |
| `current_value` | `float` | Metric value at time of firing |
| `trigger_time` | `float` | Unix timestamp of firing |

---

### LogAggregator

```python
from vetinari.dashboard.log_aggregator import (
    get_log_aggregator,
    reset_log_aggregator,
    LogRecord,
    AggregatorHandler,
    FileBackend,
    ElasticsearchBackend,
    SplunkBackend,
    DatadogBackend,
)
```

**Singleton access**

| Function | Returns | Description |
|---|---|---|
| `get_log_aggregator()` | `LogAggregator` | Return (or create) the global singleton |
| `reset_log_aggregator()` | `None` | Flush pending records and destroy singleton |

---

#### `LogRecord` dataclass

| Field | Type | Default | Description |
|---|---|---|---|
| `message` | `str` | required | Log message text |
| `level` | `str` | `"INFO"` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `timestamp` | `float` | `time.time()` | Unix timestamp |
| `trace_id` | `str \| None` | `None` | Distributed trace ID |
| `span_id` | `str \| None` | `None` | Span ID within trace |
| `request_id` | `str \| None` | `None` | HTTP / task request ID |
| `logger_name` | `str \| None` | `None` | Python logger name |
| `extra` | `dict` | `{}` | Arbitrary key-value metadata |

All `LogRecord.to_dict()` and `LogRecord.to_json()` outputs pass string values through the shared PII redaction gate before file, webhook, Datadog, SSE, or recent-log emission.

---

#### `LogAggregator` methods

**Backend management**

| Method | Returns | Description |
|---|---|---|
| `configure_backend(name, **kwargs)` | `None` | Add/replace backend; raises `ValueError` for unknown names |
| `remove_backend(name)` | `bool` | Remove backend; returns `True` if existed |
| `list_backends()` | `List[str]` | Active backend names |

**Ingestion**

| Method | Returns | Description |
|---|---|---|
| `ingest(record)` | `None` | Add one record; auto-flushes at `_batch_size` |
| `ingest_many(records)` | `None` | Add multiple records |
| `flush()` | `None` | Force-send all pending records to all backends |

**Search**

| Method | Returns | Description |
|---|---|---|
| `search(trace_id, level, logger_name, message_contains, since, limit)` | `List[LogRecord]` | AND-filtered search; results newest-first |
| `get_trace_records(trace_id)` | `List[LogRecord]` | All records for a trace, oldest-first |
| `correlate_span(trace_id, span_id)` | `List[LogRecord]` | All records for a specific span |

**Introspection**

| Method | Returns | Description |
|---|---|---|
| `get_stats()` | `dict` | `buffer_size`, `pending`, `backends`, `max_buffer`, `batch_size` |
| `clear_buffer()` | `None` | Discard all buffered records |

---

#### Backend configuration kwargs

**FileBackend**

| kwarg | Type | Default | Description |
|---|---|---|---|
| `path` | `str` | `"logs/vetinari_audit.jsonl"` | Output file path (directories created automatically) |

**ElasticsearchBackend**

| kwarg | Type | Default | Description |
|---|---|---|---|
| `url` | `str` | configured | Elasticsearch base URL |
| `index` | `str` | `"vetinari-logs"` | Target index name |
| `api_key` | `str \| None` | `None` | API key (`id:key` base64-encoded) |

**SplunkBackend**

| kwarg | Type | Default | Description |
|---|---|---|---|
| `url` | `str` | configured | Splunk HEC base URL |
| `token` | `str` | `""` | HEC token |
| `source` | `str` | `"vetinari"` | Splunk `source` field |
| `sourcetype` | `str` | `"_json"` | Splunk `sourcetype` field |

**DatadogBackend**

| kwarg | Type | Default | Description |
|---|---|---|---|
| `api_key` | `str` | `""` | Datadog API key |
| `service` | `str` | `"vetinari"` | `service` tag |
| `ddsource` | `str` | `"python"` | `ddsource` tag |
| `ddtags` | `str` | `""` | Comma-separated `ddtags` |

---

## REST API

The retired Python `/api/v1` dashboard HTTP routes are not the current runtime API surface. Dashboard metrics, traces, alerts, and log aggregation in this file are maintained as in-process Python APIs. The active HTTP host is the Rust Axum kernel under `crates/amw-kernel/`.

Current Workbench HTTP routes are implemented in `crates/amw-kernel/src/api/routes/workbench_domains.rs` and mounted by `crates/amw-kernel/src/bin/amw-kernel-server.rs`. Use the Rust route tests and kernel request dispatch tests as the route contract instead of the retired dashboard endpoint table.

Representative validation:

- `cargo test -p amw-kernel api::routes::workbench_domains::tests::kernel_request_dispatches_migration_owned_routes_without_http_proxy`
- `cargo test -p amw-kernel api::routes::workbench_domains::tests::api_kernel_contracts_training_control_and_capability_catalog_are_native`

---

## Data Schemas

### MetricsSnapshot

```python
@dataclass
class MetricsSnapshot:
    timestamp: str
    uptime_ms: float
    adapter_summary: Dict[str, Any]
    memory_summary: Dict[str, Any]
    plan_summary: Dict[str, Any]
```

### TimeSeriesData

```python
@dataclass
class TimeSeriesData:
    metric: str
    unit: str
    points: List[TimeSeriesPoint]
    min_value: float
    max_value: float
    avg_value: float
```

### TraceDetail

```python
@dataclass
class TraceDetail:
    trace_id: str
    start_time: str
    end_time: str
    duration_ms: float
    status: str  # "success" | "error" | "in_progress"
    spans: List[Dict[str, Any]]
```

### AlertThreshold

```python
@dataclass
class AlertThreshold:
    name: str
    metric_key: str
    condition: AlertCondition  # GREATER_THAN | LESS_THAN | EQUALS
    threshold_value: float
    severity: AlertSeverity  # LOW | MEDIUM | HIGH
    channels: List[str]
    duration_seconds: int
```

### LogRecord

```python
@dataclass
class LogRecord:
    message: str
    level: str
    timestamp: float
    trace_id: Optional[str]
    span_id: Optional[str]
    request_id: Optional[str]
    logger_name: Optional[str]
    extra: Dict[str, Any]
```

---

## Analytics And Inference Config

The old Phase 5 Python `/api/v1/analytics/*` and `/api/v1/config/*` tables are retired route documentation. Keep analytics and inference-profile work on the in-process Python modules unless a Rust-kernel route is added and tested in `crates/amw-kernel/src/api/routes/workbench_domains.rs`.

Current references:

- Analytics objects: `vetinari.dashboard.*` in-process APIs.
- Inference profile loading: `vetinari.config.inference_config` and `config/inference_profiles.yaml`.
- Native HTTP route authority: `crates/amw-kernel/src/api/routes/workbench_domains.rs`.

---

## RCG-0062-P02 Evidence Guard

Dashboard route claims are guarded by
`tests/test_frontend_package_boundary.py`, which verifies that legacy frontend
and API route metadata remains explicitly retired and points operators at the
Rust/Axum kernel boundary instead of a dormant browser dashboard route.
