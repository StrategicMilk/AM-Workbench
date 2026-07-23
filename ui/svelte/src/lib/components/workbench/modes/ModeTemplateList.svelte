<script>
  const { templates = [], selectedId = '', onSelect = undefined } = $props();

  let selectedTemplateId = $state(selectedId);
  $effect(() => {
    selectedTemplateId = selectedId;
  });

  function selectTemplate(template) {
    selectedTemplateId = template.id;
    if (typeof onSelect === 'function') {
      onSelect(template);
    }
  }

  function evidenceCount(template) {
    return Array.isArray(template.evidence_refs) ? template.evidence_refs.length : 0;
  }
</script>

<section class="mode-template-list" aria-label="Workbench mode templates">
  {#if templates.length === 0}
    <p class="empty-state" role="status">No mode templates available.</p>
  {:else}
    <div class="template-grid">
      {#each templates as template (template.id)}
        <article
          class:selected={selectedTemplateId === template.id}
          class="template-row"
          aria-labelledby={`mode-template-${template.id}`}
          aria-current={selectedTemplateId === template.id ? 'true' : undefined}
        >
          <div class="template-main">
            <h3 id={`mode-template-${template.id}`}>{template.name}</h3>
            <p>{template.charter}</p>
          </div>
          <dl class="template-facts">
            <div>
              <dt>Tools</dt>
              <dd>{template.allowed_tools?.length ?? 0}</dd>
            </div>
            <div>
              <dt>Evidence</dt>
              <dd>{evidenceCount(template)}</dd>
            </div>
            <div>
              <dt>Version</dt>
              <dd>{template.version}</dd>
            </div>
          </dl>
          <button
            type="button"
            class="select-button"
            aria-pressed={selectedTemplateId === template.id}
            aria-label={`Select mode template ${template.name}`}
            onclick={() => selectTemplate(template)}
          >
            Select
          </button>
        </article>
      {/each}
    </div>
  {/if}
</section>

<style>
  .mode-template-list {
    width: 100%;
  }

  .template-grid {
    display: grid;
    gap: 8px;
  }

  .template-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto 88px;
    align-items: center;
    gap: 12px;
    min-height: 84px;
    padding: 12px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    background: var(--surface-primary, #fff);
  }

  .template-row.selected {
    border-color: var(--primary, #2563eb);
    box-shadow: inset 3px 0 0 var(--primary, #2563eb);
  }

  .template-main {
    min-width: 0;
  }

  .template-main h3 {
    margin: 0 0 4px;
    color: var(--text-primary, #1f2937);
    font-size: 0.9375rem;
    font-weight: 700;
  }

  .template-main p {
    margin: 0;
    color: var(--text-muted, #4b5563);
    font-size: 0.8125rem;
    line-height: 1.4;
  }

  .template-facts {
    display: grid;
    grid-template-columns: repeat(3, minmax(54px, auto));
    gap: 8px;
    margin: 0;
  }

  .template-facts div {
    min-width: 54px;
  }

  .template-facts dt {
    color: var(--text-muted, #6b7280);
    font-size: 0.6875rem;
    line-height: 1.2;
  }

  .template-facts dd {
    margin: 2px 0 0;
    color: var(--text-primary, #111827);
    font-size: 0.8125rem;
    font-weight: 700;
  }

  .select-button {
    width: 88px;
    min-height: 44px;
    border: 1px solid var(--border-default, #cbd5e1);
    border-radius: 6px;
    background: var(--surface-secondary, #f8fafc);
    color: var(--text-primary, #111827);
    font: inherit;
    font-size: 0.8125rem;
    font-weight: 700;
    cursor: pointer;
  }

  .select-button:hover,
  .select-button:focus-visible {
    border-color: var(--primary, #2563eb);
    outline: 2px solid var(--primary-soft, rgba(37, 99, 235, 0.22));
    outline-offset: 2px;
  }

  .empty-state {
    margin: 0;
    padding: 12px;
    border: 1px solid var(--border-default, #d6d9de);
    border-radius: 6px;
    color: var(--text-muted, #4b5563);
    font-size: 0.875rem;
  }

  @media (max-width: 720px) {
    .template-row {
      grid-template-columns: 1fr;
      align-items: stretch;
    }

    .template-facts {
      grid-template-columns: repeat(3, minmax(0, 1fr));
    }

    .select-button {
      width: 100%;
    }
  }
</style>
