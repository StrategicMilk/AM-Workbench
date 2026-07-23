<script>
  import RagQueryPanel from '$components/workbench/RagQueryPanel.svelte';
  import RagRetrievalTrace from '$components/workbench/RagRetrievalTrace.svelte';
  import RagRerankBreakdown from '$components/workbench/RagRerankBreakdown.svelte';
  import RagContextAssembly from '$components/workbench/RagContextAssembly.svelte';
  import RagExperimentBar from '$components/workbench/RagExperimentBar.svelte';
  import RagIngestPanel from '$components/workbench/RagIngestPanel.svelte';
  import { workbenchKernelRequest } from '$lib/api.js';

  let datasets = $state([]);
  let selectedRevision = $state('');
  let loading = $state(false);
  let error = $state('');
  let actionError = $state('');
  let trace = $state(null);
  let rerankBreakdown = $state([]);
  let contextAssembly = $state(null);
  let verdict = $state(null);
  let draftExperiment = $state(null);

  let selectedDataset = $derived(datasets.find((item) => item.revision_id === selectedRevision) ?? null);

  function isSafeRevisionId(value) {
    return typeof value === 'string'
      && value.length > 0
      && value.length <= 160
      && /^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value)
      && !value.includes('..');
  }

  function apiError(status, body) {
    if (status === 404) return `Index missing for revision ${selectedRevision}`;
    if (status === 409) return body?.detail ?? 'Embedding model mismatch: query M1, index M2';
    if (status === 413) return body?.detail ?? 'Query too large: N bytes exceeds limit';
    return body?.detail ?? `RAG API request failed with HTTP ${status}`;
  }

  async function fetchJson(url, options = {}) {
    if (options.method === 'POST') {
      options = {
        ...options,
        headers: {
          ...(options.headers ?? {}),
          'X-Requested-With': 'XMLHttpRequest',
        },
      };
    }
    try {
      return await workbenchKernelRequest(url, options);
    } catch (err) {
      throw new Error(apiError('request', { error: err instanceof Error ? err.message : String(err) }));
    }
  }

  async function loadDatasets() {
    loading = true;
    try {
      datasets = await fetchJson('/api/workbench/rag/datasets');
      selectedRevision = selectedRevision || datasets[0]?.revision_id || '';
      error = '';
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function runReplay(payload) {
    if (!isSafeRevisionId(payload.revision_id)) {
      error = 'Invalid dataset revision id';
      return;
    }
    loading = true;
    try {
      const revisionId = encodeURIComponent(payload.revision_id);
      const body = await fetchJson(`/api/workbench/rag/datasets/${revisionId}/replay`, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload),
      });
      trace = body.trace;
      rerankBreakdown = body.rerank_breakdown ?? [];
      contextAssembly = body.context_assembly;
      verdict = body.verdict;
      draftExperiment = {
        revision_id: payload.revision_id,
        query: payload,
        trace,
        rerank_breakdown: rerankBreakdown,
        context_assembly: contextAssembly,
        verdict,
      };
      error = '';
      actionError = '';
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function saveExperiment() {
    if (!draftExperiment) return;
    actionError = '';
    try {
      draftExperiment = await fetchJson('/api/workbench/rag/experiments', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(draftExperiment),
      });
    } catch (err) {
      actionError = err.message;
    }
  }

  async function promoteExperiment() {
    if (!draftExperiment?.experiment_id) return;
    actionError = '';
    try {
      await fetchJson(`/api/workbench/rag/experiments/${draftExperiment.experiment_id}/promote-to-eval`, {
        method: 'POST',
      });
    } catch (err) {
      actionError = err.message;
    }
  }

  $effect(() => {
    loadDatasets();
  });
</script>

<div class="rag-debugger-view">
  <header class="rag-header">
    <div>
      <h2>RAG Debugger</h2>
      <p>{selectedDataset?.branch ?? selectedRevision ?? 'No dataset revision selected'}</p>
    </div>
    <select bind:value={selectedRevision} aria-label="Dataset revision">
      {#each datasets as dataset}
        <option value={dataset.revision_id}>{dataset.revision_id}</option>
      {/each}
    </select>
  </header>

  {#if error}
    <div class="rag-error" role="alert">{error}</div>
  {/if}

  <div class="rag-layout">
    <aside>
      <RagQueryPanel
        {datasets}
        {selectedRevision}
        disabled={loading}
        onRun={runReplay}
      />
      <RagIngestPanel disabled={loading} />
    </aside>

    <main>
      <RagRetrievalTrace {trace} />
      <RagRerankBreakdown breakdown={rerankBreakdown} />
      <RagContextAssembly context={contextAssembly} {verdict} />
      <RagExperimentBar
        experiment={draftExperiment}
        busy={loading}
        error={actionError}
        onReplay={() => draftExperiment && runReplay(draftExperiment.query)}
        onSave={saveExperiment}
        onPromote={promoteExperiment}
      />
    </main>
  </div>
</div>

<style>
  .rag-debugger-view {
    display: grid;
    gap: 18px;
    padding: 24px;
    max-width: 1440px;
  }

  .rag-header {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(240px, 360px);
    gap: 16px;
    align-items: end;
  }

  h2,
  p {
    margin: 0;
  }

  h2 {
    font-size: 1.25rem;
  }

  .rag-header p,
  .rag-error {
    color: var(--text-secondary);
    font-size: 0.875rem;
  }

  .rag-error {
    color: var(--danger);
    border: 1px solid var(--danger);
    border-radius: 6px;
    padding: 10px 12px;
  }

  .rag-layout {
    display: grid;
    grid-template-columns: minmax(280px, 380px) minmax(0, 1fr);
    gap: 24px;
    align-items: start;
  }

  aside,
  main {
    display: grid;
    gap: 18px;
  }

  main {
    min-width: 0;
  }

  @media (max-width: 980px) {
    .rag-header,
    .rag-layout {
      grid-template-columns: 1fr;
    }
  }
</style>
