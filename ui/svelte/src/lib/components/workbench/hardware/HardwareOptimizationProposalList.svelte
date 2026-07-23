<script>
  let { proposals = [] } = $props();

  let orderedProposals = $derived(
    [...(Array.isArray(proposals) ? proposals : [])].sort((left, right) => {
      const riskRank = { safe_adaptation: 0, safe_host_improvement: 1, risky_host_change: 2 };
      return (riskRank[left.risk] ?? 3) - (riskRank[right.risk] ?? 3);
    })
  );
</script>

<section class="proposal-list" aria-label="Hardware optimization proposals">
  <header>
    <h3>Proposals</h3>
    <span>{orderedProposals.length}</span>
  </header>

  <div class="items">
    {#each orderedProposals as proposal}
      <article class:risky={proposal.risk === 'risky_host_change'}>
        <div class="topline">
          <h4>{proposal.title}</h4>
          <span>{proposal.risk}</span>
        </div>
        <dl>
          <div>
            <dt>Scope</dt>
            <dd>{proposal.scope}</dd>
          </div>
          <div>
            <dt>Status</dt>
            <dd>{proposal.status}</dd>
          </div>
          <div>
            <dt>Review</dt>
            <dd>{proposal.review_required ? 'required' : 'not required'}</dd>
          </div>
          <div>
            <dt>Before</dt>
            <dd>{Array.isArray(proposal.before_measurement_evidence_ids) ? proposal.before_measurement_evidence_ids.join(', ') : 'not measured'}</dd>
          </div>
          <div>
            <dt>After</dt>
            <dd>{Array.isArray(proposal.expected_after_evidence_requirements) ? proposal.expected_after_evidence_requirements.join(', ') : 'not specified'}</dd>
          </div>
          <div>
            <dt>Rollback</dt>
            <dd>{proposal.rollback_notes || 'workbench policy revert'}</dd>
          </div>
        </dl>
      </article>
    {/each}
  </div>
</section>

<style>
  .proposal-list {
    display: grid;
    gap: 10px;
  }

  header,
  .topline {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  h3,
  h4 {
    margin: 0;
    letter-spacing: 0;
  }

  h3 {
    font-size: 14px;
  }

  h4 {
    font-size: 13px;
  }

  .items {
    display: grid;
    gap: 8px;
  }

  article {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 10px;
  }

  article.risky {
    border-color: #f59e0b;
  }

  dl {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 8px;
    margin: 10px 0 0;
  }

  dt {
    margin-bottom: 4px;
    color: var(--text-muted, #94a3b8);
    font-size: 11px;
  }

  dd {
    margin: 0;
    overflow-wrap: anywhere;
    font-size: 12px;
  }
</style>
