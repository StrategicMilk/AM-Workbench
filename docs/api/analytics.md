# AM Workbench Analytics — API Reference

**Phase 5 | Last Updated: March 2026**

---

## Table of Contents

1. [Anomaly Detection](#anomaly-detection)
2. [Cost Attribution](#cost-attribution)
3. [SLA / SLO Tracking](#sla--slo-tracking)
4. [Forecasting & Capacity Planning](#forecasting--capacity-planning)

---

## Anomaly Detection

```python
from vetinari.analytics import (
    get_anomaly_detector,
    reset_anomaly_detector,
    AnomalyConfig,
    AnomalyResult,
)
```

### `AnomalyConfig` dataclass

| Field | Type | Default | Description |
|---|---|---|---|
| `window_size` | `int` | `50` | Rolling window length (observations) |
| `z_threshold` | `float` | `3.0` | Z-score sigma threshold |
| `iqr_factor` | `float` | `1.5` | IQR fence multiplier (k in Q1-k·IQR, Q3+k·IQR) |
| `ewma_alpha` | `float` | `0.2` | EWMA smoothing factor (0 < α < 1) |
| `ewma_threshold` | `float` | `3.0` | EWMA sigma deviation threshold |
| `min_samples` | `int` | `5` | Minimum window size before flagging |

### `AnomalyResult` dataclass

| Field | Type | Description |
|---|---|---|
| `metric` | `str` | Metric name |
| `value` | `float` | Observed value |
| `timestamp` | `float` | Unix timestamp |
| `is_anomaly` | `bool` | True if an anomaly was detected |
| `method` | `str` | `"zscore"`, `"iqr"`, `"ewma"`, or `""` |
| `score` | `float` | Magnitude (sigma count / fence ratio) |
| `reason` | `str` | Human-readable explanation |

### `AnomalyDetector` methods

| Method | Returns | Description |
|---|---|---|
| `configure(config)` | `None` | Update detector configuration |
| `detect(metric, value)` | `AnomalyResult` | Ingest one observation; run all three detectors |
| `scan_snapshot(snapshot)` | `List[AnomalyResult]` | Walk a `MetricsSnapshot` and flag every anomalous leaf |
| `get_history(metric=None)` | `List[AnomalyResult]` | All flagged anomalies; optionally filtered by metric |
| `clear_history()` | `None` | Discard anomaly history |
| `clear_state()` | `None` | Discard history AND rolling windows |
| `get_stats()` | `dict` | `tracked_metrics`, `total_anomalies`, `config` |

**Detection priority**: Z-score → IQR → EWMA.  The first detector to fire
wins; a result is returned immediately without checking the remaining methods.

---

## Cost Attribution

```python
from vetinari.analytics import (
    get_cost_tracker,
    reset_cost_tracker,
    CostEntry,
    CostReport,
    ModelPricing,
)
```

### `ModelPricing` dataclass

| Field | Type | Default | Description |
|---|---|---|---|
| `input_per_1k` | `float` | `0.0` | USD per 1 000 input tokens |
| `output_per_1k` | `float` | `0.0` | USD per 1 000 output tokens |
| `per_request` | `float` | `0.0` | Flat fee per API call |

`pricing.compute(input_tokens, output_tokens) → float`

### `CostEntry` dataclass

| Field | Type | Default | Description |
|---|---|---|---|
| `provider` | `str` | required | Provider name (e.g. `"openai"`) |
| `model` | `str` | required | Model name (e.g. `"gpt-4"`) |
| `input_tokens` | `int` | `0` | Input token count |
| `output_tokens` | `int` | `0` | Output token count |
| `agent` | `str\|None` | `None` | Originating agent name |
| `task_id` | `str\|None` | `None` | Task identifier |
| `timestamp` | `float` | `time.time()` | Unix timestamp |
| `cost_usd` | `float` | `0.0` | Populated automatically by `record()` if 0 |
| `latency_ms` | `float` | `0.0` | Optional latency annotation |

### `CostReport` dataclass

| Field | Type | Description |
|---|---|---|
| `total_cost_usd` | `float` | Sum of all matched entry costs |
| `total_tokens` | `int` | Sum of input + output tokens |
| `total_requests` | `int` | Number of matched entries |
| `by_agent` | `dict[str, float]` | Cost per agent |
| `by_provider` | `dict[str, float]` | Cost per provider |
| `by_model` | `dict[str, float]` | Cost per `"provider:model"` |
| `by_task` | `dict[str, float]` | Cost per task_id |
| `entries` | `int` | Entry count (same as `total_requests`) |

### `CostTracker` methods

| Method | Returns | Description |
|---|---|---|
| `set_pricing(provider, model, pricing)` | `None` | Override pricing for a model (`"*"` as model = wildcard) |
| `get_pricing(provider, model)` | `ModelPricing` | Look up pricing; falls back to wildcard then zero |
| `record(entry)` | `CostEntry` | Record and auto-cost an entry |
| `get_report(agent, task_id, since)` | `CostReport` | Aggregate report with optional filters |
| `get_top_agents(n)` | `List[dict]` | N most expensive agents |
| `get_top_models(n)` | `List[dict]` | N most expensive `provider:model` combos |
| `get_stats()` | `dict` | `total_entries`, `configured_models` |
| `clear()` | `None` | Discard all recorded entries |

**Built-in pricing table** covers GPT-4, GPT-4o, GPT-3.5-turbo, Claude 3
variants, and a zero-cost wildcard for `llama_cpp:*`.

---

## SLA / SLO Tracking

```python
from vetinari.analytics import (
    get_sla_tracker,
    reset_sla_tracker,
    SLOTarget,
    SLOType,
    SLAReport,
    SLABreach,
)
```

### `SLOType` enum

| Value | Measurement | Budget semantics |
|---|---|---|
| `LATENCY_P50` | Median latency | Maximum ms |
| `LATENCY_P95` | 95th percentile latency | Maximum ms |
| `LATENCY_P99` | 99th percentile latency | Maximum ms |
| `SUCCESS_RATE` | `(successes / total) × 100` | Minimum % |
| `ERROR_RATE` | `(failures / total) × 100` | Maximum % |
| `THROUGHPUT` | `requests / elapsed_seconds` | Minimum req/s |
| `APPROVAL_RATE` | `(approved / total) × 100` | Minimum % |

### `SLOTarget` dataclass

| Field | Type | Default | Description |
|---|---|---|---|
| `name` | `str` | required | Unique identifier |
| `slo_type` | `SLOType` | required | Which metric to measure |
| `budget` | `float` | required | Threshold (semantics depend on type) |
| `window_seconds` | `float` | `3600.0` | Rolling evaluation window |
| `description` | `str` | `""` | Human-readable description |

### `SLAReport` dataclass

| Field | Type | Description |
|---|---|---|
| `slo` | `SLOTarget` | The objective being evaluated |
| `window_start` | `float` | Start of rolling window (unix timestamp) |
| `window_end` | `float` | End of rolling window |
| `total_samples` | `int` | Observations within window |
| `good_samples` | `int` | Observations satisfying the budget |
| `compliance_pct` | `float` | `good_samples / total_samples × 100` |
| `is_compliant` | `bool` | `compliance_pct >= 99.0` |
| `current_value` | `float` | Latest computed metric value |
| `breaches` | `List[SLABreach]` | Recorded breaches within window |

### `SLATracker` methods

| Method | Returns | Description |
|---|---|---|
| `register_slo(slo)` | `None` | Add a new SLO |
| `unregister_slo(name)` | `bool` | Remove by name; True if existed |
| `list_slos()` | `List[SLOTarget]` | All registered SLOs |
| `record_latency(key, latency_ms, success)` | `None` | Feed to all latency-type SLOs |
| `record_request(success)` | `None` | Feed to success/error-rate SLOs |
| `record_plan_decision(approved)` | `None` | Feed to approval-rate SLOs |
| `record_metric(slo_name, value, success)` | `None` | Direct feed to a named SLO |
| `get_report(slo_name)` | `SLAReport\|None` | Compute compliance for one SLO |
| `get_all_reports()` | `List[SLAReport]` | Compute compliance for all SLOs |
| `record_breach(breach)` | `None` | Manually record a breach event |
| `get_stats()` | `dict` | `registered_slos`, `total_breaches`, `slo_names` |
| `clear()` | `None` | Discard all observations and breaches |

---

## Forecasting & Capacity Planning

```python
from vetinari.analytics import (
    get_forecaster,
    reset_forecaster,
    ForecastRequest,
    ForecastResult,
)
```

### `ForecastRequest` dataclass

| Field | Type | Default | Description |
|---|---|---|---|
| `metric` | `str` | required | Metric name (must have ingested history) |
| `horizon` | `int` | `5` | Number of future steps to predict |
| `method` | `str` | `"linear_trend"` | `sma`, `exp_smoothing`, `linear_trend`, `seasonal` |
| `alpha` | `float` | `0.3` | Smoothing factor for `exp_smoothing` |
| `period` | `int` | `7` | Season length for `seasonal` |

### `ForecastResult` dataclass

| Field | Type | Description |
|---|---|---|
| `metric` | `str` | Metric name |
| `method` | `str` | Method used |
| `horizon` | `int` | Steps forecasted |
| `predictions` | `List[float]` | Point forecasts (length = horizon) |
| `confidence_lo` | `List[float]` | Lower 80% confidence bound |
| `confidence_hi` | `List[float]` | Upper 80% confidence bound |
| `trend_slope` | `float` | Rate of change per step (`linear_trend` / `seasonal`) |
| `rmse` | `float` | In-sample root mean squared error |
| `samples_used` | `int` | History length used |

### `Forecaster` methods

| Method | Returns | Description |
|---|---|---|
| `ingest(metric, value)` | `None` | Append one observation |
| `ingest_many(metric, values)` | `None` | Append multiple observations |
| `forecast(request)` | `ForecastResult` | Produce a forecast |
| `will_exceed(metric, threshold, horizon, method)` | `bool` | True if forecast crosses threshold within horizon |
| `steps_until_threshold(metric, threshold, horizon, method)` | `int\|None` | Steps until first threshold crossing, or None |
| `get_history(metric)` | `List[float]` | Raw stored values |
| `list_metrics()` | `List[str]` | All metrics with stored history |
| `get_stats()` | `dict` | `tracked_metrics`, `history_sizes` |
| `clear()` | `None` | Discard all history |

### Forecasting methods compared

| Method | Trend | Seasonality | Best for |
|---|---|---|---|
| `sma` | No | No | Stable, stationary series |
| `exp_smoothing` | Implicit (via decay) | No | Series with slow drift |
| `linear_trend` | Yes (OLS) | No | Steadily rising/falling series |
| `seasonal` | Yes (OLS) | Yes (additive) | Periodic workloads (daily/weekly) |

Minimum history requirements:
- `sma` / `exp_smoothing`: 2 points
- `linear_trend`: 2 points
- `seasonal`: `2 × period` points (falls back to `linear_trend` otherwise)
