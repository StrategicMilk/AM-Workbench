<script>
  /**
   * Model download panel with file variant picker and SSE progress.
   *
   * Fetches available files from the repo, lets the user pick a quant/format,
   * shows hardware fit recommendations, then streams download progress via SSE.
   *
   * @prop {object|null} model - The model to download (from catalog/search).
   * @prop {() => void} [onclose] - Called when download panel is closed.
   */
  import * as api from '$lib/api.js';
  import { fileSize, percent } from '$lib/utils/format.js';
  import { showToast } from '$lib/stores/toast.svelte.js';

  let { model = null, onclose } = $props();

  // File picker state
  let files = $state([]);
  let formats = $state({});
  let recommended = $state('');
  let selectedFile = $state('');
  let recReason = $state('');
  let loadingFiles = $state(false);
  let filesError = $state(null);

  // Download state
  let downloading = $state(false);
  let progress = $state(0);
  let downloadedBytes = $state(0);
  let totalBytes = $state(0);
  let etaSeconds = $state(0);
  let error = $state(null);
  let done = $state(false);
  let queued = $state(false);
  let lastModelKey = $state('');

  // Group files by format for display
  let groupedFiles = $derived.by(() => {
    const groups = {};
    for (const f of files) {
      const key = f.format || 'other';
      if (!groups[key]) groups[key] = [];
      groups[key].push(f);
    }
    return groups;
  });

  // Is safetensors model (multiple shards, download all)
  let isSafetensors = $derived(
    formats.safetensors > 0 && !formats.gguf
  );

  // Total size for safetensors (sum of all shards)
  let safetensorsTotal = $derived.by(() => {
    if (!isSafetensors) return 0;
    return files
      .filter((f) => f.format === 'safetensors')
      .reduce((sum, f) => sum + (f.size_gb || 0), 0);
  });

  // Fetch file variants when model changes
  $effect(() => {
    const modelKey = model ? (model.repo_id || model.id || model.name || '') : '';
    if (modelKey !== lastModelKey) {
      lastModelKey = modelKey;
      downloading = false;
      progress = 0;
      downloadedBytes = 0;
      totalBytes = 0;
      etaSeconds = 0;
      error = null;
      done = false;
      queued = false;
    }

    if (model) {
      const repoId = model.repo_id || model.id;
      if (repoId && !model._filesLoaded) {
        loadFiles(repoId);
      } else if (model.files?.length) {
        // Curated models already have files from popular endpoint
        files = model.files.map((f) => ({ ...f, format: detectFormat(f.filename) }));
        recommended = '';
        selectedFile = '';
      }
    }
  });

  function detectFormat(filename) {
    const lower = (filename || '').toLowerCase();
    if (lower.endsWith('.safetensors')) return 'safetensors';
    if (lower.endsWith('.gguf')) return 'gguf';
    if (lower.endsWith('.bin')) return 'bin';
    if (lower.endsWith('.pt')) return 'pt';
    return 'other';
  }

  function isSafeRepoId(repoId) {
    return typeof repoId === 'string'
      && /^[A-Za-z0-9][A-Za-z0-9._/-]{0,191}$/.test(repoId)
      && !repoId.includes('..')
      && !repoId.startsWith('/')
      && !repoId.endsWith('/');
  }

  function isSafeModelFilename(filename) {
    return typeof filename === 'string'
      && filename.length > 0
      && filename.length <= 240
      && /^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(filename)
      && !filename.includes('..')
      && !filename.startsWith('/')
      && !filename.endsWith('/');
  }

  async function loadFiles(repoId) {
    if (!isSafeRepoId(repoId)) {
      filesError = 'Invalid model repository id';
      files = [];
      return;
    }
    loadingFiles = true;
    filesError = null;
    // Infer primary use-case from model metadata
    const uses = model?.recommended_for ?? model?.capabilities ?? [];
    const useCase = uses[0] || 'general';
    try {
      const data = await api.getModelFiles(repoId, { useCase });
      files = data.files || [];
      formats = data.formats || {};
      recommended = data.recommended || '';
      recReason = data.recommended_reason || '';
      selectedFile = recommended || '';
    } catch (err) {
      filesError = err.message;
      files = [];
    } finally {
      loadingFiles = false;
    }
  }

  async function startDownload() {
    if (!model) return;

    // Reset all prior download state so a new attempt never inherits stale
    // progress, completion, or error from the previous download.
    downloading = true;
    done = false;
    error = null;
    progress = 0;
    downloadedBytes = 0;
    totalBytes = 0;
    etaSeconds = 0;

    const repoId = model.repo_id || model.id;
    if (!isSafeRepoId(repoId)) {
      error = 'Invalid model repository id';
      downloading = false;
      return;
    }
    // For safetensors, use the first shard's filename — the backend requires a
    // non-empty filename and returns 400 otherwise (POST /api/v1/models/download).
    // For GGUF, use the selected file.
    const filename = isSafetensors
      ? (files.find((f) => f.format === 'safetensors')?.filename ?? '')
      : (selectedFile || '');
    if (!isSafeModelFilename(filename)) {
      error = 'Invalid model file name';
      downloading = false;
      return;
    }

    try {
      // The backend POST /api/v1/models/download returns { status, repo_id, filename }.
      // There is no SSE stream endpoint — the download runs in the background
      // on the server side.  Treat the acknowledgment as completion from the
      // frontend's perspective.
      const result = await api.downloadModel({ repo_id: repoId, filename });

      if (result?.status === 'started' || result?.status === 'acknowledged') {
        queued = true;
        done = false;
        progress = 0;
        downloading = false;
        showToast(`${model.name ?? model.id} download running in background`, 'success');
      } else {
        error = 'Unexpected response from server';
        downloading = false;
      }
    } catch (err) {
      error = err.message;
      downloading = false;
    }
  }


</script>

{#if model}
  <div class="download-panel">
    <div class="download-header">
      <h3>Download Model</h3>
      {#if onclose}
        <button class="btn btn-ghost" onclick={onclose} aria-label="Close">
          <i class="fas fa-times"></i>
        </button>
      {/if}
    </div>

    <div class="download-name">{model.name ?? model.id}</div>

    {#if loadingFiles}
      <div class="loading-files">
        <i class="fas fa-spinner fa-spin"></i>
        Fetching available formats...
      </div>
    {:else if filesError}
      <div class="download-error" role="alert">
        <i class="fas fa-exclamation-triangle"></i>
        {filesError}
      </div>
    {:else if files.length > 0}
      <!-- Format/quant picker -->
      <div class="variant-picker">
        {#if isSafetensors}
          <div class="variant-info">
            <span class="variant-label">safetensors</span>
            <span class="variant-size">{safetensorsTotal.toFixed(1)} GB total ({files.filter(f => f.format === 'safetensors').length} shards)</span>
            {#if recommended === 'all_safetensors'}
              <span class="variant-rec">Fits your GPU</span>
            {:else}
              <span class="variant-warn">May need CPU offload</span>
            {/if}
          </div>
        {:else if groupedFiles.gguf?.length}
          <label class="picker-label" for="quant-select">Quantization</label>
          <select id="quant-select" class="picker-select" bind:value={selectedFile}>
            {#each groupedFiles.gguf as f}
              <option value={f.filename}>
                {f.quant || f.filename.split('/').pop()} — {f.size_gb} GB
                {f.fits ? '✓ fits' : '⚠ too large'}
                {f.filename === recommended ? ' ★ recommended' : ''}
              </option>
            {/each}
          </select>
          {#if recReason}
            <div class="variant-rec">{recReason}</div>
          {/if}
        {:else}
          <div class="variant-info">
            <span class="variant-label">{Object.keys(formats).join(', ')}</span>
            <span class="variant-size">{files.length} file(s)</span>
          </div>
        {/if}
      </div>
    {/if}

    {#if error}
      <div class="download-error" role="alert">
        <i class="fas fa-exclamation-triangle"></i>
        {error}
      </div>
    {/if}

    {#if downloading || done}
      <div class="download-progress">
        <div class="progress-bar" role="progressbar"
          aria-valuenow={progress} aria-valuemin={0} aria-valuemax={100}
          aria-label="Download progress">
          <div class="progress-fill" class:complete={done} style="width: {progress}%"></div>
        </div>
        <div class="progress-info">
          <span>{percent(progress, 0)}</span>
          {#if totalBytes > 0}
            <span>{fileSize(downloadedBytes)} / {fileSize(totalBytes)}</span>
          {/if}
          {#if etaSeconds > 0 && !done}
            <span class="eta">~{Math.ceil(etaSeconds)}s</span>
          {/if}
        </div>
      </div>
    {/if}

    {#if !downloading && !done && !queued && !loadingFiles}
      <button class="btn btn-primary" onclick={startDownload} disabled={!files.length}>
        <i class="fas fa-download"></i>
        Download
      </button>
    {/if}

    {#if done}
      <div class="download-done">
        <i class="fas fa-check-circle"></i>
        Download complete
      </div>
    {/if}

    {#if queued && !done}
      <div class="download-queued">
        <i class="fas fa-clock"></i>
        Download running in background
      </div>
    {/if}
  </div>
{/if}

<style>
  .download-panel {
    padding: 20px;
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: 12px;
  }

  .download-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
  }

  .download-header h3 {
    margin: 0;
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-primary);
  }

  .download-name {
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--text-primary);
    margin-bottom: 12px;
  }

  .loading-files {
    font-size: 0.8125rem;
    color: var(--text-muted);
    padding: 12px 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .variant-picker {
    margin-bottom: 14px;
  }

  .picker-label {
    display: block;
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--text-muted);
    margin-bottom: 6px;
  }

  .picker-select {
    width: 100%;
    padding: 8px 10px;
    background: rgba(255, 255, 255, 0.04);
    border: 1px solid var(--border-default);
    border-radius: 6px;
    color: var(--text-primary);
    font-size: 0.8125rem;
    font-family: inherit;
    cursor: pointer;
  }

  .picker-select:focus {
    outline: 2px solid transparent;
    outline-offset: 2px;
    border-color: var(--primary);
  }

  .picker-select:focus-visible {
    outline-color: var(--primary);
  }

  .variant-info {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    font-size: 0.8125rem;
  }

  .variant-label {
    font-weight: 500;
    color: var(--text-primary);
  }

  .variant-size {
    color: var(--text-muted);
  }

  .variant-rec {
    font-size: 0.6875rem;
    color: var(--success);
    font-weight: 500;
    margin-top: 4px;
  }

  .variant-warn {
    font-size: 0.6875rem;
    color: var(--warning);
    font-weight: 500;
  }

  .download-error {
    padding: 10px 14px;
    background: rgba(240, 98, 98, 0.08);
    border-radius: 8px;
    color: var(--danger);
    font-size: 0.8125rem;
    margin-bottom: 14px;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .download-progress { margin-bottom: 14px; }

  .progress-bar {
    height: 8px;
    background: rgba(255, 255, 255, 0.06);
    border-radius: 4px;
    overflow: hidden;
    margin-bottom: 8px;
  }

  .progress-fill {
    height: 100%;
    background: var(--primary);
    border-radius: 4px;
    transition: width 300ms ease;
  }

  .progress-fill.complete { background: var(--success); }

  .progress-info {
    display: flex;
    justify-content: space-between;
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .eta { color: var(--text-muted); }

  .download-done {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--success);
    font-size: 0.875rem;
    font-weight: 500;
  }

  .download-queued {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--text-muted);
    font-size: 0.875rem;
    font-weight: 500;
  }
</style>
