<script>
  /**
   * Dashboard view — system overview with KPI metrics, hardware monitoring,
   * timeseries charts, and activity feed.
   *
   * Fetches from /api/v1/analytics/overview and /health, auto-refreshes
   * on a configurable interval with proper cleanup.
   */
  import KpiCard from '$components/dashboard/KpiCard.svelte';
  import HardwareMonitor from '$components/dashboard/HardwareMonitor.svelte';
  import ActivityFeed from '$components/dashboard/ActivityFeed.svelte';
  import MetricsChart from '$components/dashboard/MetricsChart.svelte';
  import AttentionTrack from '$components/dashboard/AttentionTrack.svelte';
  import * as api from '$lib/api.js';
  import * as fmt from '$lib/utils/format.js';
  import { showToast } from '$lib/stores/toast.svelte.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import Icon from '$lib/a11y/Icon.svelte';

  /** Dashboard data from API. */
  let overview = $state(null);
  let health = $state(null);
  let loading = $state(true);
  let error = $state(null);

  /** Auto-refresh interval in seconds. */
  let refreshInterval = $state(30);
  let autoRefresh = $state(true);

  /** KPI cards derived from overview data. */
  let kpis = $derived(buildKpis(overview, health));

  /** Chart data derived from overview timeseries. */
  let latencyChartData = $derived(buildLatencyChart(overview));
  let tokenChartData = $derived(buildTokenChart(overview));

  function buildKpis(ov, h) {
    if (!ov) return [];
    return [
      {
        icon: 'fas fa-tasks',
        value: fmt.integer(ov.active_tasks ?? 0),
        label: 'Active Tasks',
        color: 'primary',
      },
      {
        icon: 'fas fa-microchip',
        value: fmt.integer(ov.models_loaded ?? 0),
        label: 'Models Loaded',
        color: 'secondary',
      },
      {
        icon: 'fas fa-coins',
        value: fmt.currency(ov.session_cost ?? 0),
        label: 'Session Cost',
        color: 'warning',
      },
      {
        icon: 'fas fa-clock',
        value: fmt.latency(ov.avg_latency_ms ?? 0),
        label: 'Avg Latency',
        color: 'success',
      },
      {
        icon: 'fas fa-check-circle',
        value: fmt.percent(ov.success_rate ?? 0),
        label: 'Success Rate',
        color: 'success',
        trend: ov.success_rate_trend ?? null,
      },
      {
        icon: 'fas fa-brain',
        value: fmt.integer(ov.memory_entries ?? 0),
        label: 'Memory Entries',
        color: 'primary',
      },
    ];
  }

  function buildLatencyChart(ov) {
    if (!ov?.latency_history) return null;
    const history = ov.latency_history;
    return {
      labels: history.map((p) => p.label ?? ''),
      datasets: [{
        label: 'Latency (ms)',
        data: history.map((p) => p.value),
        borderColor: 'rgba(78, 154, 249, 0.8)',
        backgroundColor: 'rgba(78, 154, 249, 0.1)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
      }],
    };
  }

  function buildTokenChart(ov) {
    if (!ov?.token_history) return null;
    const history = ov.token_history;
    return {
      labels: history.map((p) => p.label ?? ''),
      datasets: [{
        label: 'Tokens',
        data: history.map((p) => p.value),
        borderColor: 'rgba(33, 212, 253, 0.8)',
        backgroundColor: 'rgba(33, 212, 253, 0.1)',
        fill: true,
        tension: 0.3,
        pointRadius: 0,
      }],
    };
  }

  /**
   * Fetch dashboard data from both endpoints.
   *
   * Returns true when at least one request succeeded so callers can decide
   * whether to show a "refreshed" success toast or a degraded-state banner.
   * Both requests use individual `.catch(() => null)` so a single endpoint
   * failure does not prevent the other from populating.
   *
   * @returns {Promise<boolean>} true if at least one fetch succeeded.
   */
  async function fetchDashboard() {
    try {
      const [ov, h] = await Promise.all([
        api.getAnalyticsOverview().catch(() => null),
        api.getHealth().catch(() => null),
      ]);
      const anySucceeded = ov !== null || h !== null;
      overview = ov;
      health = h;
      if (anySucceeded) {
        error = null;
      } else {
        // Both endpoints failed — surface a degraded state instead of a blank
        // "successful" view with no data. Do not clear any existing error.
        error = error ?? 'All dashboard endpoints unreachable — backend may be down.';
      }
      return anySucceeded;
    } catch (err) {
      error = err.message;
      return false;
    } finally {
      loading = false;
    }
  }

  async function manualRefresh() {
    loading = true;
    const anySucceeded = await fetchDashboard();
    // Only celebrate a successful refresh — suppress the toast when all
    // requests failed so the user gets the error banner, not a false positive.
    if (anySucceeded) {
      showToast('Dashboard refreshed', 'success');
    }
  }

  // Auto-refresh effect with proper cleanup
  $effect(() => {
    fetchDashboard();

    if (!autoRefresh) return;

    const interval = setInterval(fetchDashboard, refreshInterval * 1000);
    return () => clearInterval(interval);
  });
</script>

<div class="dashboard-view">
  <div class="dashboard-header">
    <h2>
      <Icon name="chart-line" />
      Dashboard
    </h2>
    <HelpPopover
      title="Dashboard"
      body="Live overview of your Workbench project. Panels show active agents, recent runs, resource utilisation, and the current pipeline state. Data refreshes automatically on the configured interval; you can also trigger a manual refresh. Run status colours: blue = running, green = succeeded, yellow = blocked or approval-required, red = recovery needed. Memory and training summaries reflect the project's persistent stores and do not include in-flight agent context."
      severity="info"
    />
    <div class="dashboard-controls">
      <label class="auto-refresh-toggle">
        <input
          type="checkbox"
          bind:checked={autoRefresh}
          aria-label="Auto-refresh"
        />
        <span>Auto-refresh</span>
      </label>
      <select
        bind:value={refreshInterval}
        class="input"
        aria-label="Refresh interval"
      >
        <option value={10}>10s</option>
        <option value={30}>30s</option>
        <option value={60}>60s</option>
        <option value={120}>2m</option>
      </select>
      <button class="btn btn-secondary" onclick={manualRefresh} aria-label="Refresh dashboard now" title="Refresh now">
        <Icon name="sync-alt" class={loading ? 'fa-spin' : ''} />
      </button>
    </div>
  </div>

  {#if error}
    <div class="dashboard-error" role="alert">
      <Icon name="exclamation-triangle" />
      <span>Failed to load dashboard: {error}</span>
      <button class="btn btn-ghost" onclick={manualRefresh}>Retry</button>
    </div>
  {/if}

  <!-- Attention track — projects awaiting user input -->
  <AttentionTrack />

  <!-- KPI Grid -->
  <div class="kpi-grid" role="status" aria-live="polite">
    {#each kpis as kpi}
      <KpiCard {...kpi} />
    {/each}
  </div>

  <!-- Charts + Hardware row -->
  <div class="dashboard-row">
    <div class="dashboard-col-2">
      {#if latencyChartData}
        <MetricsChart title="Latency Trend" data={latencyChartData} />
      {/if}
      {#if tokenChartData}
        <MetricsChart title="Token Usage" data={tokenChartData} />
      {/if}
    </div>

    <div class="dashboard-col-1">
      <HardwareMonitor hardware={overview?.hardware ?? {}} />
      <ActivityFeed events={overview?.recent_events ?? []} />
    </div>
  </div>
</div>

<style>
  .dashboard-view {
    padding: 24px;
    max-width: 1400px;
  }

  .dashboard-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 24px;
    flex-wrap: wrap;
    gap: 12px;
  }

  .dashboard-header h2 {
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 0;
  }

  .dashboard-header h2 i {
    color: var(--primary);
  }

  .dashboard-controls {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .auto-refresh-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 0.8125rem;
    color: var(--text-secondary);
    cursor: pointer;
  }

  .dashboard-controls select {
    width: auto;
    min-height: 44px;
    padding: 6px 10px;
    font-size: 0.8125rem;
  }

  .dashboard-controls button,
  .dashboard-controls input[type='checkbox'] {
    min-width: 44px;
    min-height: 44px;
  }

  .dashboard-error {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 16px;
    background: rgba(240, 98, 98, 0.08);
    border: 1px solid var(--danger);
    border-radius: 8px;
    color: var(--danger);
    font-size: 0.875rem;
    margin-bottom: 24px;
  }

  .kpi-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }

  .dashboard-row {
    display: grid;
    grid-template-columns: 2fr 1fr;
    gap: 16px;
  }

  .dashboard-col-2 {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .dashboard-col-1 {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  @media (max-width: 1024px) {
    .dashboard-row {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 768px) {
    .kpi-grid {
      grid-template-columns: 1fr 1fr;
    }

    .dashboard-header {
      flex-direction: column;
      align-items: flex-start;
    }
  }
</style>
