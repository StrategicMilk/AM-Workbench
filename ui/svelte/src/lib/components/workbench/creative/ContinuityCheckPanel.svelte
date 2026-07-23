<script>
  let { violations = [] } = $props();
</script>

<section class="continuity-panel" data-testid="creative-continuity-panel">
  <header>
    <h3>Continuity</h3>
    <span class:clean={violations.length === 0} role="status" aria-live="polite">
      {violations.length === 0 ? 'Clean' : `${violations.length} flagged`}
    </span>
  </header>

  {#if violations.length === 0}
    <p>No continuity violations for the active branch.</p>
  {:else}
    <ul>
      {#each violations as violation}
        <li>
          <strong>{violation.kind}</strong>
          <span>{violation.subjectRef}</span>
          <p>{violation.description}</p>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .continuity-panel {
    display: grid;
    gap: 10px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    padding: 14px;
    background: var(--surface-primary, #fff);
  }

  header {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: center;
  }

  h3,
  p,
  ul {
    margin: 0;
  }

  header span {
    border: 1px solid #f59e0b;
    border-radius: 999px;
    padding: 4px 8px;
    color: #92400e;
    background: #fffbeb;
    font-size: 0.76rem;
    font-weight: 700;
  }

  header span.clean {
    border-color: #16a34a;
    color: #166534;
    background: #f0fdf4;
  }

  p,
  li span {
    color: var(--text-secondary, #64748b);
    line-height: 1.35;
  }

  ul {
    display: grid;
    gap: 8px;
    padding: 0;
    list-style: none;
  }

  li {
    display: grid;
    gap: 3px;
    border-left: 3px solid #f59e0b;
    padding-left: 10px;
  }
</style>
