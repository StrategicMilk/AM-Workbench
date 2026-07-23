<script>
  import * as api from '$lib/api.js';

  let {
    datasets = [],
    selectedRevision = '',
    projectId = 'default',
    ragDefaults = null,
    onRun = () => {},
    disabled = false,
  } = $props();

  let queryText = $state('Where did retrieval lose grounding?');
  let topK = $state(5);
  let metadataKey = $state('source');
  let metadataValue = $state('');
  let rewrite = $state('');
  let hybridAlpha = $state(0.5);
  let embeddingModel = $state('');
  let reranker = $state('none');
  let defaultsState = $state('loading');
  let selectedRevisionId = $state('');
  const TOP_K_MIN = 1;
  const TOP_K_MAX = 50;
  const HYBRID_ALPHA_MIN = 0;
  const HYBRID_ALPHA_MAX = 1;
  const privacyNotice =
    'Queries and filters are sent to the selected local retrieval pipeline for this replay only; retain sensitive source text in the indexed dataset, not in the question field.';

  function clampNumber(value, min, max, fallback) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return fallback;
    return Math.min(max, Math.max(min, numeric));
  }

  const fieldIds = {
    question: 'rag-query-question',
    topK: 'rag-query-top-k',
    hybridAlpha: 'rag-query-hybrid-alpha',
    embeddingModel: 'rag-query-embedding-model',
    reranker: 'rag-query-reranker',
    metadataKey: 'rag-query-filter-key',
    metadataValue: 'rag-query-filter-value',
    rewrite: 'rag-query-rewrite',
  };

  let selectedDatasetLabel = $derived(
    datasets.find((dataset) => dataset.revision_id === selectedRevisionId)?.branch ?? selectedRevisionId
  );

  $effect(() => {
    selectedRevisionId = selectedRevision;
  });

  function applyDefaults(defaults) {
    if (!defaults || typeof defaults !== 'object') {
      defaultsState = 'blocked:missing_rag_defaults';
      return;
    }
    embeddingModel = String(defaults.embedding_model ?? defaults.embeddingModel ?? '');
    reranker = String(defaults.reranker ?? 'none');
    hybridAlpha = clampNumber(
      defaults.hybrid_alpha ?? defaults.hybridAlpha ?? hybridAlpha,
      HYBRID_ALPHA_MIN,
      HYBRID_ALPHA_MAX,
      hybridAlpha
    );
    defaultsState = embeddingModel ? 'api' : 'blocked:missing_embedding_model';
  }

  $effect(() => {
    if (ragDefaults) {
      applyDefaults(ragDefaults);
      return;
    }
    let cancelled = false;
    api.getRagQueryDefaults(projectId)
      .then((result) => {
        if (!cancelled) applyDefaults(result);
      })
      .catch((error) => {
        if (!cancelled) defaultsState = `blocked:${error?.message ?? 'rag_defaults_unavailable'}`;
      });
    return () => {
      cancelled = true;
    };
  });

  function submit() {
    if (!embeddingModel.trim()) {
      defaultsState = 'blocked:missing_embedding_model';
      return;
    }
    const filters = {};
    if (metadataKey.trim() && metadataValue.trim()) {
      filters[metadataKey.trim()] = metadataValue.trim();
    }
    onRun({
      revision_id: selectedRevisionId,
      query_text: queryText,
      top_k: clampNumber(topK, TOP_K_MIN, TOP_K_MAX, 5),
      filters,
      rewrite,
      hybrid_alpha: clampNumber(hybridAlpha, HYBRID_ALPHA_MIN, HYBRID_ALPHA_MAX, 0.5),
      embedding_model: embeddingModel,
      reranker,
    });
  }
</script>

<section class="rag-query-panel" aria-label="RAG query">
  <div class="query-head" data-defaults-state={defaultsState}>
    <h3>Query</h3>
    <span>{selectedDatasetLabel || 'No revision selected'}</span>
    <small>{defaultsState}</small>
  </div>

  <label>
    <span>Dataset revision</span>
    <select bind:value={selectedRevisionId}>
      <option value="">Select revision</option>
      {#each datasets as dataset (dataset.revision_id)}
        <option value={dataset.revision_id}>{dataset.branch ?? dataset.revision_id}</option>
      {/each}
    </select>
  </label>

  <label>
    <span>Question</span>
    <textarea id={fieldIds.question} bind:value={queryText} rows="4" aria-describedby="rag-query-privacy"></textarea>
  </label>

  <p id="rag-query-privacy" class="privacy-notice">{privacyNotice}</p>

  <div class="query-grid">
    <label>
      <span>Top K</span>
      <input id={fieldIds.topK} type="number" min="1" max="50" bind:value={topK} />
    </label>
    <label>
      <span>Hybrid alpha</span>
      <input
        id={fieldIds.hybridAlpha}
        type="range"
        min="0"
        max="1"
        step="0.05"
        bind:value={hybridAlpha}
        aria-valuetext={`${Number(hybridAlpha).toFixed(2)} hybrid retrieval weight`}
      />
    </label>
    <label>
      <span>Embedding model</span>
      <input id={fieldIds.embeddingModel} bind:value={embeddingModel} />
    </label>
    <label>
      <span>Reranker</span>
      <select id={fieldIds.reranker} bind:value={reranker}>
        <option value="none">None</option>
        <option value="cross-encoder">Cross encoder</option>
        <option value="metadata-aware">Metadata aware</option>
      </select>
    </label>
  </div>

  <div class="filter-row">
    <label>
      <span>Filter key</span>
      <input id={fieldIds.metadataKey} bind:value={metadataKey} />
    </label>
    <label>
      <span>Filter value</span>
      <input id={fieldIds.metadataValue} bind:value={metadataValue} />
    </label>
  </div>

  <label>
    <span>Rewrite</span>
    <input id={fieldIds.rewrite} bind:value={rewrite} />
  </label>

  <button class="btn btn-primary" type="button" onclick={submit} disabled={disabled || !selectedRevisionId}>
    Run replay
  </button>
</section>

<style>
  .rag-query-panel {
    display: grid;
    gap: 14px;
  }

  .query-head,
  .filter-row,
  .query-grid {
    display: grid;
    gap: 12px;
  }

  .query-head {
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
  }

  .query-head h3 {
    margin: 0;
    font-size: 1rem;
  }

  .query-head span {
    color: var(--text-secondary);
    font-size: 0.8125rem;
  }

  .query-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .filter-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  label {
    display: grid;
    gap: 6px;
    color: var(--text-secondary);
    font-size: 0.8125rem;
  }

  .privacy-notice {
    margin: -4px 0 0;
    color: var(--text-muted);
    font-size: 0.76rem;
    line-height: 1.35;
  }

  input,
  select,
  textarea {
    width: 100%;
    min-width: 0;
    min-height: 44px;
  }

  @media (max-width: 760px) {
    .query-head,
    .filter-row,
    .query-grid {
      grid-template-columns: 1fr;
    }
  }
</style>
