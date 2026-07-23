<script>
  let { entries = [], onReverse = () => {} } = $props();
  let reasonByEvent = $state({});
  let safeEntries = $derived(Array.isArray(entries) ? entries.filter((entry) => entry?.event_id) : []);

  function reasonsText(entry) {
    return Array.isArray(entry?.reasons) && entry.reasons.length ? entry.reasons.join(', ') : 'No reasons recorded';
  }
</script>

<section class="refinement-journal" aria-label="Memory refinement journal">
  <h3>Refinement Journal</h3>
  {#each safeEntries as entry (entry.event_id)}
    <article>
      <div>
        <strong>{entry.kind}</strong>
        <span>{entry.event_id}</span>
      </div>
      <p>{reasonsText(entry)}</p>
      <label>
        <span>Reversal reason for {entry.event_id}</span>
        <input bind:value={reasonByEvent[entry.event_id]} placeholder="Reversal reason" />
      </label>
      <button type="button" onclick={() => onReverse(entry.event_id, reasonByEvent[entry.event_id] ?? '')}>Reverse</button>
    </article>
  {:else}
    <p role="status">No journal entries.</p>
  {/each}
</section>

<style>
  .refinement-journal { display: flex; flex-direction: column; gap: 8px; }
  h3, p { margin: 0; }
  article { border: 1px solid var(--border-default); border-radius: 8px; padding: 10px; background: var(--surface-elevated); display: grid; gap: 8px; }
  article div { display: flex; justify-content: space-between; gap: 8px; color: var(--text-primary); }
  span { color: var(--text-muted); font-size: 0.78rem; }
  label { display: grid; gap: 4px; }
  button { min-height: var(--target-min-block-size, 34px); }
</style>
