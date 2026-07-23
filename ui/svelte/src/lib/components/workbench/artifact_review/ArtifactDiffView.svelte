<script>
  let { review } = $props();

  const MAX_PRETTY_CHARS = 12000;
  const MAX_CHANGED_SECTIONS = 100;
  const pretty = (value) => {
    const rendered = JSON.stringify(value ?? null, null, 2);
    if (rendered.length <= MAX_PRETTY_CHARS) return rendered;
    return `${rendered.slice(0, MAX_PRETTY_CHARS)}\n... truncated after ${MAX_PRETTY_CHARS} characters`;
  };
  const changedSections = $derived(
    Array.isArray(review?.diff?.changed_sections)
      ? review.diff.changed_sections.slice(0, MAX_CHANGED_SECTIONS)
      : []
  );
</script>

<section class="artifact-diff-view" aria-label="Artifact diff">
  <div class="raw-grid">
    <section>
      <h3>Before</h3>
      <pre data-testid="before-artifact-raw">{pretty(review?.before_artifact)}</pre>
    </section>
    <section>
      <h3>After</h3>
      <pre data-testid="after-artifact-raw">{pretty(review?.after_artifact)}</pre>
    </section>
  </div>

  <section class="changes">
    <h3>Changed sections</h3>
    <ul data-testid="changed-sections-list">
      {#if changedSections.length === 0}
        <li class="empty">No structural changes.</li>
      {:else}
        {#each changedSections as section}
          <li>
            <strong>{section.path}</strong>
            <span>{section.change_kind}</span>
            <pre>{section.before_value_repr}</pre>
            <pre>{section.after_value_repr}</pre>
          </li>
        {/each}
      {/if}
    </ul>
  </section>
</section>

<style>
  .artifact-diff-view {
    display: grid;
    gap: 16px;
  }

  .raw-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 12px;
  }

  h3 {
    margin: 0 0 8px;
    font-size: 0.95rem;
  }

  pre {
    max-height: 280px;
    overflow: auto;
    padding: 12px;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    background: #f9fafb;
    color: #111827;
    font-size: 0.82rem;
    line-height: 1.45;
  }

  ul {
    display: grid;
    gap: 10px;
    padding: 0;
    margin: 0;
    list-style: none;
  }

  li {
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 10px;
  }

  li span {
    margin-left: 8px;
    color: #4b5563;
    font-size: 0.8rem;
  }

  .empty {
    color: #4b5563;
  }
</style>
