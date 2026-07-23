<script>
  import ChoiceRow from './ChoiceRow.svelte';
  import SurfaceSelector from './SurfaceSelector.svelte';
  import { QuickChoicesStore } from './quickChoicesStore.svelte.js';

  let { initialSurface = 'chat', store = new QuickChoicesStore() } = $props();

  let selectedSurface = $state('');
  const surfaces = $derived(store.catalog?.surfaces ?? [selectedSurface]);
  const choices = $derived(store.catalog?.choices ?? []);

  $effect(() => {
    if (!selectedSurface) {
      selectedSurface = initialSurface;
    }
  });

  $effect(() => {
    if (!selectedSurface) return;
    store.loadCatalog(selectedSurface).catch((error) => {
      store.error = error instanceof Error ? error.message : String(error);
    });
  });
</script>

<section class="quick-choices-panel" aria-label="Workbench model quick choices">
  <div class="panel-header">
    <div>
      <h2>Model Quick Choices</h2>
      <p>{selectedSurface}</p>
    </div>
    <SurfaceSelector surfaces={surfaces} selected={selectedSurface} onSelect={(surface) => { selectedSurface = surface; }} />
  </div>

  {#if store.loading}
    <div class="state" role="status">Loading model choices.</div>
  {:else if store.error}
    <div class="state error" role="alert">{store.error}</div>
  {:else if choices.length === 0}
    <div class="state" role="status">No model choices available for this surface.</div>
  {:else}
    <div class="choice-list">
      {#each choices as choice (choice.model_ref.qualified_id)}
        <ChoiceRow {choice} {store} surface={selectedSurface} />
      {/each}
    </div>
  {/if}
</section>

<style>
  .quick-choices-panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
    width: 100%;
  }

  .panel-header {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
    flex-wrap: wrap;
  }

  h2,
  p {
    margin: 0;
  }

  h2 {
    color: var(--text-primary, #111827);
    font-size: 1.25rem;
  }

  p {
    margin-top: 3px;
    color: var(--text-muted, #4b5563);
    font-family: var(--font-mono, monospace);
    font-size: 0.8125rem;
  }

  .choice-list {
    display: grid;
    gap: 8px;
  }

  .state {
    padding: 24px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
    color: var(--text-muted, #4b5563);
  }

  .error {
    color: var(--danger, #b91c1c);
  }
</style>
