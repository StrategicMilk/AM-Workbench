/**
 * Vetinari Monitoring Dashboard — dashboard.js
 *
 * Responsibilities:
 *   - Fetch metrics, timeseries, traces and alerts from the REST API
 *   - Render KPI cards, Chart.js charts, data tables, alert panels
 *   - Auto-refresh at a configurable interval
 *   - Sidebar navigation between sections
 *   - Trace search and detail modal
 */

'use strict';

/* ──────────────────────────────────────────────────────────────
   Configuration
   ────────────────────────────────────────────────────────────── */
const API_BASE = '/api/v1';
/** Configurable currency symbol — override via window.VConfig if set on the page. */
const _currencySymbol = (window.VConfig && window.VConfig.currencySymbol) || '$';

/* ──────────────────────────────────────────────────────────────
   State
   ────────────────────────────────────────────────────────────── */
let _refreshTimer   = null;
let _charts         = {};          // keyed by canvas id
// _activeAlerts and _alertHistory are populated from /api/v1/analytics/alerts
// on every refresh cycle — they are never client-only accumulations.
let _activeAlerts   = [];
let _alertHistory   = [];

/* ──────────────────────────────────────────────────────────────
   DOM helpers
   ────────────────────────────────────────────────────────────── */
const $  = (id) => document.getElementById(id);
const fmtMs  = (v) => v == null ? '—' : `${new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(+v)} ms`;
const fmtPct = (v) => v == null ? '—' : `${new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(+v)}%`;
const fmtNum = (v) => v == null ? '—' : (+v).toLocaleString();
const fmtTs  = (iso) => {
    if (!iso) return '—';
    try { return new Date(iso).toLocaleTimeString(); } catch { return iso; }
};
const fmtUnixTs = (epoch) => {
    if (epoch == null) return '—';
    try { return new Date(epoch * 1000).toLocaleTimeString(); } catch { return String(epoch); }
};

const _escapeDiv = document.createElement('div');
function escapeHtml(text) {
    _escapeDiv.textContent = String(text);
    return _escapeDiv.innerHTML;
}

function reportDashboardSectionError(sectionName, err) {
    console.warn(`${sectionName} fetch failed`, err);
    const targetId = sectionName === 'sla' ? 'slaTableBody' : 'anomalyTableBody';
    const colspan = sectionName === 'sla' ? 7 : 6;
    const target = $(targetId);
    if (target) {
        target.innerHTML = `<tr><td colspan="${colspan}" class="dash-error">${escapeHtml(sectionName)} unavailable</td></tr>`;
    }
}

function pill(text, type) {
    return `<span class="dash-pill dash-pill-${escapeHtml(type)}">${escapeHtml(text)}</span>`;
}

function statusPill(status) {
    const map = { success: 'success', error: 'error', in_progress: 'info' };
    return pill(status, map[status] || 'info');
}

/* ──────────────────────────────────────────────────────────────
   API calls
   ────────────────────────────────────────────────────────────── */
async function apiFetch(path) {
    const res = await fetch(`${API_BASE}${path}`);
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    return res.json();
}

/* ──────────────────────────────────────────────────────────────
   Status dot
   ────────────────────────────────────────────────────────────── */
function setStatus(online) {
    const dot   = $('statusDot');
    const label = $('statusLabel');
    if (dot) dot.className = `dash-status-dot ${online ? 'online' : 'offline'}`;
    if (label) label.textContent = online ? 'Connected' : 'Offline';
}

/* ──────────────────────────────────────────────────────────────
   Charts
   ────────────────────────────────────────────────────────────── */
// Read chart colours from CSS custom properties so they adapt to the active theme
function _chartColors() {
    const s = getComputedStyle(document.documentElement);
    return {
        tick: s.getPropertyValue('--text-muted').trim() || '#7a8491',
        gridX: s.getPropertyValue('--border-subtle').trim() || 'rgba(255,255,255,0.04)',
        gridY: s.getPropertyValue('--border-default').trim() || 'rgba(255,255,255,0.06)',
    };
}
function _chartDefaults() {
    const c = _chartColors();
    return {
        type: 'line',
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: { duration: 300 },
            plugins: { legend: { display: false } },
            scales: {
                x: { ticks: { color: c.tick, maxTicksLimit: 6 }, grid: { color: c.gridX } },
                y: { ticks: { color: c.tick }, grid: { color: c.gridY } }
            }
        }
    };
}

function makeChart(canvasId, label, colour) {
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;
    if (_charts[canvasId]) { _charts[canvasId].destroy(); }
    if (!window.Chart) {
        const fallbackId = `${canvasId}Fallback`;
        let fallback = document.getElementById(fallbackId);
        if (!fallback) {
            fallback = document.createElement('div');
            fallback.id = fallbackId;
            fallback.className = 'dash-chart-fallback';
            fallback.setAttribute('role', 'status');
            ctx.insertAdjacentElement('afterend', fallback);
        }
        ctx.hidden = true;
        fallback.textContent = `${label}: chart renderer unavailable`;
        _charts[canvasId] = {
            data: { labels: [], datasets: [{ data: [] }] },
            update() {
                const latest = this.data.datasets[0].data.at(-1);
                fallback.textContent = latest == null ? `${label}: no data` : `${label}: ${latest}`;
            },
            destroy() {
                fallback.remove();
                ctx.hidden = false;
            }
        };
        return _charts[canvasId];
    }
    const cfg = JSON.parse(JSON.stringify(_chartDefaults()));
    cfg.data = {
        labels: [],
        datasets: [{
            label,
            data: [],
            borderColor: colour,
            backgroundColor: colour.replace(')', ', 0.1)').replace('rgb', 'rgba'),
            borderWidth: 2,
            pointRadius: 3,
            tension: 0.35,
            fill: true
        }]
    };
    _charts[canvasId] = new Chart(ctx, cfg);
    return _charts[canvasId];
}

function updateChart(canvasId, timeseries) {
    const chart = _charts[canvasId];
    if (!chart || !timeseries || !timeseries.points) return;
    chart.data.labels   = timeseries.points.map(p => fmtTs(p.timestamp));
    chart.data.datasets[0].data = timeseries.points.map(p => p.value);
    chart.update('none');
}

function initCharts() {
    makeChart('latencyChart',    'Latency (ms)',   'rgb(78, 154, 249)');
    makeChart('successChart',    'Success (%)',    'rgb(56, 211, 159)');
    makeChart('tokenChart',      'Tokens',         'rgb(167, 139, 250)');
    makeChart('memLatencyChart', 'Mem Latency (ms)','rgb(33, 212, 253)');
}

/* ──────────────────────────────────────────────────────────────
   Latest metrics → KPI cards + tables
   ────────────────────────────────────────────────────────────── */
function renderOverviewKPIs(snap) {
    const a = snap.adapters || {};
    const p = snap.plan     || {};

    // Show welcome banner for new users with no activity yet
    const welcomeEl = $('dash-welcome-banner');
    if (welcomeEl) {
        const hasActivity = (a.total_requests || 0) > 0;
        welcomeEl.style.display = hasActivity ? 'none' : '';
    }

    $('kpi-total-requests').textContent = fmtNum(a.total_requests);
    $('kpi-avg-latency').textContent    = fmtMs(a.average_latency_ms);
    $('kpi-tokens').textContent         = fmtNum(a.total_tokens_used);

    const total = (a.total_successful || 0) + (a.total_failed || 0);
    const rate  = total > 0 ? new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format((a.total_successful / total) * 100) : '—';
    $('kpi-success-rate').textContent   = total > 0 ? `${rate}%` : '—';
    $('kpi-approval-rate').textContent  = fmtPct(p.approval_rate);

    $('kpi-active-alerts').textContent  = _activeAlerts.length;
    const badge = $('alertBadge');
    if (_activeAlerts.length > 0) {
        badge.textContent = _activeAlerts.length;
        badge.style.display = '';
    } else {
        badge.style.display = 'none';
    }

    const uptime = snap.uptime_ms != null
        ? `Uptime: ${new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(snap.uptime_ms / 1000)} s`
        : '';
    $('uptimeLabel').textContent = uptime;
}

function renderAdapterTable(snap) {
    const providers = (snap.adapters || {}).providers || {};
    const providerKeys = Object.keys(providers);

    // Populate provider filter dropdown
    const sel = $('latencyProviderFilter');
    const current = sel.value;
    // Clear existing options except first
    while (sel.options.length > 1) sel.remove(1);
    providerKeys.forEach(k => {
        const opt = document.createElement('option');
        opt.value = k; opt.textContent = k;
        sel.appendChild(opt);
    });
    if (current && providerKeys.includes(current)) sel.value = current;

    const body = $('adapterTableBody');
    if (!providerKeys.length) {
        body.innerHTML = '<tr><td colspan="8" class="dash-empty">No adapter data yet.</td></tr>';
        return;
    }
    body.innerHTML = providerKeys.map(k => {
        const p = providers[k];
        return `<tr>
            <td>${escapeHtml(p.provider || k)}</td>
            <td><code>${escapeHtml(p.model || '—')}</code></td>
            <td>${fmtNum(p.requests)}</td>
            <td>${fmtPct(p.success_rate)}</td>
            <td>${fmtMs(p.avg_latency_ms)}</td>
            <td>${fmtMs(p.min_latency_ms)} / ${fmtMs(p.max_latency_ms)}</td>
            <td>${fmtNum(p.tokens_used)}</td>
            <td>${fmtTs(p.last_request)}</td>
        </tr>`;
    }).join('');
    $('adapterTimestamp').textContent = fmtTs(snap.timestamp);
}

function renderMemoryTable(snap) {
    const backends = (snap.memory || {}).backends || {};
    const keys = Object.keys(backends);
    const body = $('memoryTableBody');
    if (!keys.length) {
        body.innerHTML = '<tr><td colspan="9" class="dash-empty">No memory data yet.</td></tr>';
        return;
    }
    body.innerHTML = keys.map(k => {
        const b = backends[k];
        return `<tr>
            <td>${escapeHtml(b.backend || k)}</td>
            <td>${fmtNum(b.writes)}</td>
            <td>${fmtNum(b.reads)}</td>
            <td>${fmtNum(b.searches)}</td>
            <td>${fmtMs(b.avg_write_latency_ms)}</td>
            <td>${fmtMs(b.avg_read_latency_ms)}</td>
            <td>${fmtMs(b.avg_search_latency_ms)}</td>
            <td>${fmtPct(b.dedup_hit_rate)}</td>
            <td>${fmtNum(b.sync_failures)}</td>
        </tr>`;
    }).join('');
}

function renderPlanKPIs(snap) {
    const p = snap.plan || {};
    $('plan-approved').textContent  = fmtNum(p.approved);
    $('plan-rejected').textContent  = fmtNum(p.rejected);
    $('plan-auto').textContent      = fmtNum(p.auto_approved);
    $('plan-rate').textContent      = fmtPct(p.approval_rate);
    $('plan-risk').textContent      = p.average_risk_score != null
        ? new Intl.NumberFormat(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 }).format(+p.average_risk_score) : '—';
    $('plan-time').textContent      = fmtMs(p.average_approval_time_ms);
}

/* ──────────────────────────────────────────────────────────────
   Traces
   ────────────────────────────────────────────────────────────── */
function renderTraceTable(data) {
    const traces = data.traces || [];
    $('traceCount').textContent = `${data.count || 0} trace(s)`;
    const body = $('traceTableBody');
    if (!traces.length) {
        body.innerHTML = '<tr><td colspan="7" class="dash-empty">No traces found.</td></tr>';
        return;
    }
    body.innerHTML = traces.map(t => `<tr>
        <td><code>${escapeHtml(t.trace_id)}</code></td>
        <td>${fmtTs(t.start_time)}</td>
        <td>${fmtMs(t.duration_ms)}</td>
        <td>${t.span_count}</td>
        <td>${statusPill(t.status)}</td>
        <td>${escapeHtml(t.root_operation || '—')}</td>
        <td><button class="dash-btn dash-btn-secondary dash-btn-sm" data-action="load-trace-detail" data-trace-id="${escapeHtml(t.trace_id)}">
            <i class="fas fa-eye"></i>
        </button></td>
    </tr>`).join('');
}

async function loadTraceDetail(traceId) {
    try {
        const detail = await apiFetch(`/traces/${encodeURIComponent(traceId)}`);
        showTraceModal(detail);
    } catch (e) {
        // showTraceDetail is not defined — display the error inline in the detail panel
        const panel = $('traceDetailPanel');
        const body  = $('traceDetailBody');
        if (panel && body) {
            body.innerHTML = `<p class="dash-error">Failed to load trace <code>${escapeHtml(traceId)}</code>: ${escapeHtml(e.message)}</p>`;
            panel.style.display = '';
        }
    }
}

function showTraceModal(detail) {
    $('modalTraceId').textContent = detail.trace_id;
    const spans = detail.spans || [];

    // Find max duration for bar scaling
    const maxDur = Math.max(...spans.map(s => s.duration_ms || 0), 1);

    $('modalSpanList').innerHTML = spans.length
        ? `<div class="dash-trace-timeline">${spans.map(s => {
            const dur = s.duration_ms || 0;
            const pct = Math.max((dur / maxDur) * 100, 1).toFixed(1);
            // Use span-level quality_score if present, fall back to trace-level
            const spQ = s.quality_score != null ? s.quality_score
                      : (detail.quality_score != null ? detail.quality_score : null);
            const spLabel = s.operation || s.span_id || '—';
            const spExplanation = spQ != null
                ? `${spLabel} — quality: ${Number(spQ).toFixed(2)}`
                : `${spLabel} — no quality data`;
            return `<div class="dash-span-row">
                <div class="dash-span-label">${renderMessageBubble(spLabel, spQ, spExplanation)}</div>
                <div class="dash-span-bar-wrap">
                    <div class="dash-span-bar" style="width:${pct}%"></div>
                </div>
                <div class="dash-span-dur">${fmtMs(dur)}</div>
            </div>`;
          }).join('')}</div>`
        : '<div class="dash-empty">No spans in this trace.</div>';

    $('traceModal').style.display = 'flex';
}

/* ──────────────────────────────────────────────────────────────
   Alerts panel (client-side, polled from /api/v1/stats)
   Note: The backend AlertEngine is Python-only; we poll /health
   and /stats and display client-side stored alert state.
   ────────────────────────────────────────────────────────────── */
function renderAlerts() {
    const activeEl  = $('activeAlertsList');
    const historyEl = $('alertHistoryList');
    $('activeAlertCount').textContent = _activeAlerts.length;

    activeEl.innerHTML = _activeAlerts.length
        ? _activeAlerts.map(a => alertItemHtml(a, true)).join('')
        : '<div class="dash-empty">No active alerts.</div>';

    historyEl.innerHTML = _alertHistory.length
        ? [..._alertHistory].reverse().map(a => alertItemHtml(a, false)).join('')
        : '<div class="dash-empty">No alerts fired yet.</div>';
}

function alertItemHtml(a, isActive) {
    const sevMap = {
        high:   { cls: 'dash-icon-red',    icon: 'fas fa-exclamation-circle' },
        medium: { cls: 'dash-icon-yellow', icon: 'fas fa-exclamation-triangle' },
        low:    { cls: 'dash-icon-blue',   icon: 'fas fa-info-circle' }
    };
    const sev = sevMap[a.severity] || sevMap.medium;
    // Backend guarantees `source` ("anomaly" or "sla_breach"); all other fields are
    // best-effort depending on which subsystem produced the alert.
    const label = escapeHtml(a.name || a.detail || a.source || 'Alert');
    const sourceTag = `<span class="dash-alert-source">${escapeHtml(a.source || '')}</span>`;
    let detail = '';
    if (a.metric_key != null) {
        const val = isNaN(+a.current_value) ? '—' : new Intl.NumberFormat(undefined, { minimumFractionDigits: 3, maximumFractionDigits: 3 }).format(+a.current_value);
        detail = `${escapeHtml(a.metric_key)} = ${val} (threshold ${escapeHtml(String(a.condition || ''))} ${escapeHtml(String(a.threshold_value || ''))})`;
    } else if (a.detail) {
        detail = escapeHtml(a.detail);
    }
    const time = a.trigger_time ? new Date(a.trigger_time * 1000).toLocaleTimeString() : '—';
    return `<div class="dash-alert-item">
        <div class="dash-alert-severity ${sev.cls}"><i class="${sev.icon}"></i></div>
        <div class="dash-alert-body">
            <div class="dash-alert-name">${label} ${sourceTag}</div>
            ${detail ? `<div class="dash-alert-detail">${detail}</div>` : ''}
        </div>
        <div class="dash-alert-time">${time}</div>
    </div>`;
}

/* ──────────────────────────────────────────────────────────────
   Analytics: Cost, SLA, Anomalies, Forecast
   ────────────────────────────────────────────────────────────── */
function renderCostSection(report, topData) {
    $('cost-total').textContent = report.total_cost_usd != null
        ? `${_currencySymbol}${new Intl.NumberFormat(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 }).format(+report.total_cost_usd)}` : '—';
    $('cost-tokens').textContent = fmtNum(report.total_tokens);
    $('cost-requests').textContent = fmtNum(report.total_requests);

    const agentBody = $('costAgentTableBody');
    const agents = (topData && topData.top_agents) || [];
    agentBody.innerHTML = agents.length
        ? agents.map(a => `<tr><td>${a.agent || '(unknown)'}</td><td>${_currencySymbol}${new Intl.NumberFormat(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 }).format(+a.cost_usd)}</td></tr>`).join('')
        : '<tr><td colspan="2" class="dash-empty">No agent cost data.</td></tr>';

    const modelBody = $('costModelTableBody');
    const models = (topData && topData.top_models) || [];
    modelBody.innerHTML = models.length
        ? models.map(m => `<tr><td><code>${m.model}</code></td><td>${_currencySymbol}${new Intl.NumberFormat(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 }).format(+m.cost_usd)}</td></tr>`).join('')
        : '<tr><td colspan="2" class="dash-empty">No model cost data.</td></tr>';
}

function renderSLASection(reports) {
    const body = $('slaTableBody');
    if (!reports || !reports.length) {
        body.innerHTML = '<tr><td colspan="7" class="dash-empty">No SLA data yet.</td></tr>';
        return;
    }
    body.innerHTML = reports.map(r => {
        const slo = r.slo || {};
        const compliant = r.is_compliant != null ? r.is_compliant : (r.compliance_pct >= (slo.budget || 0));
        const statusCls = compliant ? 'success' : 'error';
        return `<tr>
            <td>${slo.name || '—'}</td>
            <td>${slo.slo_type || '—'}</td>
            <td>${slo.budget != null ? new Intl.NumberFormat(undefined, { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(+slo.budget) : '—'}</td>
            <td>${r.current_value != null ? new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(+r.current_value) : '—'}</td>
            <td>${fmtPct(r.compliance_pct)}</td>
            <td>${fmtNum(r.total_samples)}</td>
            <td>${pill(compliant ? 'OK' : 'BREACH', statusCls)}</td>
        </tr>`;
    }).join('');
}

function renderAnomalySection(data) {
    const anomalies = data.anomalies || [];
    $('anomalyCount').textContent = `${anomalies.length} anomaly(s)`;
    const body = $('anomalyTableBody');
    if (!anomalies.length) {
        body.innerHTML = '<tr><td colspan="6" class="dash-empty">No anomalies detected.</td></tr>';
        return;
    }
    body.innerHTML = anomalies.map(a => `<tr>
        <td>${fmtUnixTs(a.timestamp)}</td>
        <td>${escapeHtml(a.metric || '—')}</td>
        <td>${new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(+a.value)}</td>
        <td>${escapeHtml(a.method || '—')}</td>
        <td>${new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(+a.score)}</td>
        <td>${escapeHtml(a.reason || '—')}</td>
    </tr>`).join('');
}

function renderForecastSection(result) {
    const preds = result.predictions || [];
    const lo = result.confidence_lo || [];
    const hi = result.confidence_hi || [];
    const body = $('forecastTableBody');
    if (!preds.length) {
        body.innerHTML = '<tr><td colspan="4" class="dash-empty">No forecast data.</td></tr>';
        $('forecastMeta').textContent = '';
        return;
    }
    body.innerHTML = preds.map((p, i) => `<tr>
        <td>${i + 1}</td>
        <td>${new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(+p)}</td>
        <td>${lo[i] != null ? new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(+lo[i]) : '—'}</td>
        <td>${hi[i] != null ? new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(+hi[i]) : '—'}</td>
    </tr>`).join('');
    $('forecastMeta').textContent = `Method: ${result.method || '—'} | RMSE: ${
        result.rmse != null ? new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(+result.rmse) : '—'} | Samples: ${
        fmtNum(result.samples_used)} | Slope: ${
        result.trend_slope != null ? new Intl.NumberFormat(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 }).format(+result.trend_slope) : '—'}`;
}

function runForecast() {
    // The singular /analytics/forecast endpoint no longer exists — show the
    // unavailable notice instead of making a dead HTTP request.
    populateForecastMetrics();
}

function populateForecastMetrics() {
    // The singular /analytics/forecast endpoint no longer exists — the backend
    // exposes /api/v1/analytics/forecasts (plural) which returns aggregate stats,
    // not a per-metric series.  Show a static notice until the UI is updated to
    // consume the new endpoint shape.
    const panel = $('forecastPanel') || $('forecastSection');
    if (!panel) return;
    const sel = $('forecastMetric');
    if (sel) sel.disabled = true;
    const notice = panel.querySelector('.forecast-unavailable-notice');
    if (!notice) {
        const div = document.createElement('p');
        div.className = 'dash-empty forecast-unavailable-notice';
        div.textContent = 'Forecast unavailable — analytics endpoint moved to /analytics/forecasts.';
        panel.prepend(div);
    }
}

/* ──────────────────────────────────────────────────────────────
   Main refresh cycle
   ────────────────────────────────────────────────────────────── */
async function refresh() {
    const icon = $('refreshIcon');
    icon.classList.add('dash-spinning');

    try {
        // 1. Health check
        await apiFetch('/health');
        setStatus(true);

        // 2. Latest metrics
        const snap = await apiFetch('/metrics/latest');
        renderOverviewKPIs(snap);
        renderAdapterTable(snap);
        renderMemoryTable(snap);
        renderPlanKPIs(snap);

        // 3. Time-series charts
        const provider = $('latencyProviderFilter').value || undefined;
        const providerQ = provider ? `&provider=${encodeURIComponent(provider)}` : '';

        const [latTS, sucTS, tokTS, memTS] = await Promise.allSettled([
            apiFetch(`/metrics/timeseries?metric=latency${providerQ}`),
            apiFetch('/metrics/timeseries?metric=success_rate'),
            apiFetch('/metrics/timeseries?metric=token_usage'),
            apiFetch('/metrics/timeseries?metric=memory_latency'),
        ]);

        if (latTS.status === 'fulfilled') updateChart('latencyChart',    latTS.value);
        if (sucTS.status === 'fulfilled') updateChart('successChart',    sucTS.value);
        if (tokTS.status === 'fulfilled') updateChart('tokenChart',      tokTS.value);
        if (memTS.status === 'fulfilled') updateChart('memLatencyChart', memTS.value);

        // 4. Traces (last 50)
        const traceData = await apiFetch('/traces?limit=50');
        renderTraceTable(traceData);

        // 4b. Alerts — fetch from backend and update module-level state before rendering.
        // Non-fatal: if the endpoint is unavailable the panel shows the last known state.
        try {
            const alertsRes = await apiFetch('/analytics/alerts');
            const incoming = alertsRes.alerts || [];
            // Active alerts are those currently firing; history is a rolling accumulation.
            _activeAlerts = incoming;
            // Append any new alert ids to history (avoid duplicates by trigger_time + source)
            const historyKeys = new Set(_alertHistory.map(a => `${a.source}|${a.trigger_time}`));
            for (const a of incoming) {
                const key = `${a.source}|${a.trigger_time}`;
                if (!historyKeys.has(key)) {
                    _alertHistory.push(a);
                    historyKeys.add(key);
                }
            }
            // Cap history at 200 entries to prevent unbounded growth
            if (_alertHistory.length > 200) _alertHistory = _alertHistory.slice(-200);
        } catch (e) {
            console.warn('Alerts fetch failed — displaying last known state.', e);
        }
        renderAlerts();

        // 5. Analytics sections (non-blocking — failures are silently ignored)
        Promise.allSettled([
            apiFetch('/analytics/cost'),
            apiFetch('/analytics/cost/top'),
        ]).then(([costRes, topRes]) => {
            if (costRes.status === 'fulfilled' && topRes.status === 'fulfilled') {
                // Backend returns {"cost": report} — unwrap the envelope before passing
                // the report object to renderCostSection.
                renderCostSection(costRes.value.cost, topRes.value);
            }
        });

        apiFetch('/analytics/sla').then(data => {
            // Backend returns {"sla": [...]} — key is "sla", not "reports".
            renderSLASection(data.sla || []);
        }).catch(err => reportDashboardSectionError('sla', err));

        apiFetch('/analytics/anomalies').then(data => {
            renderAnomalySection(data);
        }).catch(err => reportDashboardSectionError('anomalies', err));

        populateForecastMetrics();

    } catch (e) {
        setStatus(false);
        console.warn('Dashboard refresh error:', e);
    } finally {
        icon.classList.remove('dash-spinning');
    }
}

/* ──────────────────────────────────────────────────────────────
   Auto-refresh scheduling
   ────────────────────────────────────────────────────────────── */
function scheduleRefresh() {
    clearInterval(_refreshTimer);
    const toggle = $('autoRefreshToggle');
    if (!toggle.checked) return;
    const ms = parseInt($('refreshInterval').value, 10) || 15000;
    _refreshTimer = setInterval(refresh, ms);
}

/* ──────────────────────────────────────────────────────────────
   Navigation
   ────────────────────────────────────────────────────────────── */
function switchSection(name) {
    document.querySelectorAll('.dash-section').forEach(s => s.classList.remove('active'));
    document.querySelectorAll('.dash-nav-item').forEach(a => a.classList.remove('active'));

    const sec = document.getElementById(`section-${name}`);
    if (sec) sec.classList.add('active');

    const item = document.querySelector(`.dash-nav-item[data-section="${name}"]`);
    if (item) item.classList.add('active');

    const titles = {
        overview: 'Overview', adapters: 'Adapters', memory: 'Memory',
        plan: 'Plan Gate', traces: 'Traces', alerts: 'Alerts',
        cost: 'Cost', sla: 'SLA', anomalies: 'Anomalies', forecast: 'Forecast',
        pipeline: 'Pipeline', 'model-health': 'Model Health',
        'cost-breakdown': 'Cost Breakdown', 'decision-journal': 'Decision Journal',
        autonomy: 'Autonomy',
    };
    $('pageTitle').textContent = titles[name] || name;

    // Lazy-load section data when switching to it
    if (name === 'pipeline')          renderPipelineSection();
    if (name === 'model-health')      renderModelHealthSection();
    if (name === 'cost-breakdown')    renderCostBreakdownSection();
    if (name === 'decision-journal')  renderDecisionJournalSection();
    if (name === 'autonomy')          renderAutonomySection();
}

/* ──────────────────────────────────────────────────────────────
   Event bindings
   ────────────────────────────────────────────────────────────── */
function bindEvents() {
    // Sidebar nav
    document.querySelectorAll('.dash-nav-item').forEach(el => {
        el.addEventListener('click', (e) => {
            e.preventDefault();
            switchSection(el.dataset.section);
        });
    });

    // Refresh controls
    $('refreshNowBtn').addEventListener('click', refresh);
    $('autoRefreshToggle').addEventListener('change', scheduleRefresh);
    $('refreshInterval').addEventListener('change', scheduleRefresh);

    // Provider filter changes → re-fetch charts
    $('latencyProviderFilter').addEventListener('change', refresh);

    // Trace search
    $('traceSearchBtn').addEventListener('click', async () => {
        const id = $('traceSearchInput').value.trim();
        if (!id) return;
        try {
            const data = await apiFetch(`/traces?trace_id=${encodeURIComponent(id)}`);
            renderTraceTable(data);
        } catch (e) {
            $('traceTableBody').innerHTML = `<tr><td colspan="7" class="dash-empty">${escapeHtml(e.message)}</td></tr>`;
        }
    });

    $('traceClearBtn').addEventListener('click', async () => {
        $('traceSearchInput').value = '';
        try {
            const data = await apiFetch('/traces?limit=50');
            renderTraceTable(data);
        } catch (e) {
            $('traceTableBody').innerHTML = `<tr><td colspan="7" class="dash-error">Trace list unavailable: ${escapeHtml(String(e))}</td></tr>`;
        }
    });

    // Trace detail close
    $('traceDetailClose') && $('traceDetailClose').addEventListener('click', () => {
        $('traceDetailPanel').style.display = 'none';
    });

    // Trace modal close
    $('traceModalClose').addEventListener('click', () => {
        $('traceModal').style.display = 'none';
    });
    $('traceModal').addEventListener('click', (e) => {
        if (e.target === $('traceModal')) $('traceModal').style.display = 'none';
    });

    // Forecast run button
    const forecastBtn = $('forecastRunBtn');
    if (forecastBtn) forecastBtn.addEventListener('click', runForecast);

    // Clear alert history (client-side)
    $('clearAlertHistory').addEventListener('click', () => {
        _alertHistory = [];
        renderAlerts();
    });

    // Keyboard: Escape closes modal
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') $('traceModal').style.display = 'none';
    });

    // Delegated click handler for data-action buttons
    document.addEventListener('click', (e) => {
        const el = e.target.closest('[data-action]');
        if (!el) return;
        if (el.dataset.action === 'load-trace-detail') {
            loadTraceDetail(el.dataset.traceId);
        }
    });
}

/* ──────────────────────────────────────────────────────────────
   MessageBubble — confidence-coded output display
   ────────────────────────────────────────────────────────────── */

/**
 * Render a message bubble with a confidence-coded left border derived from
 * quality_score.  Solid green = HIGH (≥ 0.7), dashed amber = MEDIUM (0.4–0.7),
 * dotted red = LOW (< 0.4).  Hover title shows the explanation string.
 */
function renderMessageBubble(content, qualityScore, explanation) {
    const q = qualityScore != null ? Number(qualityScore) : -1;
    let borderStyle, label;
    if (q >= 0.7) {
        borderStyle = '3px solid #22c55e';
        label = 'HIGH';
    } else if (q >= 0.4) {
        borderStyle = '3px dashed #f59e0b';
        label = 'MEDIUM';
    } else if (q >= 0) {
        borderStyle = '3px dotted #ef4444';
        label = 'LOW';
    } else {
        borderStyle = '3px solid var(--border-subtle,rgba(255,255,255,0.1))';
        label = 'UNKNOWN';
    }
    const hoverText = explanation || (q >= 0
        ? `Confidence: ${label} (quality score: ${q.toFixed(2)})`
        : 'Confidence: unknown — no quality score available');
    return `<div class="message-bubble" style="border-left:${borderStyle};padding:0.35rem 0.65rem;margin:0.2rem 0;border-radius:0 4px 4px 0;background:var(--bg-card,rgba(255,255,255,0.03))" title="${escapeHtml(hoverText)}">${escapeHtml(content)}</div>`;
}

/* ──────────────────────────────────────────────────────────────
   Pipeline visualization
   ────────────────────────────────────────────────────────────── */

async function renderPipelineSection() {
    const el = $('pipelineSection');
    if (!el) return;
    el.innerHTML = '<p class="dash-loading">Loading pipeline\u2026</p>';
    try {
        const data = await apiFetch('/pipeline/status');
        const stages = data.stages || [];
        const activeStage = data.active_stage || null;

        let html = '<div class="pipeline-bar">';
        stages.forEach((s, i) => {
            const cls = s.name === activeStage ? 'active' : (s.exit_count > 0 ? 'complete' : 'idle');
            const dropPct = (s.drop_rate * 100).toFixed(1);
            html += `<div class="pipeline-stage ${escapeHtml(cls)}" title="${escapeHtml(s.name)}">
                <div>${escapeHtml(s.name.charAt(0).toUpperCase() + s.name.slice(1))}</div>
                <div class="pipeline-drop-rate">In: ${fmtNum(s.entry_count)} / Drop: ${dropPct}%</div>
            </div>`;
            if (i < stages.length - 1) {
                html += '<div style="padding:0 0.25rem;color:var(--text-muted,#7a8491)">&#9658;</div>';
            }
        });
        html += '</div>';

        if (stages.length === 0) {
            html = '<p class="dash-empty">No pipeline activity recorded yet.</p>';
        }
        el.innerHTML = html;
    } catch (err) {
        el.innerHTML = `<p class="dash-error">Pipeline data unavailable: ${escapeHtml(String(err))}</p>`;
    }

}

/* ──────────────────────────────────────────────────────────────
   Model health gauges
   ────────────────────────────────────────────────────────────── */

async function renderModelHealthSection() {
    const el = $('modelHealthSection');
    if (!el) return;
    el.innerHTML = '<p class="dash-loading">Loading health\u2026</p>';
    try {
        const data = await apiFetch('/dashboard/model-health');
        const dims = [
            { key: 'input_drift',    label: 'Input Drift' },
            { key: 'behavior_drift', label: 'Behavior Drift' },
            { key: 'quality_drift',  label: 'Quality Drift' },
        ];
        let html = '<div class="health-gauges">';
        dims.forEach(({ key, label }) => {
            const g = data[key] || { level: 'green', value: 0, detectors_triggered: [] };
            const pct = (g.value * 100).toFixed(1);
            const triggered = (g.detectors_triggered || []).join(', ') || 'none';
            html += `<div class="health-gauge ${escapeHtml(g.level)}" title="Detectors: ${escapeHtml(triggered)}" onclick="window._drillDrift('${escapeHtml(key)}')">
                <div class="health-gauge-label">${escapeHtml(label)}</div>
                <div class="health-gauge-value">${pct}%</div>
                <div style="font-size:0.75rem">${escapeHtml((g.level || '').toUpperCase())}</div>
            </div>`;
        });
        html += '</div>';
        el.innerHTML = html;
    } catch (err) {
        el.innerHTML = `<p class="dash-error">Health data unavailable: ${escapeHtml(String(err))}</p>`;
    }

}

window._drillDrift = function(key) {
    // Navigate to quality drift detail — logs intent, could be extended to a modal
    console.info('[dashboard] drill-down requested for drift dimension:', key);
    apiFetch('/dashboard/quality/drift-stats').then(data => {
        alert(`Drift stats for ${key}:\n${JSON.stringify(data, null, 2)}`);
    }).catch(err => {
        alert(`Drift stats unavailable for ${key}: ${err}`);
    });
};

/* ──────────────────────────────────────────────────────────────
   Cost breakdown section
   ────────────────────────────────────────────────────────────── */

async function renderCostBreakdownSection() {
    const el = $('costBreakdownSection');
    if (!el) return;
    el.innerHTML = '<p class="dash-loading">Loading cost breakdown\u2026</p>';
    try {
        const data = await apiFetch('/cost-analysis');
        const byStage = data.by_stage || {};
        const byModel = data.by_model || {};
        const byStageModel = data.by_stage_model || {};
        const zeroQuality = data.zero_quality_records || [];

        // Compute totals and find most expensive stage/model using actual token counts
        const stageTotals = Object.entries(byStage)
            .map(([name, val]) => ({ name, tokens: val.total_tokens || 0 }))
            .sort((a, b) => b.tokens - a.tokens);
        const modelTotals = Object.entries(byModel)
            .map(([name, val]) => ({ name, tokens: val.total_tokens || 0 }))
            .sort((a, b) => b.tokens - a.tokens);

        const totalTokens = stageTotals.reduce((s, x) => s + x.tokens, 0);
        const topStage = stageTotals[0] || null;
        const topModel = modelTotals[0] || null;

        let html = `<div style="margin-bottom:0.75rem;font-size:0.85rem">
            <strong>Total tokens:</strong> ${totalTokens.toLocaleString()}
            ${topStage ? ` &nbsp;|&nbsp; <strong>Costliest stage:</strong> ${escapeHtml(topStage.name)} (${topStage.tokens.toLocaleString()} tokens)` : ''}
            ${topModel ? ` &nbsp;|&nbsp; <strong>Costliest model:</strong> ${escapeHtml(topModel.name)} (${topModel.tokens.toLocaleString()} tokens)` : ''}
            ${zeroQuality.length ? ` &nbsp;|&nbsp; <span style="color:#f59e0b">${zeroQuality.length} zero-quality record(s)</span>` : ''}
        </div>`;

        // Render a stacked bar using Canvas if Chart.js available, else a table
        const canvasId = 'costBreakdownChart';
        html += `<canvas id="${canvasId}" height="120"></canvas>`;
        el.innerHTML = html;

        // Build Chart.js stacked bar using actual per-stage-per-model token counts
        const ctx = document.getElementById(canvasId);
        if (ctx && window.Chart) {
            const labels = stageTotals.map(s => s.name);
            // Collect all models that appear in any stage's cross-breakdown
            const allModels = [...new Set(
                stageTotals.flatMap(s => Object.keys(byStageModel[s.name] || {}))
            )];
            const datasets = allModels.map((modelName, i) => ({
                label: modelName,
                data: stageTotals.map(s => {
                    const stageBucket = byStageModel[s.name] || {};
                    return (stageBucket[modelName] || {}).total_tokens || 0;
                }),
                backgroundColor: `hsl(${(i * 60) % 360}, 60%, 50%)`,
            }));
            if (_charts[canvasId]) {
                _charts[canvasId].destroy();
                delete _charts[canvasId];
            }
            _charts[canvasId] = new Chart(ctx, {
                type: 'bar',
                data: { labels, datasets },
                options: {
                    responsive: true,
                    plugins: { legend: { display: true } },
                    scales: {
                        x: { stacked: true },
                        y: { stacked: true, title: { display: true, text: 'Tokens used' } },
                    },
                },
            });
        }
    } catch (err) {
        el.innerHTML = `<p class="dash-error">Cost data unavailable: ${escapeHtml(String(err))}</p>`;
    }
}

/* ──────────────────────────────────────────────────────────────
   Decision journal
   ────────────────────────────────────────────────────────────── */

let _journalFilters = { type: '', confidence_min: '', since_iso: '' };

async function renderDecisionJournalSection() {
    const el = $('decisionJournalSection');
    if (!el) return;
    el.innerHTML = '<p class="dash-loading">Loading decisions\u2026</p>';
    try {
        let path = '/decisions?limit=50';
        if (_journalFilters.type)           path += `&type=${encodeURIComponent(_journalFilters.type)}`;
        if (_journalFilters.confidence_min) path += `&confidence_min=${encodeURIComponent(_journalFilters.confidence_min)}`;
        if (_journalFilters.since_iso)      path += `&since_iso=${encodeURIComponent(_journalFilters.since_iso)}`;

        const data = await apiFetch(path);
        const decisions = data.decisions || [];

        const confidenceClass = (c) => {
            if (c === 'high') return 'confidence-high';
            if (c === 'low')  return 'confidence-low';
            return 'confidence-medium';
        };
        const confidenceLabel = (c) => ({ high: 'HIGH', medium: 'MED', low: 'LOW' }[c] || (c || '').toUpperCase());

        let filtersHtml = `<div style="display:flex;gap:0.5rem;flex-wrap:wrap;margin-bottom:0.75rem">
            <select id="djTypeFilter" onchange="window._djFilter()" style="flex:1;min-width:120px">
                <option value="">All types</option>
                <option value="model_selection">Model selection</option>
                <option value="parameter_tuning">Parameter tuning</option>
                <option value="task_routing">Task routing</option>
                <option value="quality_threshold">Quality threshold</option>
            </select>
            <select id="djConfFilter" onchange="window._djFilter()" style="flex:1;min-width:120px">
                <option value="">Any confidence</option>
                <option value="0.9">High only</option>
                <option value="0.5">Medium+</option>
            </select>
            <input type="date" id="djDateFilter" onchange="window._djFilter()" style="flex:1;min-width:120px" title="Since date">
        </div>`;

        let listHtml = '<ul class="decision-timeline">';
        if (decisions.length === 0) {
            listHtml += '<li style="padding:1rem;color:var(--text-muted,#7a8491)">No decisions recorded yet.</li>';
        } else {
            decisions.forEach(d => {
                const cc = confidenceClass(d.confidence);
                const cl = confidenceLabel(d.confidence);
                listHtml += `<li class="decision-entry ${cc}" onclick="this.classList.toggle('expanded')" title="Click to expand">
                    <div class="decision-header">
                        <span class="decision-ts">${fmtTs(d.timestamp)}</span>
                        ${pill(d.type || d.decision_type || 'unknown', 'info')}
                        ${pill(cl, d.confidence === 'high' ? 'success' : (d.confidence === 'low' ? 'error' : 'info'))}
                        <strong>${escapeHtml(d.chosen || '')}</strong>
                    </div>
                    <div class="decision-reasoning">${escapeHtml(d.reasoning || 'No reasoning recorded.')}</div>
                </li>`;
            });
        }
        listHtml += '</ul>';

        el.innerHTML = filtersHtml + listHtml;

        // Restore filter values (elements are recreated each render)
        const typeEl = $('djTypeFilter');
        const confEl = $('djConfFilter');
        const dateEl = $('djDateFilter');
        if (typeEl && _journalFilters.type) typeEl.value = _journalFilters.type;
        if (confEl && _journalFilters.confidence_min) confEl.value = _journalFilters.confidence_min;
        if (dateEl && _journalFilters.since_iso) {
            dateEl.value = _journalFilters.since_iso.slice(0, 10);
        }
    } catch (err) {
        el.innerHTML = `<p class="dash-error">Decision journal unavailable: ${escapeHtml(String(err))}</p>`;
    }
}

window._djFilter = function() {
    _journalFilters.type           = ($('djTypeFilter') || {}).value || '';
    _journalFilters.confidence_min = ($('djConfFilter') || {}).value || '';
    const dateVal = ($('djDateFilter') || {}).value || '';
    _journalFilters.since_iso      = dateVal ? new Date(dateVal).toISOString() : '';
    renderDecisionJournalSection();
};

/* ──────────────────────────────────────────────────────────────
   Autonomy panel
   ────────────────────────────────────────────────────────────── */

async function renderAutonomySection() {
    const el = $('autonomySection');
    if (!el) return;
    el.innerHTML = '<p class="dash-loading">Loading autonomy\u2026</p>';
    try {
        const [statusData, promotionsData, rollbackData] = await Promise.all([
            apiFetch('/autonomy/status'),
            apiFetch('/autonomy/promotions/pending'),
            apiFetch('/autonomy/rollback/history'),
        ]);

        // Backend returns {total, summary} where summary = {conservative: N, balanced: N, aggressive: N}.
        // Derive the dominant mode by picking the level with the highest action count.
        const summary = statusData.summary || {};
        const mode = Object.entries(summary).reduce(
            (best, [lvl, cnt]) => (cnt > (summary[best] || 0) ? lvl : best),
            'balanced'
        );
        const modeVal = { conservative: 0, balanced: 50, aggressive: 100 }[mode] || 50;
        // promotionsData.promotions is the correct key (not .pending)
        const promotions = promotionsData.promotions || [];
        const total = statusData.total || 0;

        let html = `<div>
            <label style="font-size:0.85rem;font-weight:600">Dominant Mode: <em>${escapeHtml(mode)}</em></label>
            <input type="range" class="autonomy-mode-slider" min="0" max="100" value="${modeVal}" disabled>
            <div class="autonomy-mode-labels"><span>Conservative</span><span>Aggressive</span></div>
        </div>`;

        // Level breakdown from summary
        const summaryEntries = Object.entries(summary);
        if (summaryEntries.length > 0) {
            html += '<h4 style="margin:0.75rem 0 0.5rem;font-size:0.85rem">Actions by Level</h4>';
            html += '<div class="autonomy-grid">';
            summaryEntries.forEach(([lvl, cnt]) => {
                html += `<div class="autonomy-action-row">
                    <span>${escapeHtml(lvl)}</span>
                    ${pill(String(cnt), 'info')}
                </div>`;
            });
            if (total) html += `<div class="autonomy-action-row" style="font-size:0.8rem;color:var(--text-muted,#7a8491)"><span>Total tracked</span><span>${total}</span></div>`;
            html += '</div>';
        }

        // Pending promotions
        if (promotions.length > 0) {
            html += '<h4 style="margin:0.75rem 0 0.5rem;font-size:0.85rem">Pending Promotions</h4>';
            html += '<ul style="list-style:none;padding:0;margin:0">';
            promotions.forEach(p => {
                html += `<li style="display:flex;justify-content:space-between;align-items:center;padding:0.4rem 0;border-bottom:1px solid var(--border-subtle,rgba(255,255,255,0.04))">
                    <span>${escapeHtml(p.action_type || p.type || 'unknown')}</span>
                    <button class="autonomy-veto-btn" onclick="window._vetoPromotion('${escapeHtml(p.action_type || p.type || '')}', this)">Veto</button>
                </li>`;
            });
            html += '</ul>';
        }

        // Rollback history (last 24h)
        const rollbacks = rollbackData.rollbacks || [];
        if (rollbacks.length > 0) {
            html += '<h4 style="margin:0.75rem 0 0.5rem;font-size:0.85rem">Rollback History (24h)</h4>';
            html += '<ul style="list-style:none;padding:0;margin:0;font-size:0.8rem">';
            rollbacks.slice(0, 10).forEach(r => {
                html += `<li style="padding:0.3rem 0;color:var(--text-muted,#7a8491)">${fmtTs(r.timestamp)} \u2014 ${escapeHtml(r.action_type || r.action_id || 'unknown')} rolled back</li>`;
            });
            html += '</ul>';
        }

        el.innerHTML = html;
    } catch (err) {
        el.innerHTML = `<p class="dash-error">Autonomy data unavailable: ${escapeHtml(String(err))}</p>`;
    }
}

window._vetoPromotion = async function(actionType, btn) {
    btn.disabled = true;
    btn.textContent = 'Vetoing\u2026';
    try {
        const res = await fetch(
            `${API_BASE}/autonomy/promotions/${encodeURIComponent(actionType)}/veto`,
            {
                method: 'POST',
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
            }
        );
        if (!res.ok) throw new Error(`${res.status}`);
        btn.closest('li').remove();
    } catch (err) {
        btn.disabled = false;
        btn.textContent = 'Veto';
        alert(`Veto failed: ${err}`);
    }
};

/* ──────────────────────────────────────────────────────────────
   Welcome back banner
   ────────────────────────────────────────────────────────────── */

async function maybeShowWelcomeBack() {
    const BANNER_KEY = 'vetinari_last_visit';
    const DISMISS_KEY = 'vetinari_wb_dismissed';
    const now = Date.now();
    const lastVisit = parseInt(localStorage.getItem(BANNER_KEY) || '0', 10);
    const lastDismissed = parseInt(localStorage.getItem(DISMISS_KEY) || '0', 10);

    // Update last visit timestamp
    localStorage.setItem(BANNER_KEY, String(now));

    // Show banner only if away >1h and not dismissed in the last hour
    if (!lastVisit || (now - lastVisit) < 3600000) return;
    if (lastDismissed && (now - lastDismissed) < 3600000) return;

    const container = $('welcomeBackContainer');
    if (!container) return;

    try {
        const sinceIso = new Date(lastVisit).toISOString();
        const data = await apiFetch(`/dashboard/welcome-back?since_iso=${encodeURIComponent(sinceIso)}`);

        if (!data.show_summary) return;  // API signals nothing noteworthy to show

        const newModels = data.new_models_discovered || data.new_models || [];
        const learningImprovements = data.learning_improvements_applied || data.learning_improvements || [];
        const items = [];
        if (data.projects_completed > 0) items.push(`${data.projects_completed} project(s) completed`);
        if (data.quality_trend && data.quality_trend !== 'stable') items.push(`Quality trend: ${data.quality_trend}`);
        if (newModels.length > 0) items.push(`New models: ${newModels.join(', ')}`);
        if (learningImprovements.length > 0) items.push(...learningImprovements);
        if (data.needs_attention && data.needs_attention.length > 0) items.push(...data.needs_attention.map(a => `\u26a0 ${a}`));

        if (items.length === 0) return;  // nothing interesting to show

        container.innerHTML = `<div class="welcome-back-banner">
            <div>
                <h3>Welcome back!</h3>
                <ul style="margin:0;padding-left:1.2rem;font-size:0.875rem">
                    ${items.map(i => `<li>${escapeHtml(i)}</li>`).join('')}
                </ul>
            </div>
            <button class="welcome-back-dismiss" onclick="window._dismissWelcomeBack()" title="Dismiss">\u00d7</button>
        </div>`;
    } catch (e) {
        // Banner is non-critical; log so failures are visible in dev tools
        console.error('maybeShowWelcomeBack: welcome-back API unavailable — banner suppressed.', e);
        if (container) {
            container.innerHTML = '<div class="welcome-back-banner welcome-back-degraded">Welcome back — activity summary unavailable</div>';
        }
    }
}

window._dismissWelcomeBack = function() {
    localStorage.setItem('vetinari_wb_dismissed', String(Date.now()));
    const container = $('welcomeBackContainer');
    if (container) container.innerHTML = '';
};

/* ──────────────────────────────────────────────────────────────
   SSE initialisation — called once at boot
   ────────────────────────────────────────────────────────────── */
function initDashboardSSE() {
    // Open a single SSE connection shared by both the pipeline bar and the
    // model-health gauges.  All handlers are registered in one connect() call
    // so that a second call from renderModelHealthSection never tears down
    // the connection that renderPipelineSection already established.
    if (!window.SSEManager) return;
    window.SSEManager.connect(
        'dashboard-pipeline-sse',
        '/api/v1/dashboard/events/stream',
        {
            pipeline_stage: () => { renderPipelineSection(); },
            quality_result: () => { renderModelHealthSection(); },
        }
    );
}

/* ──────────────────────────────────────────────────────────────
   Boot
   ────────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
    initCharts();
    bindEvents();
    refresh();
    scheduleRefresh();
    maybeShowWelcomeBack();
    renderPipelineSection();
    initDashboardSSE();
});
