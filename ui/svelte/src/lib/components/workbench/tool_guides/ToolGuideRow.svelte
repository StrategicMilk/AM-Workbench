<script>
  let { guide } = $props();

  const provenance = $derived((guide?.provenance_refs ?? []).join(', '));
  const safetyNotes = $derived(guide?.safety_notes ?? []);
</script>

<article class="tool-guide-row">
  <header>
    <div>
      <h3>{guide.guide_id}</h3>
      <p>v{guide.version}</p>
    </div>
    <span>{guide.token_count} tokens</span>
  </header>

  <p class="attribution">{guide.attribution}</p>
  {#if safetyNotes.length}
    <ul>
      {#each safetyNotes as note}
        <li>{note}</li>
      {/each}
    </ul>
  {/if}
  <p class="provenance">{provenance}</p>
</article>

<style>
  .tool-guide-row {
    display: grid;
    gap: 8px;
    padding: 12px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
  }

  header {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    align-items: flex-start;
  }

  h3,
  p,
  ul {
    margin: 0;
  }

  h3 {
    color: var(--text-primary, #111827);
    font-size: 0.95rem;
  }

  header p,
  span,
  .provenance {
    color: var(--text-muted, #4b5563);
    font-family: var(--font-mono, monospace);
    font-size: 0.75rem;
  }

  .attribution {
    color: var(--text-primary, #111827);
    font-size: 0.85rem;
    overflow-wrap: anywhere;
  }

  ul {
    padding-left: 18px;
    color: var(--text-secondary, #374151);
    font-size: 0.85rem;
  }
</style>
