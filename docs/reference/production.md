# AM Workbench — Production Deployment Guide

**Operational deployment guide | Last drift check: 2026-05-20**

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Environment Configuration](#environment-configuration)
3. [Running the Dashboard Server](#running-the-dashboard-server)
4. [Configuring Alerts](#configuring-alerts)
5. [Log Aggregation Setup](#log-aggregation-setup)
6. [Analytics & SLA Monitoring](#analytics--sla-monitoring)
7. [Security Hardening](#security-hardening)
8. [Health Checks & Monitoring](#health-checks--monitoring)
9. [Performance Tuning](#performance-tuning)
10. [Backup & Recovery](#backup--recovery)
11. [Upgrading](#upgrading)
12. [Troubleshooting](#troubleshooting)

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11+ (project floor); CI also exercises 3.12 and 3.13 |
| Rust toolchain | Stable Rust compatible with the workspace `rust-version` |
| llama-cpp-python | latest (installed via pyproject.toml) |
| OS | Linux (Ubuntu 22.04+), macOS 13+, Windows 10+ |
| RAM | 8 GB minimum; 32 GB recommended for large models |
| Disk | 20 GB for application + model storage |

---

## Environment Configuration

The supported runtime reads configuration from:

1. environment variables
2. `~/.vetinari/config.yaml`
3. project defaults in `config/models.yaml`

Python 3.11 remains within the official CPython security-support window
through 2027-10. Do not publish production guidance that implies Python 3.10
is supported unless `pyproject.toml` is lowered and CI re-adds 3.10 coverage.

```bash
python -m venv .venv312
source .venv312/bin/activate
pip install -e ".[dev,local,ml,search,notifications]"
```

### Required variables

| Variable | Description | Example |
|---|---|---|
| `VETINARI_MODELS_DIR` | Directory containing llama-cpp GGUF model files (one backend among many — vLLM/SGLang/NIM resolve via HuggingFace repo IDs instead) | `/var/lib/vetinari/models` |

### Optional variables

| Variable | Default | Description |
|---|---|---|
| `VETINARI_NATIVE_MODELS_DIR` | `./models/native` | Native-model directory for `vllm`/NIM |
| `VETINARI_VLLM_ENDPOINT` | `http://localhost:8000` | OpenAI-compatible `vllm` endpoint |
| `VETINARI_NIM_ENDPOINT` | `http://localhost:8001` | OpenAI-compatible NVIDIA NIM endpoint |
| `VETINARI_WEB_PORT` | `5000` | Dashboard server port |
| `VETINARI_WEB_HOST` | `127.0.0.1` | Dashboard bind address |
| `VETINARI_LOG_LEVEL` | `INFO` | Python log level |
| `LOG_FILE` | `logs/vetinari.log` | Log output path |
| `TELEMETRY_EXPORT_PATH` | `logs/telemetry.json` | Telemetry export file |
| `DD_API_KEY` | — | Datadog API key for log aggregation |
| `ES_URL` | — | Elasticsearch URL for log aggregation |
| `SPLUNK_HEC_TOKEN` | — | Splunk HEC token |

### Native backend note

On Windows, the supported `vllm` path is to run `vllm` inside WSL and export `VETINARI_VLLM_ENDPOINT` in the Windows shell that starts AM Workbench. See [README.md](../../README.md) for the operator steps and download links.

---

## Running the Dashboard Server

### Development

```bash
python -m vetinari serve --port 5000
```

### Production (native Rust kernel server)

```bash
python -m pip install -e ".[dev,local,ml,search,notifications]"
cargo run -p amw-kernel --bin amw-kernel-server -- --host 127.0.0.1 --port 5000
```

The supported API-domain deployment topology is the native Rust kernel server
serving the migrated AM Workbench route surface. Litestar compatibility modules
remain for protected sibling surfaces and historical route tests, but they are
not the live AM Workbench API host.

### systemd service

```ini
# /etc/systemd/system/vetinari-dashboard.service
[Unit]
Description=AM Workbench Monitoring Dashboard
After=network.target

[Service]
Type=simple
User=vetinari
WorkingDirectory=/opt/vetinari
Environment=PYTHONPATH=/opt/vetinari
ExecStart=/usr/bin/cargo run -p amw-kernel --bin amw-kernel-server -- \
    --host 127.0.0.1 \
    --port 5000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable vetinari-dashboard
sudo systemctl start vetinari-dashboard
```

### Nginx reverse proxy (recommended)

```nginx
server {
    listen 80;
    server_name dashboard.yourdomain.com;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_read_timeout 60s;
    }
}
```

---

## Configuring Alerts

Register thresholds at startup. Recommended production defaults:

```python
from vetinari.dashboard.alerts import (
    get_alert_engine,
    AlertThreshold,
    AlertCondition,
    AlertSeverity,
)

engine = get_alert_engine()

engine.register_threshold(
    AlertThreshold(
        name="high-adapter-latency",
        metric_key="adapters.average_latency_ms",
        condition=AlertCondition.GREATER_THAN,
        threshold_value=500.0,
        severity=AlertSeverity.HIGH,
        channels=["log"],
        duration_seconds=60,  # sustained for 1 min before firing
    )
)

engine.register_threshold(
    AlertThreshold(
        name="low-success-rate",
        metric_key="adapters.total_failed",
        condition=AlertCondition.GREATER_THAN,
        threshold_value=10.0,
        severity=AlertSeverity.HIGH,
        channels=["log"],
    )
)

engine.register_threshold(
    AlertThreshold(
        name="low-plan-approval",
        metric_key="plan.approval_rate",
        condition=AlertCondition.LESS_THAN,
        threshold_value=70.0,
        severity=AlertSeverity.MEDIUM,
        channels=["log"],
    )
)
```

Run `engine.evaluate_all()` on a schedule (e.g. every 30 s via APScheduler
or a background thread).

### Webhook integration

```python
from vetinari.dashboard.alerts import DISPATCHERS, AlertRecord
import requests


def pagerduty_dispatcher(alert: AlertRecord) -> None:
    requests.post(
        "https://events.pagerduty.com/v2/enqueue",
        json={
            "routing_key": "YOUR_INTEGRATION_KEY",
            "event_action": "trigger",
            "payload": {
                "summary": f"[{alert.threshold.severity.value.upper()}] {alert.threshold.name}",
                "source": "vetinari",
                "severity": alert.threshold.severity.value,
                "custom_details": alert.to_dict(),
            },
        },
    )


DISPATCHERS["pagerduty"] = pagerduty_dispatcher
```

---

## Log Aggregation Setup

### File (always recommended as a baseline)

```python
from vetinari.dashboard.log_aggregator import get_log_aggregator

agg = get_log_aggregator()
agg.configure_backend("file", path="logs/vetinari_audit.jsonl")
```

### Elasticsearch

```python
agg.configure_backend(
    "elasticsearch",
    url="https://elastic.yourhost.com:9200",
    index="vetinari-prod",
    api_key="YOUR_ES_API_KEY",
)
```

### Datadog

```python
import os

agg.configure_backend(
    "datadog",
    api_key=os.environ["DD_API_KEY"],
    service="vetinari",
    ddsource="python",
    ddtags="env:prod,region:us-east-1",
)
```

### Flush on shutdown

```python
import atexit

atexit.register(get_log_aggregator().flush)
```

---

## Analytics & SLA Monitoring

### Recommended SLOs for production

```python
from vetinari.analytics.sla import get_sla_tracker, SLOTarget, SLOType

sla = get_sla_tracker()

sla.register_slo(
    SLOTarget(
        name="adapter-p95-latency",
        slo_type=SLOType.LATENCY_P95,
        budget=500.0,
        window_seconds=3600,
        description="95% of adapter calls complete within 500 ms",
    )
)

sla.register_slo(
    SLOTarget(
        name="adapter-success-rate",
        slo_type=SLOType.SUCCESS_RATE,
        budget=99.0,
        window_seconds=3600,
        description="Adapter success rate >= 99%",
    )
)
```

### Cost budget alerts

```python
from vetinari.analytics.cost import get_cost_tracker

tracker = get_cost_tracker()
report = tracker.get_report()
if report.total_cost_usd > 10.0:  # $10 daily budget
    logger.warning("Daily cost budget exceeded: $%.4f", report.total_cost_usd)
```

---

## Security Hardening

1. **Secret scanning**: all log output must go through the secret scanner
   before being written to external systems:

   ```python
   from vetinari.security import get_secret_scanner

   scanner = get_secret_scanner()
   safe_payload = scanner.sanitize_dict(metrics_dict)
   ```

2. **CORS**: restrict `Access-Control-Allow-Origin` in production at the native
   Rust kernel API host or the fronting reverse proxy.

3. **HTTPS**: always terminate TLS at Nginx/load balancer before the app.

4. **Database**: the unified SQLite store defaults to `.vetinari/vetinari.db`.
   Restrict file permissions on that file or set `VETINARI_DB_PATH` explicitly.

---

## Health Checks & Monitoring

### Health endpoint

```
GET http://localhost:5000/health
-> { "status": "ok", "server": "amw-kernel" }

GET http://localhost:5000/api/v1/health
-> { "status": "healthy" | "degraded", "checks": { ... }, "timestamp": "..." }
```

Use `/health` for a low-level process liveness probe. Use `/api/v1/health` for
operator diagnostics; it is a public composite status surface and may report
partial subsystem failures as `degraded`.

### Prometheus scrape (optional)

Export telemetry to Prometheus format:

```python
from vetinari.telemetry import get_telemetry_collector

telemetry = get_telemetry_collector()
telemetry.export_prometheus("logs/metrics.prom")
```

Point your Prometheus scraper at the file or expose it via a custom endpoint.

---

## Performance Tuning

| Setting | Default | Recommendation |
|---|---|---|
| Uvicorn workers | 1 | Keep a single process while the state stores remain SQLite-backed |
| Log aggregator batch size | 100 | Increase to 500 for high-throughput |
| Anomaly window size | 50 | Increase to 200 for noisier signals |
| Trace buffer | 1 000 | Increase to 5 000 if trace detail is important |
| Auto-refresh interval (UI) | 15 s | 30 s or 60 s for low-traffic deployments |

### Memory footprint

- Log aggregator buffer: ~5 000 records × ~500 bytes ≈ 2.5 MB
- Telemetry: O(providers × models) — typically < 1 MB
- Trace buffer: 1 000 × ~2 KB ≈ 2 MB

---

## Backup & Recovery

```bash
# Back up the unified SQLite database while the process can be running.
sqlite3 .vetinari/vetinari.db ".backup 'backups/vetinari_$(date +%Y%m%d).db'"

# If sqlite3 is unavailable, stop AM Workbench first, then copy the DB plus WAL/SHM files together.
cp .vetinari/vetinari.db backups/
cp .vetinari/vetinari.db-wal backups/ 2>/dev/null || true
cp .vetinari/vetinari.db-shm backups/ 2>/dev/null || true

# Back up audit log
cp logs/vetinari_audit.jsonl backups/

# Back up user configuration
cp ~/.vetinari/config.yaml backups/config_$(date +%Y%m%d).yaml

# Restore only while AM Workbench is stopped.
cp backups/vetinari_20260303.db .vetinari/vetinari.db
```

Before accepting a backup as restorable, run the manifest drill:

```bash
python scripts/check_backup_restore_drill.py --self-test
python scripts/check_backup_restore_drill.py --strict
python scripts/check_backup_restore_drill.py --backup-dir /path/to/vetinari-backup
python scripts/backup_restore_state.py restore /path/to/vetinari-backup --dry-run
```

The drill must reject corrupt manifests, missing backup payloads, unsafe restore
targets, stale recovery markers, checksum mismatches, and partial writes before
its strict result is trusted. The specific backup must also pass `--backup-dir`
and dry-run restore before any overwrite. Metadata-spine JSONL remains the
source of truth for Workbench state; a SQLite index without its JSONL append log
is damaged state, not a clean restore source.

---

## Upgrading

```bash
git pull origin main
pip install -e ".[dev,local,ml,search,notifications]"

# Run migrations
python -m vetinari migrate

# Verify tests still pass
pytest tests/ -q

# Restart the service
sudo systemctl restart vetinari-dashboard
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Dashboard shows stale data | No adapter calls recorded | Ensure agents are running and recording telemetry |
| `/api/v1/health` returns 500 | Python exception in startup | Check `logs/error.log`; `journalctl -u vetinari-dashboard` |
| Alert never fires | `evaluate_all()` not being called | Add a background scheduler thread |
| High memory usage | Log aggregator buffer full | Reduce `MAX_BUFFER` or call `clear_buffer()` periodically |
| Elasticsearch rejecting batches | Index template mismatch | Delete and recreate the index; check field type conflicts |
| Native kernel process restarting | Timeout on slow model calls or startup failure | Check `journalctl -u vetinari-dashboard`, reduce concurrent workload, and inspect model endpoint health |
| SQLite locked | Concurrent process or long-running transaction against the SQLite state store | Stop extra AM Workbench processes, let the lock clear, and back up the SQLite file before repair; PostgreSQL is not a supported remediation in the current runtime |
