<script>
  import Icon from '$lib/a11y/Icon.svelte';
  import { pullModelFromHub, searchModelHub } from '$lib/api.js';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let query = $state('llama');
  let results = $state([]);
  let searching = $state(false);
  let pulling = $state('');
  let error = $state('');
  let lastPull = $state(null);

  async function searchHub() {
    if (!query.trim() || searching) return;
    searching = true;
    error = '';
    try {
      const payload = await searchModelHub(query.trim());
      results = payload.results ?? [];
    } catch (err) {
      error = err.message ?? String(err);
    } finally {
      searching = false;
    }
  }

  function evidenceRefsForModel(model) {
    return [
      ...(Array.isArray(model?.evidence_refs) ? model.evidence_refs : []),
      ...(Array.isArray(model?.provenance_refs) ? model.provenance_refs : []),
      model?.source_ref,
      model?.source_url,
      model?.model_card_ref,
      model?.digest,
      model?.sha256,
    ].filter(Boolean);
  }

  function modelEvidenceIssue(model) {
    const repoId = model?.repo_id ?? model?.id ?? model?.name ?? 'unknown';
    const refs = evidenceRefsForModel(model);
    if (refs.length === 0) {
      return 'missing_model_hub_evidence';
    }
    try {
      requireEvidence(refs, `model-hub:${repoId}`);
      return '';
    } catch (err) {
      return err.message ?? String(err);
    }
  }

  async function pullModel(model) {
    const repoId = model.repo_id ?? model.id ?? model.name;
    if (!repoId || pulling) return;
    const evidenceIssue = modelEvidenceIssue(model);
    if (evidenceIssue) {
      error = evidenceIssue;
      return;
    }
    pulling = repoId;
    error = '';
    try {
      lastPull = await pullModelFromHub({ repo_id: repoId, filename: model.filename, model_format: model.format });
    } catch (err) {
      error = err.message ?? String(err);
    } finally {
      pulling = '';
    }
  }
</script>

<section class="model-hub-browser" data-testid="fsa0053-model-hub-browser" aria-label="Model hub browser">
  <form class="hub-search" onsubmit={(event) => { event.preventDefault(); void searchHub(); }}>
    <input bind:value={query} aria-label="Model hub search" />
    <button type="submit" disabled={searching || !query.trim()} aria-label="Search model hub" title="Search model hub">
      <Icon name={searching ? 'spinner' : 'search'} class={searching ? 'fa-spin' : ''} />
    </button>
  </form>

  {#if error}
    <div class="hub-error" role="alert">{error}</div>
  {/if}

  {#if lastPull}
    <div class="hub-status" role="status">{lastPull.status ?? 'started'}: {lastPull.repo_id ?? lastPull.download_id}</div>
  {/if}

  <div class="hub-results">
    {#each results as model (model.id ?? model.repo_id ?? model.name)}
      {@const repoId = model.repo_id ?? model.id ?? model.name}
      {@const evidenceIssue = modelEvidenceIssue(model)}
      <article class="hub-result">
        <div>
          <strong>{model.name ?? repoId}</strong>
          <span>{model.source_type ?? model.source ?? 'hub'}</span>
          {#if evidenceIssue}
            <span class="evidence-warning">{evidenceIssue}</span>
          {/if}
        </div>
        <button
          type="button"
          onclick={() => pullModel(model)}
          disabled={pulling === repoId || Boolean(evidenceIssue)}
          aria-label="Pull model {repoId}"
          title="Pull model"
        >
          <Icon name={pulling === repoId ? 'spinner' : 'download'} class={pulling === repoId ? 'fa-spin' : ''} />
        </button>
      </article>
    {/each}
  </div>
</section>

<style>
  .model-hub-browser {
    display: grid;
    gap: 10px;
    border: 1px solid var(--border-default);
    border-radius: 8px;
    background: var(--surface-elevated);
    padding: 12px;
  }

  .hub-search {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 38px;
    gap: 8px;
  }

  input,
  button {
    min-height: 36px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-bg);
    color: var(--text-primary);
    font: inherit;
  }

  input {
    min-width: 0;
    padding: 7px 10px;
  }

  button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }

  .hub-results {
    display: grid;
    gap: 8px;
  }

  .hub-result {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 38px;
    gap: 8px;
    align-items: center;
    border: 1px solid var(--border-subtle, var(--border-default));
    border-radius: 6px;
    padding: 8px;
  }

  .hub-result div {
    display: grid;
    gap: 2px;
    min-width: 0;
  }

  .hub-result strong,
  .hub-result span {
    overflow-wrap: anywhere;
  }

  .hub-result span,
  .hub-status {
    color: var(--text-muted);
    font-size: 0.78rem;
  }

  .hub-result .evidence-warning {
    color: var(--danger);
  }

  .hub-error {
    color: var(--danger);
    font-size: 0.82rem;
  }
</style>
