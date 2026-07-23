<script>
  import { workbenchKernelRequest } from '$lib/api.js';

  let loading = $state(true);
  let error = $state(null);
  let snapshot = $state(null);

  const API_BASE = '/api/workbench/private-ai';

  let supportRows = $derived(snapshot?.support_matrix ?? []);
  let actions = $derived(snapshot?.recommended_actions ?? []);
  let hardware = $derived(snapshot?.hardware ?? {});
  let runtime = $derived(snapshot?.runtime ?? {});
  let queue = $derived(snapshot?.queue ?? {});
  let modelStore = $derived(snapshot?.model_store ?? {});
  let routing = $derived(snapshot?.routing ?? {});
  let degraded = $derived(snapshot?.degradation_reasons ?? []);

  async function request(path) {
    return workbenchKernelRequest(`${API_BASE}${path}`);
  }

  async function loadSnapshot() {
    loading = true;
    error = null;
    try {
      const body = await request('/snapshot');
      snapshot = body.snapshot ?? body;
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  function valueOrUnknown(value, suffix = '') {
    if (value === null || value === undefined || value === '') return 'unknown';
    return `${value}${suffix}`;
  }

  $effect(() => {
    void loadSnapshot();
  });
</script>

<main class="private-ai-cockpit" aria-label="Private AI Appliance">
  <header class="cockpit-header">
    <div>
      <h1>Private AI Appliance</h1>
      <p>GPU, CPU, RAM, storage, driver, WSL, queue, model, and cloud routing posture.</p>
    </div>
    <button type="button" onclick={loadSnapshot} disabled={loading}>Refresh</button>
  </header>

  {#if loading}
    <section class="loading-state" role="status" aria-live="polite">Loading private appliance cockpit...</section>
  {:else if error}
    <section class="cockpit-alert" role="alert">
      <strong>Action required</strong>
      <span>{error}</span>
    </section>
  {:else if snapshot}
    <section class="status-strip" aria-label="Overall support status">
      <span class="status-pill" data-status={snapshot.overall_status}>{snapshot.overall_status}</span>
      {#each actions as action (action)}
        <span class="action-chip">{action}</span>
      {/each}
    </section>

    {#if degraded.length > 0}
      <section class="cockpit-alert" role="status">
        <strong>Degraded cells</strong>
        <ul>
          {#each degraded as reason (reason)}
            <li>{reason}</li>
          {/each}
        </ul>
      </section>
    {/if}

    <section class="cockpit-grid" aria-label="Runtime cockpit domains">
      <article class="cockpit-panel">
        <h2>Hardware</h2>
        <dl>
          <div><dt>GPU</dt><dd>{valueOrUnknown(hardware.gpu_count)}</dd></div>
          <div><dt>CPU</dt><dd>{valueOrUnknown(hardware.cpu_cores, ' cores')}</dd></div>
          <div><dt>RAM</dt><dd>{valueOrUnknown(hardware.ram_gb, ' GB')}</dd></div>
          <div><dt>storage</dt><dd>{valueOrUnknown(hardware.storage_free_gb, ' GB free')}</dd></div>
          <div><dt>driver</dt><dd>{hardware.driver_status ?? 'unknown'}</dd></div>
          <div><dt>WSL</dt><dd>{hardware.wsl_ready === true ? 'ready' : hardware.wsl_ready === false ? 'blocked' : 'unknown'}</dd></div>
        </dl>
      </article>

      <article class="cockpit-panel">
        <h2>Runtime</h2>
        <dl>
          <div><dt>local runtime</dt><dd>{runtime.runtime_name ?? 'unknown'}</dd></div>
          <div><dt>health</dt><dd>{runtime.health_status ?? 'unknown'}</dd></div>
          <div><dt>detail</dt><dd>{runtime.detail ?? 'not reported'}</dd></div>
          <div><dt>cloud</dt><dd>{routing.cloud_fallback_enabled === true ? 'enabled' : routing.cloud_fallback_enabled === false ? 'disabled' : 'unknown'}</dd></div>
        </dl>
      </article>

      <article class="cockpit-panel">
        <h2>Queue</h2>
        <dl>
          <div><dt>active</dt><dd>{valueOrUnknown(queue.active)}</dd></div>
          <div><dt>queued</dt><dd>{valueOrUnknown(queue.queued)}</dd></div>
          <div><dt>capacity</dt><dd>{valueOrUnknown(queue.capacity)}</dd></div>
          <div><dt>queue</dt><dd>{queue.saturated === true ? 'saturated' : queue.saturated === false ? 'available' : 'unknown'}</dd></div>
        </dl>
      </article>

      <article class="cockpit-panel">
        <h2>Model Store</h2>
        <dl>
          <div><dt>model store</dt><dd>{modelStore.model_store_present === true ? 'present' : modelStore.model_store_present === false ? 'missing' : 'unknown'}</dd></div>
          <div><dt>model count</dt><dd>{valueOrUnknown(modelStore.available_models)}</dd></div>
          <div><dt>loaded model</dt><dd>{modelStore.loaded_model ?? 'none'}</dd></div>
          <div><dt>load state</dt><dd>{modelStore.load_state ?? 'unknown'}</dd></div>
        </dl>
      </article>
    </section>

    <section class="support-matrix" aria-label="Support matrix">
      <h2>Support Matrix</h2>
      <div class="matrix-table">
        {#each supportRows as row (row.row_id)}
          <article class:matched={row.matched} data-status={row.status}>
            <header>
              <span>{row.label}</span>
              <strong>{row.status}</strong>
            </header>
            <p>{row.reason}</p>
            <footer>{row.operator_action}</footer>
          </article>
        {/each}
      </div>
    </section>
  {/if}
</main>

<style>
  .private-ai-cockpit {
    display: grid;
    gap: 16px;
    padding: 16px;
    color: var(--text-primary, #e5e7eb);
  }

  .cockpit-header {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: flex-start;
  }

  h1,
  h2,
  p {
    margin-top: 0;
  }

  h1 {
    margin-bottom: 4px;
    font-size: 1.35rem;
  }

  h2 {
    margin-bottom: 12px;
    font-size: 1rem;
  }

  button {
    border: 1px solid var(--border-color, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: inherit;
    padding: 7px 12px;
  }

  .status-strip,
  .cockpit-grid,
  .matrix-table {
    display: grid;
    gap: 12px;
  }

  .status-strip {
    grid-template-columns: repeat(auto-fit, minmax(180px, max-content));
    align-items: center;
  }

  .cockpit-grid {
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  }

  .cockpit-panel,
  .cockpit-alert,
  .support-matrix article,
  .loading-state {
    border: 1px solid var(--border-color, #334155);
    border-radius: 8px;
    background: var(--surface-panel, #0f172a);
    padding: 14px;
  }

  dl {
    display: grid;
    gap: 8px;
    margin: 0;
  }

  dl div {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }

  dt {
    color: var(--text-muted, #94a3b8);
  }

  dd {
    margin: 0;
    text-align: right;
  }

  .status-pill,
  .action-chip {
    border: 1px solid var(--border-color, #334155);
    border-radius: 999px;
    padding: 6px 10px;
    font-size: 0.8rem;
  }

  .status-pill[data-status="validated"],
  .support-matrix article[data-status="validated"] strong,
  .support-matrix article[data-status="promotion-eligible"] strong {
    color: #86efac;
  }

  .status-pill[data-status="unsupported"],
  .support-matrix article[data-status="unsupported"] strong,
  .support-matrix article[data-status="action-required"] strong {
    color: #fca5a5;
  }

  .support-matrix article[data-status="experimental"] strong,
  .support-matrix article[data-status="degraded"] strong {
    color: #fbbf24;
  }

  .cockpit-alert {
    border-color: #f59e0b;
  }

  .matrix-table {
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  }

  .support-matrix article {
    opacity: 0.72;
  }

  .support-matrix article.matched {
    opacity: 1;
    border-color: #38bdf8;
  }

  .support-matrix header,
  .support-matrix footer {
    display: flex;
    justify-content: space-between;
    gap: 12px;
  }

  .support-matrix footer {
    color: var(--text-muted, #94a3b8);
    font-size: 0.82rem;
  }

  @media (max-width: 760px) {
    .private-ai-cockpit {
      padding: 12px;
    }

    .cockpit-header,
    .support-matrix header,
    .support-matrix footer {
      align-items: flex-start;
      flex-direction: column;
    }

    .status-strip,
    .cockpit-grid,
    .matrix-table {
      grid-template-columns: 1fr;
    }

    dl div {
      flex-direction: column;
      gap: 4px;
    }

    dd {
      overflow-wrap: anywhere;
      text-align: left;
    }
  }
</style>
