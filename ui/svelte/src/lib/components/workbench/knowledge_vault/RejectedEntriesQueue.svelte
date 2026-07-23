<script>
  let { entries = [] } = $props();
</script>

<section class="rejected-queue" aria-label="Rejected vault entries">
  <h3>Rejected</h3>
  {#each entries as entry (entry.entry_id)}
    <article data-rejected="true">
      <strong>{entry.title}</strong>
      <span>{entry.slug}</span>
      <p>{entry.reasons.join(', ')}</p>
    </article>
  {:else}
    <p>No rejected entries.</p>
  {/each}
</section>

<style>
  .rejected-queue { display: flex; flex-direction: column; gap: 8px; }
  h3, p { margin: 0; }
  article { border: 1px solid var(--warning, #f0b429); border-radius: 8px; padding: 10px; background: var(--surface-elevated); }
  article span { display: block; color: var(--text-muted); font-size: 0.78rem; margin-top: 2px; }
</style>
