<script>
  let { machineProfile = {}, runtimeAppliance = {}, degradationReasons = [] } = $props();

  let normalizedProfile = $derived(
    machineProfile && Object.keys(machineProfile).length > 0
      ? machineProfile
      : runtimeAppliance.machine_profile ?? runtimeAppliance.machine ?? {}
  );

  function numericValue(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function valueOrUnknown(value, suffix = '') {
    if (value === null || value === undefined || value === '') return 'unknown';
    return `${value}${suffix}`;
  }

  function metricValue(key, suffix = '') {
    const value = numericValue(normalizedProfile[key]);
    return value === null ? 'unknown' : `${value}${suffix}`;
  }

  let hasCompatibleMetrics = $derived(
    ['available_vram_gb', 'total_vram_gb', 'available_ram_gb', 'total_ram_gb', 'available_cpu_threads', 'cpu_threads'].some(
      (key) => numericValue(normalizedProfile[key]) !== null
    )
  );

  let failureReasons = $derived([
    ...(Array.isArray(degradationReasons) ? degradationReasons : []),
    ...(hasCompatibleMetrics ? [] : ['machine metrics unavailable']),
  ]);

  let queueLabel = $derived(
    normalizedProfile.queue_depth !== undefined && normalizedProfile.queue_capacity !== undefined
      ? `${normalizedProfile.queue_depth}/${normalizedProfile.queue_capacity}`
      : 'unknown'
  );

  let runtimeStatus = $derived(
    normalizedProfile.runtime_status ?? runtimeAppliance.runtime?.health_status ?? runtimeAppliance.health_status ?? 'unknown'
  );
</script>

<section class="machine-panel" aria-label="Machine resource state" data-compatible-metrics={hasCompatibleMetrics}>
  <header>
    <h2>Machine</h2>
    {#if failureReasons.length > 0}
      <span class="status warning">degraded</span>
    {:else}
      <span class="status ready">ready</span>
    {/if}
  </header>

  <dl class="metric-grid">
    <div>
      <dt>GPU VRAM</dt>
      <dd>{metricValue('available_vram_gb', ' GB')} / {metricValue('total_vram_gb', ' GB')}</dd>
    </div>
    <div>
      <dt>System RAM</dt>
      <dd>{metricValue('available_ram_gb', ' GB')} / {metricValue('total_ram_gb', ' GB')}</dd>
    </div>
    <div>
      <dt>CPU Threads</dt>
      <dd>{metricValue('available_cpu_threads')} / {metricValue('cpu_threads')}</dd>
    </div>
    <div>
      <dt>SSD Free</dt>
      <dd>{metricValue('storage_free_gb', ' GB')}</dd>
    </div>
    <div>
      <dt>Queue</dt>
      <dd>{queueLabel}</dd>
    </div>
    <div>
      <dt>Runtime</dt>
      <dd>{runtimeStatus}</dd>
    </div>
    <div>
      <dt>Model Store</dt>
      <dd>{normalizedProfile.model_store_status ?? runtimeAppliance.model_store?.load_state ?? 'unknown'}</dd>
    </div>
    <div>
      <dt>Cloud Fallback</dt>
      <dd>{normalizedProfile.cloud_fallback_enabled === true ? 'enabled' : normalizedProfile.cloud_fallback_enabled === false ? 'disabled' : 'unknown'}</dd>
    </div>
  </dl>

  {#if failureReasons.length > 0}
    <ul class="degradation-list" aria-label="Machine degradation reasons">
      {#each failureReasons as reason}
        <li>{valueOrUnknown(reason)}</li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .machine-panel {
    display: grid;
    gap: 12px;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  h2 {
    margin: 0;
    font-size: 16px;
    letter-spacing: 0;
  }

  .metric-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 10px;
    margin: 0;
  }

  .metric-grid div {
    min-height: 70px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 10px;
  }

  dt {
    margin-bottom: 6px;
    color: var(--text-muted, #94a3b8);
    font-size: 12px;
  }

  dd {
    margin: 0;
    font-size: 15px;
  }

  .status {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 5px 8px;
    font-size: 12px;
  }

  .ready {
    color: #86efac;
  }

  .warning {
    color: #fbbf24;
  }

  .degradation-list {
    display: grid;
    gap: 4px;
    margin: 0;
    padding-left: 18px;
    color: #fbbf24;
    font-size: 12px;
  }
</style>
