<script>
  import { getEngineHealth, getEngineMetrics, getEngineVersion } from '$lib/api.js';

  let { backends = [] } = $props();
  const POLL_INTERVAL_MS = 5000;
  // Mirrors the landed ENG-P07 supervisor lifecycle vocabulary at the UI boundary.
  const ENGINE_STATES = {
    RUNNING: { label: 'Running', tone: 'running' },
    STOPPED: { label: 'Stopped', tone: 'stopped' },
    MISSING: { label: 'Not found', tone: 'missing' },
    VERSION_MISMATCH: { label: 'Version mismatch', tone: 'version-mismatch' },
    DEGRADED: { label: 'Degraded', tone: 'degraded' },
  };

  let state = $state('DEGRADED');
  let metrics = $state(null);
  let version = $state(null);
  let unavailableMessage = $state('Engine status unavailable');
  let stateView = $derived(ENGINE_STATES[state] ?? ENGINE_STATES.DEGRADED);

  async function refresh() {
    const [healthResult, metricsResult, versionResult] = await Promise.allSettled([
      getEngineHealth(), getEngineMetrics(), getEngineVersion(),
    ]);
    if (healthResult.status === 'fulfilled') {
      state = String(healthResult.value?.engine_state ?? 'RUNNING').toUpperCase().replaceAll('-', '_');
      unavailableMessage = '';
    } else {
      state = String(healthResult.reason?.body?.engine_state ?? 'DEGRADED').toUpperCase().replaceAll('-', '_');
      unavailableMessage = healthResult.reason?.body?.message ?? 'Engine status unavailable';
    }
    metrics = metricsResult.status === 'fulfilled' ? (metricsResult.value?.metrics ?? metricsResult.value) : null;
    version = versionResult.status === 'fulfilled' ? versionResult.value?.engine_version : null;
  }

  $effect(() => {
    refresh();
    const timer = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  });
</script>

<section class="backend-status" aria-label="Backend status">
  <header class="engine-header">
    <h3>AM Engine</h3>
    <span class="state {stateView.tone}" data-state={state}>{stateView.label}</span>
    {#if version}<span class="version">v{version}</span>{/if}
  </header>
  {#if state === 'RUNNING' && metrics}
    <dl class="metrics" aria-label="Engine metrics">
      <div><dt>Queue</dt><dd>{metrics.queue_depth ?? 'unavailable'}</dd></div>
      <div><dt>Busy slots</dt><dd>{metrics.slots_busy ?? 'unavailable'}</dd></div>
      <div><dt>KV occupancy</dt><dd>{metrics.kv_occupancy_pct != null ? `${metrics.kv_occupancy_pct}%` : 'unavailable'}</dd></div>
      <div><dt>Throughput</dt><dd>{metrics.tok_s != null ? `${metrics.tok_s} tok/s` : 'unavailable'}</dd></div>
    </dl>
  {:else}
    <p class="unavailable" role="status">{unavailableMessage || 'Engine metrics unavailable'}</p>
  {/if}

  <h3>Backends</h3>
  <div class="backend-grid">
    {#each backends as backend}
      <div class="backend-row">
        <span>{backend.provider_type ?? backend.provider}</span>
        <span>{backend.status ?? 'not_installed'}</span>
        <span>{backend.cache_durability ?? 'none'}</span>
      </div>
    {/each}
  </div>
</section>

<style>
  .backend-status { display: grid; gap: 10px; padding: 12px; border: 1px solid var(--border-default); border-radius: 8px; }
  .engine-header { display: flex; align-items: center; gap: 8px; }
  h3 { margin: 0; font-size: 0.9375rem; }
  .state { padding: 2px 8px; border-radius: 999px; font-size: .75rem; font-weight: 600; }
  .running { color: var(--success); background: color-mix(in srgb, var(--success) 12%, transparent); }
  .stopped, .missing { color: var(--text-muted); background: var(--bg-secondary); }
  .version-mismatch, .degraded { color: var(--text-primary); background: color-mix(in srgb, var(--warning) 12%, transparent); }
  .version { color: var(--text-muted); font-size: .8rem; }
  .metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 0; }
  .metrics div { display: flex; justify-content: space-between; gap: 8px; }
  dt { color: var(--text-muted); } dd { margin: 0; font-variant-numeric: tabular-nums; }
  .unavailable { margin: 0; color: var(--text-muted); }
  .backend-grid { display: grid; gap: 4px; }
  .backend-row { display: grid; grid-template-columns: minmax(0, 1fr) auto auto; gap: 12px; min-height: 32px; align-items: center; border-bottom: 1px solid var(--border-default); }
</style>
