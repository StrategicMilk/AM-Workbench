<script>
  let { observations = [] } = $props();

  const categoryOrder = [
    'cpu',
    'ram',
    'disk',
    'gpu_vram',
    'model_load',
    'embedding_vector_search',
    'windows_wsl_path',
    'service_residency',
    'thermal_power',
    'runtime_version'
  ];

  let safeObservations = $derived(Array.isArray(observations) ? observations.filter(Boolean) : []);
  let rows = $derived(
    categoryOrder.map((kind) => safeObservations.find((item) => item.kind === kind) ?? {
      kind,
      status: 'unavailable',
      value: null,
      unit: 'missing',
      evidence_id: 'missing'
    })
  );
  let readyCount = $derived(rows.filter((row) => row.status === 'ready').length);

  function metricValue(row) {
    if (row.value === null || row.value === undefined || Number.isNaN(row.value)) return row.status;
    return `${row.value} ${row.unit ?? ''}`.trim();
  }
</script>

<section class="benchmark-matrix" aria-labelledby="hardware-benchmark-title">
  <header>
    <h3 id="hardware-benchmark-title">Benchmarks</h3>
    <span role="status" aria-label={`${readyCount} of ${rows.length} benchmarks ready`}>{readyCount}/{rows.length}</span>
  </header>

  <div class="matrix" role="table" aria-label="Measured benchmark categories" aria-rowcount={rows.length + 1}>
    <div class="row header-row" role="row">
      <div role="columnheader">Category</div>
      <div role="columnheader">Status</div>
      <div role="columnheader">Value</div>
      <div role="columnheader">Evidence</div>
    </div>
    {#each rows as row}
      <div class="row" role="row">
        <div class="kind" role="rowheader">{row.kind.replaceAll('_', ' ')}</div>
        <div class:ready={row.status === 'ready'} class:degraded={row.status !== 'ready'} role="cell">
          {row.status}
        </div>
        <div role="cell">{metricValue(row)}</div>
        <div class="evidence" role="cell">{row.evidence_id}</div>
      </div>
    {/each}
  </div>
</section>

<style>
  .benchmark-matrix {
    display: grid;
    gap: 10px;
  }

  header,
  .row {
    display: grid;
    grid-template-columns: minmax(150px, 1.2fr) minmax(92px, 0.7fr) minmax(120px, 0.8fr) minmax(160px, 1fr);
    gap: 8px;
    align-items: center;
  }

  header {
    grid-template-columns: 1fr auto;
  }

  h3 {
    margin: 0;
    font-size: 14px;
    letter-spacing: 0;
  }

  .matrix {
    display: grid;
    gap: 6px;
  }

  .row {
    min-height: 38px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    padding: 8px;
    background: var(--surface-elevated, #111827);
    font-size: 12px;
  }

  .header-row {
    min-height: 30px;
    background: transparent;
    color: var(--text-muted, #94a3b8);
    font-weight: 700;
  }

  .kind {
    text-transform: capitalize;
  }

  .ready {
    color: #86efac;
  }

  .degraded {
    color: #fbbf24;
  }

  .evidence {
    color: var(--text-muted, #94a3b8);
    overflow-wrap: anywhere;
  }

  @media (max-width: 720px) {
    .row {
      grid-template-columns: 1fr;
    }
  }
</style>
