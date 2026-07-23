<script>
  let { entry = null } = $props();
</script>

<section class="vault-entry-panel" aria-label="Vault entry">
  {#if entry}
    <h3>{entry.title}</h3>
    <div class="meta" aria-label="Vault entry metadata">
      <span>{entry.kind}</span>
      <span aria-label={`Confidence ${Math.round(entry.confidence * 100)}%`}>{Math.round(entry.confidence * 100)}%</span>
      <span>{entry.boundary_class}</span>
    </div>
    <p>{entry.slug}</p>
    <section class="frontmatter" aria-label="Vault entry structured metadata">
      <h4>Structured metadata</h4>
      <pre aria-label="Vault entry frontmatter JSON">{JSON.stringify(entry.frontmatter, null, 2)}</pre>
    </section>
  {:else}
    <p role="status">Select a vault entry.</p>
  {/if}
</section>

<style>
  .vault-entry-panel { border: 1px solid var(--border-default); border-radius: 8px; padding: 12px; background: var(--surface-elevated); }
  h3, h4, p { margin: 0; }
  h4 { font-size: 0.78rem; color: var(--text-muted); }
  .frontmatter { display: grid; gap: 6px; }
  .meta { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0; color: var(--text-muted); font-size: 0.82rem; }
  pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 0.78rem; color: var(--text-secondary); }
</style>
