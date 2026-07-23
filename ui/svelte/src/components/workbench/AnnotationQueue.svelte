<script lang="ts">
  import AnnotationCard from './AnnotationCard.svelte';

  let { template, dataset_asset_id, batch_size = 50, onCommit = () => {} } = $props();

  let strategy = $state<'uncertainty' | 'margin' | 'disagreement'>('uncertainty');
  let queue = $state<{ batch_id: string; items: any[]; template_name: string; template_version: string; record_kind: string } | null>(null);
  let activeIndex = $state(0);
  let loadError = $state<string | null>(null);
  let loading = $state(false);
  let activeItem = $derived(queue?.items?.[activeIndex] ?? null);

  $effect(() => {
    void strategy;
    if (template && dataset_asset_id) {
      load();
    }
  });

  async function load() {
    loading = true;
    loadError = null;
    const boundedBatchSize = Math.max(1, Math.min(Number(batch_size) || 50, 200));
    try {
      const response = await fetch('/api/v1/workbench/annotation/queue', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'X-Requested-With': 'XMLHttpRequest'
        },
        body: JSON.stringify({ template, dataset_asset_id, strategy, batch_size: boundedBatchSize })
      });
      const json = await response.json();
      if (!response.ok) {
        loadError = json.reason ?? `server-error: ${response.status}`;
        queue = null;
        return;
      }
      queue = json;
      activeIndex = 0;
    } catch (error) {
      loadError = `network-error: ${error instanceof Error ? error.message : 'unknown'}`;
    } finally {
      loading = false;
    }
  }

  function advance() {
    if (!queue) return;
    activeIndex = Math.min(activeIndex + 1, queue.items.length);
  }
</script>

<section class="annotation-queue">
  <header>
    <select bind:value={strategy} aria-label="Priority strategy">
      <option value="uncertainty">uncertainty</option>
      <option value="margin">margin</option>
      <option value="disagreement">disagreement</option>
    </select>
    <button type="button" onclick={load} disabled={loading}>Load</button>
    <span>{queue?.items.length ?? 0} items in batch {queue?.batch_id ?? ''}</span>
  </header>

  {#if loadError}
    <p class="error" role="alert">{loadError}</p>
  {:else if activeItem}
    <AnnotationCard
      {template}
      item={activeItem}
      record_kind={queue?.record_kind}
      onCommit={(detail) => {
        onCommit(detail);
        advance();
      }}
    />
  {:else}
    <p>No items queued.</p>
  {/if}

  <footer>
    <button type="button" onclick={() => (activeIndex = Math.max(activeIndex - 1, 0))}>Previous</button>
    <button type="button" onclick={advance}>Next</button>
  </footer>
</section>

<style>
  .annotation-queue {
    display: grid;
    gap: 0.75rem;
  }
  header,
  footer {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
  }
  .error {
    color: var(--color-danger, #b42318);
  }

  select,
  button {
    min-height: 44px;
  }
</style>
