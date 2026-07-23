<script>
  /**
   * Model card showing name, parameters, format, capabilities, and status.
   *
   * @prop {object} model - Model data from API.
   * @prop {(modelId: string) => void} [onselect] - Called when model is selected.
   * @prop {(modelId: string, modelName: string) => void} [onremove] - Called after the caller confirms removal.
   */
  import { fileSize, decimal } from '$lib/utils/format.js';

  let { model, onselect, onremove } = $props();
  const MAX_USE_CASES = 8;

  function boundedStringList(values, maxItems = MAX_USE_CASES) {
    if (!Array.isArray(values)) return [];
    return values
      .map((value) => String(value ?? '').trim())
      .filter(Boolean)
      .slice(0, maxItems);
  }

  let score = $derived(model.fitness_score ?? model.score ?? null);
  let scoreClass = $derived(
    score == null ? '' :
    score >= 0.8 ? 'score-high' :
    score >= 0.5 ? 'score-mid' : 'score-low'
  );

  // Use-case tags: from recommended_for, capabilities, or HF tags
  let useCases = $derived.by(() => {
    if (model.recommended_for?.length) return boundedStringList(model.recommended_for);
    if (model.capabilities?.length) return boundedStringList(model.capabilities);
    // Map HF pipeline tags to human-friendly labels
    const tagMap = {
      'text-generation': 'general',
      'text2text-generation': 'general',
      'conversational': 'chat',
      'image-text-to-text': 'vision',
      'feature-extraction': 'embeddings',
      'question-answering': 'reasoning',
      'summarization': 'documentation',
      'translation': 'translation',
      'fill-mask': 'general',
      'text-classification': 'classification',
      'token-classification': 'extraction',
    };
    const tags = boundedStringList(model.metrics?.tags, 32);
    const mapped = tags
      .map((t) => tagMap[t])
      .filter(Boolean);
    return [...new Set(mapped)].slice(0, MAX_USE_CASES);
  });

  let statusLabel = $derived(
    model.status === 'loaded' ? 'Loaded' :
    model.status === 'available' ? 'Available' :
    model.format ?? model.category ?? 'Remote'
  );

  let statusClass = $derived(
    model.status === 'loaded' ? 'status-loaded' :
    model.status === 'available' ? 'status-available' : 'status-remote'
  );

  let canRemove = $derived(Boolean(onremove) && model.status !== 'remote');
</script>

<div class="model-card" class:active={model.active}>
  <div class="model-header">
    <div class="model-name" title={model.name ?? model.id}>
      {model.name ?? model.id ?? 'Unknown'}
    </div>
    {#if score != null}
      <div class="model-score {scoreClass}" title="Fitness score" aria-label={`Fitness score ${decimal(score * 100, 0)}`}>
        {decimal(score * 100, 0)}
      </div>
    {/if}
  </div>

  <div class="model-meta">
    {#if model.parameters}
      <span class="model-tag">{model.parameters}</span>
    {/if}
    {#if model.format}
      <span class="model-tag">{model.format}</span>
    {/if}
    {#if model.memory_gb}
      <span class="model-tag">{model.memory_gb} GB</span>
    {/if}
    {#if model.size && !model.memory_gb}
      <span class="model-tag">{fileSize(model.size)}</span>
    {/if}
    {#if model.quantization}
      <span class="model-tag">{model.quantization}</span>
    {/if}
  </div>

  {#if useCases.length > 0}
    <div class="model-uses">
      {#each useCases as use}
        <span class="use-tag">{use}</span>
      {/each}
    </div>
  {/if}

  {#if model.description}
    <div class="model-desc">{model.description}</div>
  {/if}

  <div class="model-footer">
    <span class="model-status {statusClass}" role="status" aria-label={`Model status ${statusLabel}`}>
      {statusLabel}
    </span>
    {#if onselect}
      <button
        class="btn btn-small btn-primary"
        onclick={() => onselect(model.id)}
        disabled={model.active}
        aria-label={`${model.active ? 'Active model' : 'Select model'} ${model.name ?? model.id ?? 'unknown'}`}
      >
        {model.active ? 'Active' : 'Select'}
      </button>
    {/if}
    {#if canRemove}
      <button
        class="btn btn-small btn-danger"
        onclick={() => onremove(model.id, model.name ?? model.id)}
        disabled={model.active}
        title={model.active ? 'Active model cannot be removed' : 'Remove model'}
        aria-label={`Remove model ${model.name ?? model.id ?? 'unknown'}`}
      >
        Remove
      </button>
    {/if}
  </div>
</div>

<style>
  .model-card {
    padding: 16px;
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: 10px;
    transition: border-color 200ms, box-shadow 200ms;
  }

  .model-card:hover {
    border-color: var(--border-hover, rgba(255, 255, 255, 0.12));
  }

  .model-card.active {
    border-color: var(--primary);
    box-shadow: 0 0 0 1px var(--primary);
  }

  .model-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
  }

  .model-name {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
  }

  .model-score {
    font-size: 0.75rem;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 6px;
    flex-shrink: 0;
    margin-left: 8px;
  }

  .score-high { background: rgba(56, 211, 159, 0.15); color: var(--success); }
  .score-mid  { background: rgba(245, 165, 36, 0.15); color: var(--warning); }
  .score-low  { background: rgba(240, 98, 98, 0.15); color: var(--danger); }

  .model-meta {
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }

  .model-tag {
    font-size: 0.6875rem;
    padding: 2px 8px;
    background: rgba(255, 255, 255, 0.05);
    border-radius: 4px;
    color: var(--text-muted);
  }

  .model-uses {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }

  .use-tag {
    font-size: 0.625rem;
    padding: 1px 6px;
    background: rgba(56, 147, 211, 0.12);
    border-radius: 3px;
    color: var(--primary);
    font-weight: 500;
    text-transform: lowercase;
  }

  .model-desc {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-bottom: 12px;
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  .model-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }

  .model-status {
    font-size: 0.75rem;
    font-weight: 500;
    text-transform: capitalize;
  }

  .status-loaded   { color: var(--success); }
  .status-available { color: var(--primary); }
  .status-remote    { color: var(--text-muted); }

  .btn-danger {
    background: var(--danger-muted);
    color: var(--danger);
    border: 1px solid rgba(240, 98, 98, 0.35);
  }

  .btn-danger:hover:not(:disabled) {
    background: rgba(240, 98, 98, 0.2);
  }

  .model-footer :global(.btn) {
    min-height: 44px;
  }
</style>
