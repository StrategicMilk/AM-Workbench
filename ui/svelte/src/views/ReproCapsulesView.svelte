<script>
  import { showToast } from '$lib/stores/toast.svelte.js';
  import { workbenchKernelRequest } from '$lib/api.js';

  let { projectId = new URLSearchParams(window.location.search).get('project_id') ?? 'default' } = $props();

  let capsules = $state([]);
  let selectedRunId = $state(null);
  let selectedExport = $state(null);
  let loading = $state(true);
  let exporting = $state(false);
  let error = $state(null);

  let selectedCapsule = $derived(capsules.find((capsule) => capsule.run_id === selectedRunId) ?? null);
  let sealedCount = $derived(capsules.filter((capsule) => capsule.proof_status === 'sealed').length);
  let redactedCount = $derived(capsules.filter((capsule) => capsule.redactions_applied.length > 0).length);

  async function getJson(path, params = {}) {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== '') query.set(key, value);
    }
    const url = query.toString() ? `${path}?${query.toString()}` : path;
    return workbenchKernelRequest(url);
  }

  function copyCommand(command) {
    navigator.clipboard?.writeText(command)
      .then(() => showToast('Reproduction command copied.', 'success'))
      .catch((err) => showToast(`Copy failed: ${err.message ?? err}`, 'error'));
  }

  async function exportCapsule(runId) {
    exporting = true;
    selectedExport = null;
    try {
      selectedExport = await getJson(`/api/workbench/repro-capsules/${encodeURIComponent(runId)}/export`, {
        project_id: projectId,
      });
    } catch (err) {
      showToast(`Capsule export failed: ${err.message ?? err}`, 'error');
    } finally {
      exporting = false;
    }
  }

  $effect(() => {
    let cancelled = false;
    loading = true;
    error = null;
    getJson('/api/workbench/repro-capsules', { project_id: projectId })
      .then((rows) => {
        if (cancelled) return;
        capsules = rows;
        if (!selectedRunId && rows.length > 0) selectedRunId = rows[0].run_id;
        if (selectedRunId && !rows.some((capsule) => capsule.run_id === selectedRunId)) selectedRunId = rows[0]?.run_id ?? null;
        loading = false;
      })
      .catch((err) => {
        if (cancelled) return;
        error = err.message ?? String(err);
        showToast(`Repro capsule load failed: ${error}`, 'error');
        loading = false;
      });
    return () => { cancelled = true; };
  });
</script>

<div class="repro-capsules">
  <header class="capsule-header">
    <div>
      <h2>Repro Capsules</h2>
      <p>{projectId}</p>
    </div>
    <div class="summary-strip" aria-label="Capsule summary">
      <span>{capsules.length} runs</span>
      <span>{sealedCount} sealed</span>
      <span>{redactedCount} redacted</span>
    </div>
  </header>

  {#if loading}
    <div class="state">Loading sealed capsule proofs.</div>
  {:else if error}
    <div class="state error">{error}</div>
  {:else}
    <div class="capsule-layout">
      <main class="capsule-list" aria-label="Repro capsule list">
        {#each capsules as capsule (capsule.run_id)}
          <article
            class:selected={selectedRunId === capsule.run_id}
            class="capsule-card"
            data-testid={`repro-capsule-${capsule.run_id}`}
          >
            <header>
              <div>
                <span class={`status ${capsule.proof_status}`}>{capsule.proof_status}</span>
                <h3>{capsule.capsule_id}</h3>
              </div>
              <div class="card-actions">
                <button type="button" onclick={() => { selectedRunId = capsule.run_id; selectedExport = null; }}>
                  Select
                </button>
                <button type="button" onclick={() => copyCommand(capsule.reproduction_command)}>
                  Copy
                </button>
              </div>
            </header>

            <dl class="facts">
              <div>
                <dt>run</dt>
                <dd>{capsule.run_id}</dd>
              </div>
              <div>
                <dt>hash</dt>
                <dd>{capsule.manifest_hash_sha256}</dd>
              </div>
              <div>
                <dt>evidence</dt>
                <dd>{capsule.assets} assets / {capsule.traces} traces / {capsule.evals} evals</dd>
              </div>
              <div>
                <dt>redactions</dt>
                <dd>{capsule.redactions_applied.length === 0 ? 'none' : capsule.redactions_applied.join(', ')}</dd>
              </div>
            </dl>

            <code>{capsule.reproduction_command}</code>
          </article>
        {:else}
          <div class="state">No sealed repro capsules are available for this project.</div>
        {/each}
      </main>

      <aside class="detail-panel" aria-label="Repro capsule detail">
        {#if selectedCapsule}
          <h3>{selectedCapsule.capsule_id}</h3>
          <dl class="detail-facts">
            <div>
              <dt>manifest hash</dt>
              <dd>{selectedCapsule.manifest_hash_sha256}</dd>
            </div>
            <div>
              <dt>proof status</dt>
              <dd>{selectedCapsule.proof_status}</dd>
            </div>
            <div>
              <dt>redaction result</dt>
              <dd>{selectedCapsule.redactions_applied.length === 0 ? 'no redactions needed' : selectedCapsule.redactions_applied.join(', ')}</dd>
            </div>
          </dl>
          <div class="actions">
            <button type="button" onclick={() => exportCapsule(selectedCapsule.run_id)} disabled={exporting}>
              {exporting ? 'Exporting' : 'Export'}
            </button>
            <button type="button" onclick={() => copyCommand(selectedCapsule.reproduction_command)}>
              Copy
            </button>
          </div>
          {#if selectedExport}
            <section class="export-preview" aria-label="Redacted export preview">
              <h4>Redacted export</h4>
              <pre>{JSON.stringify(selectedExport.manifest, null, 2)}</pre>
            </section>
          {/if}
        {:else}
          <p>Select a capsule to inspect its sealed proof.</p>
        {/if}
      </aside>
    </div>
  {/if}
</div>

<style>
  .repro-capsules { padding: 18px; max-width: 1500px; display: flex; flex-direction: column; gap: 14px; }
  .capsule-header { display: flex; justify-content: space-between; gap: 14px; align-items: flex-start; flex-wrap: wrap; }
  h2, h3, h4, p, dl { margin: 0; }
  h2 { font-size: 1.25rem; color: var(--text-primary); }
  h3 { font-size: 1rem; color: var(--text-primary); overflow-wrap: anywhere; }
  h4 { font-size: 0.78rem; text-transform: uppercase; color: var(--text-muted); }
  p, dt, dd { color: var(--text-muted); }
  .summary-strip { display: flex; gap: 8px; color: var(--text-muted); font-size: 0.85rem; }
  .capsule-layout { display: grid; grid-template-columns: minmax(420px, 1fr) minmax(300px, 420px); gap: 12px; align-items: start; }
  .capsule-list, .detail-panel { display: grid; gap: 10px; }
  .capsule-card, .detail-panel, .state { border: 1px solid var(--border-default); background: var(--surface-elevated); border-radius: 8px; padding: 12px; }
  .capsule-card { display: grid; gap: 12px; }
  .capsule-card.selected { border-color: var(--accent); }
  .capsule-card header { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
  .card-actions { display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; }
  button { border: 1px solid var(--border-default); background: var(--surface-default); color: var(--text-primary); border-radius: 6px; padding: 6px 10px; }
  button:disabled { opacity: 0.62; cursor: wait; }
  .status { border-radius: 999px; padding: 3px 8px; font-size: 0.75rem; border: 1px solid var(--border-default); text-transform: uppercase; }
  .status.sealed { color: var(--success); }
  .facts, .detail-facts { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .facts div, .detail-facts div { border-top: 1px solid var(--border-default); padding-top: 7px; min-width: 0; }
  dt { font-size: 0.72rem; text-transform: uppercase; }
  dd, code, pre { overflow-wrap: anywhere; }
  code, pre { background: var(--surface-default); border: 1px solid var(--border-default); border-radius: 6px; color: var(--text-primary); }
  code { padding: 7px; display: block; }
  pre { max-height: 480px; overflow: auto; padding: 10px; white-space: pre-wrap; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  .export-preview { display: grid; gap: 8px; }
  .state { padding: 32px; color: var(--text-muted); }
  .error { color: var(--danger); }
  @media (max-width: 1050px) { .capsule-layout { grid-template-columns: 1fr; } }
  @media (max-width: 680px) { .facts, .detail-facts { grid-template-columns: 1fr; } }
</style>
