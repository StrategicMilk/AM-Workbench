<script>
  /**
   * Model recommendations panel.
   *
   * Shows recommended models based on scoring and current task context.
   *
   * @prop {Array<object>} recommendations - Scored model recommendations.
   * @prop {(modelId: string) => void} [onselect] - Called when a model is selected.
   */
  let { recommendations = [], onselect } = $props();
  const modalities = ['text', 'vision', 'audio', 'image_generation', 'video_generation', 'embedding', 'three_d'];
  let activeModality = $state('text');

  function recommendationModalities(rec) {
    const values = [
      rec?.modality,
      rec?.primary_modality,
      ...(Array.isArray(rec?.modalities) ? rec.modalities : []),
      ...(Array.isArray(rec?.capabilities) ? rec.capabilities : []),
      ...(Array.isArray(rec?.recommended_for) ? rec.recommended_for : []),
    ];
    return values.map((value) => String(value ?? '').toLowerCase()).filter(Boolean);
  }

  function matchesActiveModality(rec) {
    const values = recommendationModalities(rec);
    if (values.length === 0) return activeModality === 'text';
    return values.some((value) => value === activeModality || value.includes(activeModality));
  }

  let filteredRecommendations = $derived(recommendations.filter(matchesActiveModality));
</script>

{#if recommendations.length > 0}
  <div class="recommendations-panel">
    <div class="modality-tabs" role="tablist" aria-label="Model modality">
      {#each modalities as modality}
        <button
          role="tab"
          aria-selected={activeModality === modality}
          class:active={activeModality === modality}
          onclick={() => (activeModality = modality)}
        >
          {modality.replace('_', ' ')}
        </button>
      {/each}
    </div>
    <h3 class="rec-title">
      <i class="fas fa-star"></i>
      Recommended Models
    </h3>

    <div class="rec-list">
      {#each filteredRecommendations.slice(0, 5) as rec}
        <button class="rec-item" onclick={() => onselect?.(rec.id ?? rec.name)}>
          <div class="rec-name">{rec.name ?? rec.id}</div>
          <div class="rec-meta">
            {#if rec.recommended_for?.length}
              <span class="rec-reason">{rec.recommended_for.join(', ')}</span>
            {:else if rec.reason}
              <span class="rec-reason">{rec.reason}</span>
            {/if}
            {#if rec.memory_gb}
              <span class="rec-size">{rec.memory_gb} GB</span>
            {/if}
          </div>
        </button>
      {:else}
        <div class="rec-empty" role="status">No {activeModality.replace('_', ' ')} recommendations.</div>
      {/each}
    </div>
  </div>
{/if}

<style>
  .recommendations-panel {
    padding: 20px;
    background: var(--surface-bg);
    border: 1px solid var(--border-default);
    border-radius: 12px;
  }

  .rec-title {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-primary);
    margin: 0 0 16px 0;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .rec-title i { color: var(--warning); }

  .rec-list {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .modality-tabs {
    display: flex;
    gap: 4px;
    overflow-x: auto;
    margin-bottom: 12px;
  }

  .modality-tabs button {
    min-height: 32px;
    border: 1px solid var(--border-default);
    background: transparent;
    color: var(--text-muted);
  }

  .modality-tabs button.active {
    color: var(--text-primary);
    border-color: var(--accent);
  }

  .rec-item {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 10px 12px;
    background: transparent;
    border: none;
    color: var(--text-primary);
    text-align: left;
    font-family: inherit;
    cursor: pointer;
    border-radius: 8px;
    transition: background 100ms;
    width: 100%;
  }

  .rec-item:hover {
    background: rgba(255, 255, 255, 0.04);
  }

  .rec-name {
    font-size: 0.875rem;
    font-weight: 500;
  }

  .rec-meta {
    display: flex;
    gap: 10px;
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .rec-size {
    color: var(--text-muted);
    font-weight: 400;
  }

  .rec-empty {
    color: var(--text-muted);
    font-size: 0.8125rem;
    padding: 8px 0;
  }
</style>
