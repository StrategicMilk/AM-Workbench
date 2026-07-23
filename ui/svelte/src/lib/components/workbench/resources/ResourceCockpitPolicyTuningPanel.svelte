<script>
  import { ApprovalDiffReview } from '$lib/components/workbench/approval-diff';
  import { postResourceCockpitApprovalDiff } from '$lib/api.js';

  let { proposals = [], projectId = 'default', onRequestApprovalDiff = () => {} } = $props();
  let selectedDiff = $state(null);
  let pendingProposalId = $state(null);
  let error = $state(null);

  async function requestApprovalDiff(proposal) {
    pendingProposalId = proposal.proposal_id;
    error = null;
    try {
      selectedDiff = await postResourceCockpitApprovalDiff(proposal.proposal_id, proposal);
      onRequestApprovalDiff(selectedDiff);
    } catch (err) {
      error = err.message;
    } finally {
      pendingProposalId = null;
    }
  }

  function dimensions(proposal) {
    return Object.keys(proposal.dimension_changes ?? {}).join(', ') || 'unknown';
  }
</script>

<section class="policy-panel" aria-label="Resource policy tuning proposals">
  <header>
    <h2>Policy Tuning</h2>
    <span>{proposals.length}</span>
  </header>

  {#if error}
    <div class="alert" role="alert" aria-live="assertive">{error}</div>
  {/if}

  <div class="proposal-list">
    {#each proposals as proposal (proposal.proposal_id)}
      <article>
        <div class="proposal-head">
          <strong>{proposal.target}</strong>
          <span>{Math.round((proposal.confidence ?? 0) * 100)}%</span>
        </div>
        <dl>
          <div><dt>dimensions</dt><dd>{dimensions(proposal)}</dd></div>
          <div><dt>evidence</dt><dd>{proposal.evidence_ids?.length ?? 0}</dd></div>
          <div><dt>rollback</dt><dd>{proposal.rollback_target_ref ?? 'unknown'}</dd></div>
        </dl>
        <button
          type="button"
          onclick={() => requestApprovalDiff(proposal)}
          disabled={pendingProposalId === proposal.proposal_id}
        >
          Request approval diff
        </button>
      </article>
    {:else}
      <p class="empty" role="status">No policy proposals available.</p>
    {/each}
  </div>

  {#if selectedDiff}
    <section class="handoff" aria-live="polite">
      <h3>Approval Diff Handoff</h3>
      <p>{selectedDiff.proposal_id} is {selectedDiff.status}</p>
      <ApprovalDiffReview projectId={projectId || 'default'} approvalDiff={selectedDiff} autoload={false} />
    </section>
  {/if}
</section>

<style>
  .policy-panel,
  .proposal-list,
  article,
  dl {
    display: grid;
    gap: 12px;
  }

  header,
  .proposal-head,
  dl div {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  h2,
  h3 {
    margin: 0;
    letter-spacing: 0;
  }

  h2 {
    font-size: 16px;
  }

  h3 {
    font-size: 14px;
  }

  article,
  .empty,
  .alert,
  .handoff {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 10px;
  }

  .alert {
    border-color: #f59e0b;
  }

  dl {
    margin: 0;
  }

  dt,
  .empty {
    color: var(--text-muted, #94a3b8);
  }

  dd {
    margin: 0;
    text-align: right;
    overflow-wrap: anywhere;
  }

  button {
    justify-self: start;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-panel, #0f172a);
    color: inherit;
    padding: 7px 10px;
  }
</style>
