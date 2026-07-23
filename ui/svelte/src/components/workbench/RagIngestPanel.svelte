<script>
  import Icon from '$lib/a11y/Icon.svelte';
  import { uploadRagDocument } from '$lib/api.js';

  let { disabled = false } = $props();

  let fileInputEl = $state(null);
  let selectedFile = $state(null);
  let uploading = $state(false);
  let result = $state(null);
  let error = $state('');

  function chooseFile() {
    if (!disabled) {
      fileInputEl?.click();
    }
  }

  function handleFileChange(event) {
    selectedFile = event.currentTarget.files?.[0] ?? null;
    result = null;
    error = '';
  }

  async function uploadSelected() {
    if (!selectedFile || uploading) return;
    uploading = true;
    error = '';
    result = null;
    try {
      result = await uploadRagDocument(selectedFile);
    } catch (err) {
      error = err.message ?? String(err);
    } finally {
      uploading = false;
    }
  }
</script>

<section class="rag-ingest-panel" data-testid="fsa0052-rag-ingest-panel" aria-label="RAG document ingestion">
  <input
    bind:this={fileInputEl}
    type="file"
    accept=".txt,.md,.json,.csv,.pdf,application/pdf,text/*,application/octet-stream"
    hidden
    onchange={handleFileChange}
    aria-hidden="true"
  />

  <div class="ingest-row">
    <button type="button" onclick={chooseFile} disabled={disabled || uploading}>
      <Icon name="file-arrow-up" />
      Select
    </button>
    <span class="file-name">{selectedFile?.name ?? 'No document selected'}</span>
    <button type="button" class="primary" onclick={uploadSelected} disabled={disabled || uploading || !selectedFile}>
      <Icon name={uploading ? 'spinner' : 'upload'} class={uploading ? 'fa-spin' : ''} />
      Upload
    </button>
  </div>

  {#if result}
    <div class="ingest-result" role="status">
      <strong>{result.ingested_chunks ?? 0}</strong>
      <span>{result.source}</span>
    </div>
  {/if}

  {#if error}
    <div class="ingest-error" role="alert">{error}</div>
  {/if}
</section>

<style>
  .rag-ingest-panel {
    display: grid;
    gap: 10px;
    border: 1px solid var(--border-default);
    border-radius: 8px;
    background: var(--surface-elevated);
    padding: 12px;
  }

  .ingest-row {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    gap: 8px;
    align-items: center;
  }

  button {
    min-height: 36px;
    display: inline-flex;
    align-items: center;
    gap: 6px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-bg);
    color: var(--text-primary);
    font: inherit;
    padding: 7px 10px;
  }

  button.primary {
    border-color: var(--primary);
    background: var(--primary);
    color: var(--text-on-primary);
  }

  .file-name {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-secondary);
  }

  .ingest-result,
  .ingest-error {
    display: flex;
    gap: 8px;
    align-items: center;
    font-size: 0.82rem;
  }

  .ingest-result {
    color: var(--success);
  }

  .ingest-error {
    color: var(--danger);
  }

  @media (max-width: 720px) {
    .ingest-row {
      grid-template-columns: 1fr;
    }
  }
</style>
