<script>
  import ToolGuideDiagnosticList from './ToolGuideDiagnosticList.svelte';
  import ToolGuideRow from './ToolGuideRow.svelte';
  import { ToolGuideStore } from './toolGuideStore.svelte.js';

  let { activeTools = [], tokenBudget = null, store = new ToolGuideStore() } = $props();

  const selectedGuides = $derived(store.selectedGuides);
  const diagnostics = $derived(store.diagnostics);
  const boundedText = $derived(store.selection?.bounded_text ?? '');
  const tokenStatus = $derived(
    store.selection ? `${store.selection.total_token_count}/${store.selection.token_budget} tokens` : 'No selection'
  );
  let requestKey = $state('');

  $effect(() => {
    const nextKey = JSON.stringify({ activeTools, tokenBudget });
    if (nextKey === requestKey) return;
    requestKey = nextKey;
    store.selectGuides(activeTools, tokenBudget).catch((error) => {
      store.error = error instanceof Error ? error.message : String(error);
    });
  });
</script>

<section class="tool-guide-panel" aria-label="Workbench tool guides">
  <header>
    <div>
      <h2>Tool Guides</h2>
      <p>{tokenStatus}</p>
    </div>
    <button
      type="button"
      onclick={() => store.loadCatalog().catch((error) => {
        store.error = error instanceof Error ? error.message : String(error);
      })}
      disabled={store.loading}
    >
      Refresh
    </button>
  </header>

  {#if store.loading}
    <div class="state" role="status">Loading tool guides.</div>
  {:else if store.error}
    <div class="state error" role="alert">{store.error}</div>
  {:else}
    <ToolGuideDiagnosticList {diagnostics} />

    {#if selectedGuides.length}
      <div class="guide-list">
        {#each selectedGuides as guide (guide.guide_id)}
          <ToolGuideRow {guide} />
        {/each}
      </div>
      <pre>{boundedText}</pre>
    {:else}
      <div class="state" role="status">No active tool guide text selected.</div>
    {/if}
  {/if}
</section>

<style>
  .tool-guide-panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
    width: 100%;
  }

  header {
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

  header p {
    margin-top: 3px;
    color: var(--text-muted, #4b5563);
    font-family: var(--font-mono, monospace);
    font-size: 0.8125rem;
  }

  button {
    padding: 7px 10px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
    color: var(--text-primary, #111827);
    cursor: pointer;
  }

  button:disabled {
    cursor: wait;
    opacity: 0.7;
  }

  .guide-list {
    display: grid;
    gap: 8px;
  }

  .state {
    padding: 18px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
    color: var(--text-muted, #4b5563);
  }

  .error {
    color: var(--danger, #b91c1c);
  }

  pre {
    max-height: 220px;
    margin: 0;
    padding: 12px;
    overflow: auto;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-subtle, #f8fafc);
    color: var(--text-secondary, #374151);
    font-family: var(--font-mono, monospace);
    font-size: 0.8125rem;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
</style>
