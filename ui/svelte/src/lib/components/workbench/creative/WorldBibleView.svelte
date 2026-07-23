<script>
  let { world, toneGuide, timeline = [], relationships = [] } = $props();
</script>

<section class="world-bible" data-testid="creative-world-bible">
  <header>
    <div>
      <h2>{world.title}</h2>
      <p>{world.summary}</p>
    </div>
    <span role="status" aria-label="World voice summary: {toneGuide.voiceSummary}" aria-live="polite">{toneGuide.voiceSummary}</span>
  </header>

  <div class="world-grid">
    <section aria-label="Timeline">
      <h3>Timeline</h3>
      <ol>
        {#each timeline as event}
          <li>
            <strong>{event.sequence}</strong>
            <span>{event.summary}</span>
            <small>{event.evidence_refs?.length ? event.evidence_refs.join(', ') : 'no evidence'}</small>
          </li>
        {/each}
      </ol>
    </section>

    <section aria-label="Relationships">
      <h3>Relationships</h3>
      <ul>
        {#each relationships as relationship}
          <li aria-label="{relationship.source} to {relationship.target}: {relationship.type}">
            {relationship.source}
            <span aria-hidden="true">-&gt;</span>
            {relationship.target}: {relationship.type}
            <small>{relationship.evidence_refs?.length ? relationship.evidence_refs.join(', ') : 'no evidence'}</small>
          </li>
        {/each}
      </ul>
    </section>

    <section aria-label="World provenance">
      <h3>Provenance</h3>
      <dl>
        <div><dt>Authority</dt><dd>{world.authority_ref ?? 'no authority'}</dd></div>
        <div><dt>Provenance</dt><dd>{world.provenance_ref ?? 'no provenance'}</dd></div>
        <div><dt>Evidence</dt><dd>{world.evidence_refs?.length ? world.evidence_refs.join(', ') : 'no evidence'}</dd></div>
      </dl>
    </section>

    <section aria-label="Style rules">
      <h3>Style</h3>
      <div class="rule-row">
        {#each toneGuide.styleRules as rule}
          <span>{rule}</span>
        {/each}
      </div>
    </section>
  </div>
</section>

<style>
  .world-bible {
    display: grid;
    gap: 14px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    padding: 16px;
    background: var(--surface-secondary, #f8fafc);
  }

  header {
    display: flex;
    justify-content: space-between;
    gap: 16px;
  }

  h2,
  h3,
  p,
  ol,
  ul {
    margin: 0;
  }

  h2 {
    font-size: 1.3rem;
  }

  p,
  dl,
  dd,
  li {
    color: var(--text-secondary, #64748b);
    line-height: 1.4;
  }

  header > span,
  .rule-row span {
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 999px;
    padding: 5px 9px;
    background: var(--surface-primary, #fff);
    font-size: 0.875rem;
    font-weight: 700;
    align-self: start;
  }

  .world-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr) minmax(0, 1fr);
    gap: 12px;
  }

  section {
    display: grid;
    gap: 8px;
    min-width: 0;
  }

  ol,
  ul {
    display: grid;
    gap: 6px;
    padding-left: 18px;
  }

  li strong {
    margin-right: 6px;
    color: var(--text-primary, #111827);
  }

  small,
  dd {
    color: var(--text-secondary, #64748b);
    font-size: 0.78rem;
    overflow-wrap: anywhere;
  }

  dl {
    display: grid;
    gap: 6px;
  }

  dd {
    margin: 0;
  }

  .rule-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  @media (max-width: 900px) {
    header,
    .world-grid {
      display: grid;
      grid-template-columns: 1fr;
    }
  }
</style>
