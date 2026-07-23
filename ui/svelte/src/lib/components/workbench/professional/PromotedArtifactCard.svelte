<script>
  let { record = null } = $props();

  const promotionLabels = {
    checklist: 'Checklist',
    document_packet: 'Document packet',
    professional_memo: 'Professional memo',
    source_backed_note: 'Source-backed note',
    reminder: 'Reminder',
    evidence_notebook_entry: 'Evidence notebook entry',
    meeting_prep_brief: 'Meeting prep brief',
  };

  const rigorLabels = {
    check_it_carefully: 'Check it carefully',
    make_it_reusable: 'Make it reusable',
  };

  let artifactKindLabel = $derived(record?.artifact_kind ? (promotionLabels[record.artifact_kind] ?? record.artifact_kind) : '');
  let rigorLabel = $derived(record?.rigor_level ? (rigorLabels[record.rigor_level] ?? record.rigor_level) : '');
</script>

<section class="artifact-card" aria-label="Promoted artifact record">
  {#if record}
    <header>
      <div>
        <h3>{artifactKindLabel}</h3>
        <p>{record.artifact_id}</p>
      </div>
      <span aria-label={`Artifact kind ${artifactKindLabel}`}>{artifactKindLabel}</span>
    </header>

    <dl>
      <div>
        <dt>Rigor level</dt>
        <dd>{rigorLabel}</dd>
      </div>
      <div>
        <dt>Claim decision</dt>
        <dd>{record.claim_promotion_decision_ref}</dd>
      </div>
      <div>
        <dt>Mode lens</dt>
        <dd>{record.mode_lens_id}</dd>
      </div>
    </dl>

    <section>
      <h4>Provenance</h4>
      <ol>
        {#each record.provenance as pair}
          <li><strong>{pair[0]}</strong>: {pair[1]}</li>
        {/each}
      </ol>
    </section>

    <section>
      <h4>Source cards</h4>
      <div class="links">
        {#each record.source_card_ids as sourceId}
          <a href={`/workbench/source-tool-cards?source_card_id=${encodeURIComponent(sourceId)}`}>{sourceId}</a>
        {:else}
          <span>No source_card_ids</span>
        {/each}
      </div>
    </section>

    <section>
      <h4>Tool cards</h4>
      <div class="links">
        {#each record.tool_card_ids as toolId}
          <a href={`/workbench/source-tool-cards?tool_card_id=${encodeURIComponent(toolId)}`}>{toolId}</a>
        {:else}
          <span>No tool_card_ids</span>
        {/each}
      </div>
    </section>
  {:else}
    <div class="empty">No promoted artifact yet.</div>
  {/if}
</section>

<style>
  .artifact-card {
    display: grid;
    gap: 12px;
    padding: 14px;
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 8px;
    background: var(--surface-primary, #fff);
    color: var(--text-primary, #111827);
  }

  header,
  dl {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 10px;
  }

  dl {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  h3,
  h4,
  p,
  dl,
  dd,
  ol {
    margin: 0;
  }

  p,
  dd,
  .empty {
    color: var(--text-secondary, #4b5563);
  }

  h4,
  dt {
    font-size: 0.78rem;
    font-weight: 700;
  }

  header span,
  .links a,
  .links span {
    border: 1px solid var(--border-color, #d1d5db);
    border-radius: 6px;
    padding: 4px 7px;
    font-size: 0.78rem;
  }

  .links {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  a {
    color: var(--accent-color, #2563eb);
  }

  @media (max-width: 760px) {
    header,
    dl {
      grid-template-columns: 1fr;
    }
  }
</style>
