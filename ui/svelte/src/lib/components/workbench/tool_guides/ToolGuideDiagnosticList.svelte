<script>
  let { diagnostics = [] } = $props();

  const visibleDiagnostics = $derived(diagnostics ?? []);
</script>

{#if visibleDiagnostics.length}
  <section class="diagnostics" aria-label="Tool guide diagnostics">
    {#each visibleDiagnostics as diagnostic, index (`${diagnostic.guide_id ?? 'catalog'}-${diagnostic.status}-${index}`)}
      <article class={`diagnostic ${diagnostic.status}`}>
        <div>
          <strong>{diagnostic.status}</strong>
          {#if diagnostic.guide_id}
            <span>{diagnostic.guide_id}</span>
          {/if}
        </div>
        <p>{diagnostic.message}</p>
        {#if diagnostic.detail}
          <code>{diagnostic.detail}</code>
        {/if}
      </article>
    {/each}
  </section>
{/if}

<style>
  .diagnostics {
    display: grid;
    gap: 8px;
  }

  .diagnostic {
    display: grid;
    gap: 5px;
    padding: 10px;
    border: 1px solid var(--border-default, #d6d9de);
    border-left: 3px solid var(--accent, #2563eb);
    border-radius: 6px;
    background: var(--surface-subtle, #f8fafc);
  }

  .stale_guide,
  .fingerprint_mismatch,
  .over_token_budget {
    border-left-color: var(--warning, #b45309);
  }

  .blocked {
    border-left-color: var(--danger, #b91c1c);
  }

  div {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: baseline;
  }

  strong,
  span,
  code {
    font-family: var(--font-mono, monospace);
    font-size: 0.75rem;
  }

  p {
    margin: 0;
    color: var(--text-secondary, #374151);
    font-size: 0.85rem;
  }

  code {
    white-space: normal;
    overflow-wrap: anywhere;
    color: var(--text-muted, #4b5563);
  }
</style>
