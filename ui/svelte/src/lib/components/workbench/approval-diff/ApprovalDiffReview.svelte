<script>
  import Icon from '$lib/a11y/Icon.svelte';
  import VisuallyHidden from '$lib/a11y/VisuallyHidden.svelte';
  import { fetchResourceCockpitPolicyProposals, postResourceCockpitApprovalDiff } from '$lib/api.js';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';
  import { APPROVAL_DIFF_STATUS } from '$lib/uiEnums.js';

  let {
    projectId = 'default',
    approvalDiff = null,
    loading = false,
    error = null,
    proposal = null,
    autoload = true,
  } = $props();
  let loadedApprovalDiff = $state(null);
  let loadingFromApi = $state(false);
  let loadError = $state(null);
  let loadedKey = $state('');

  const requiredDimensions = [
    { key: 'output_behavior', label: 'Output behavior' },
    { key: 'prompt', label: 'Prompt' },
    { key: 'model', label: 'Model' },
    { key: 'route', label: 'Route' },
    { key: 'retrieval', label: 'Retrieval' },
    { key: 'tools', label: 'Tools' },
    { key: 'cost', label: 'Cost' },
    { key: 'latency', label: 'Latency' },
    { key: 'safety', label: 'Safety' },
    { key: 'eval_score', label: 'Eval score' },
    { key: 'affected_assets', label: 'Affected assets' },
    { key: 'rollback_target', label: 'Rollback target' },
    { key: 'policy_gates', label: 'Policy gates' },
  ];

  const emptyDiff = {};

  function asList(value) {
    return Array.isArray(value) ? value.filter(Boolean) : [];
  }

  function isPresent(value) {
    return value !== null && value !== undefined && String(value).trim() !== '';
  }

  function evidenceBlockers(refs, context) {
    if (refs.length === 0) {
      return ['missing_source_backed_refs'];
    }
    try {
      requireEvidence(refs, context);
      return [];
    } catch (error) {
      return [`invalid_source_backed_refs:${error.message}`];
    }
  }

  function failClosedState(reason, details = []) {
    return {
      status: APPROVAL_DIFF_STATUS.BLOCKED,
      proposalId: 'No source-backed diff selected',
      target: 'Governed promotion',
      blockers: [reason, ...details],
      dimensions: emptyDiff,
      gatePassed: false,
      provenanceRef: '',
      authorityRef: '',
      persistedStateRefs: [],
      recovery: 'Reload approval diff evidence before promotion.',
    };
  }

  function proposalKey(candidate) {
    if (!candidate || typeof candidate !== 'object') return `project:${projectId || 'default'}`;
    return `proposal:${candidate.proposal_id ?? candidate.proposalId ?? projectId ?? 'default'}`;
  }

  function selectProposal(items) {
    if (!Array.isArray(items) || items.length === 0) return null;
    return items.find((item) => item?.status === APPROVAL_DIFF_STATUS.OPEN) ?? items[0];
  }

  async function loadSourceBackedApprovalDiff(key) {
    loadingFromApi = true;
    loadError = null;
    try {
      let sourceProposal = proposal;
      if (!sourceProposal) {
        const response = await fetchResourceCockpitPolicyProposals();
        sourceProposal = selectProposal(response?.items ?? response?.proposals ?? response);
      }
      if (!sourceProposal) {
        loadedApprovalDiff = null;
        loadError = 'no_source_backed_policy_proposal';
        loadedKey = key;
        return;
      }
      const proposalId = sourceProposal.proposal_id ?? sourceProposal.proposalId;
      if (!proposalId) {
        loadedApprovalDiff = null;
        loadError = 'policy_proposal_missing_id';
        loadedKey = key;
        return;
      }
      loadedApprovalDiff = await postResourceCockpitApprovalDiff(proposalId, sourceProposal);
      loadedKey = key;
    } catch (err) {
      loadedApprovalDiff = null;
      loadError = err instanceof Error ? err.message : String(err);
      loadedKey = key;
    } finally {
      loadingFromApi = false;
    }
  }

  function normalizeApprovalDiff(diff) {
    if (loading || loadingFromApi) {
      return failClosedState('approval_diff_loading');
    }
    const effectiveError = error ?? loadError;
    if (effectiveError) {
      return failClosedState('approval_diff_load_error', [String(effectiveError)]);
    }
    if (!diff || typeof diff !== 'object') {
      return failClosedState('missing_approval_diff');
    }

    const dimensions = diff.dimensions && typeof diff.dimensions === 'object' ? diff.dimensions : emptyDiff;
    const blockers = asList(diff.gate_blockers ?? diff.blockers);
    const missing = [];

    if (!isPresent(diff.proposal_id ?? diff.proposalId)) missing.push('proposal_id');
    if (!isPresent(diff.provenance_ref ?? diff.provenanceRef)) missing.push('provenance');
    if (!isPresent(diff.authority_ref ?? diff.authorityRef)) missing.push('authority');
    const persistedStateRefs = asList(diff.persisted_state_refs ?? diff.persistedStateRefs);
    if (persistedStateRefs.length === 0) missing.push('persisted_state');
    if (dimensions === emptyDiff) missing.push('dimensions');
    const refBlockers = evidenceBlockers(
      [diff.provenance_ref ?? diff.provenanceRef, diff.authority_ref ?? diff.authorityRef, ...persistedStateRefs].filter(Boolean),
      'approval-diff',
    );

    const stale =
      diff.stale === true ||
      diff.freshness === APPROVAL_DIFF_STATUS.STALE ||
      diff.status === APPROVAL_DIFF_STATUS.STALE;
    const explicitlyReady =
      diff.status === APPROVAL_DIFF_STATUS.READY || diff.status === APPROVAL_DIFF_STATUS.APPROVED;
    const gatePassed =
      diff.gate_passed === true && blockers.length === 0 && missing.length === 0 && refBlockers.length === 0 && !stale;

    if (!gatePassed || !explicitlyReady) {
      return {
        status: stale ? APPROVAL_DIFF_STATUS.STALE : APPROVAL_DIFF_STATUS.BLOCKED,
        proposalId: diff.proposal_id ?? diff.proposalId ?? 'Unidentified proposal',
        target: diff.target ?? 'Governed promotion',
        blockers: [
          ...blockers,
          ...refBlockers,
          ...missing.map((field) => `missing_${field}`),
          ...(explicitlyReady ? [] : ['status_not_ready']),
          ...(stale ? ['stale_approval_diff'] : []),
        ],
        dimensions,
        gatePassed: false,
        provenanceRef: diff.provenance_ref ?? diff.provenanceRef ?? '',
        authorityRef: diff.authority_ref ?? diff.authorityRef ?? '',
        persistedStateRefs,
        recovery: diff.recovery ?? 'Resolve blockers before promotion.',
      };
    }

    return {
      status: APPROVAL_DIFF_STATUS.READY,
      proposalId: diff.proposal_id ?? diff.proposalId,
      target: diff.target ?? 'Governed promotion',
      blockers: [],
      dimensions,
      gatePassed,
      provenanceRef: diff.provenance_ref ?? diff.provenanceRef,
      authorityRef: diff.authority_ref ?? diff.authorityRef,
      persistedStateRefs,
      recovery: '',
    };
  }

  $effect(() => {
    const effectiveDiff = approvalDiff ?? null;
    const key = proposalKey(proposal);
    if (!autoload || effectiveDiff || loadedKey === key || loadingFromApi) return;
    void loadSourceBackedApprovalDiff(key);
  });

  let reviewState = $derived(normalizeApprovalDiff(approvalDiff ?? loadedApprovalDiff));
</script>

<section class="approval-diff-view" aria-label="Approval diff review">
  <header class="approval-diff-header">
    <div>
      <p class="eyebrow">Workbench</p>
      <h1>Approval Diff</h1>
    </div>
    <div class="status-chip" data-status={reviewState.status}>
      <Icon name={reviewState.gatePassed ? 'circle-check' : 'lock'} />
      <span>{reviewState.status}</span>
    </div>
  </header>

  <div class="summary-strip">
    <div>
      <span class="label">Project</span>
      <strong>{projectId || 'default'}</strong>
    </div>
    <div>
      <span class="label">Proposal</span>
      <strong>{reviewState.proposalId}</strong>
    </div>
    <div>
      <span class="label">Target</span>
      <strong>{reviewState.target}</strong>
    </div>
    <div>
      <span class="label">Provenance</span>
      <strong>{reviewState.provenanceRef || 'missing'}</strong>
    </div>
    <div>
      <span class="label">Authority</span>
      <strong>{reviewState.authorityRef || 'missing'}</strong>
    </div>
    <div>
      <span class="label">Persisted state</span>
      <strong>{reviewState.persistedStateRefs.join(', ') || 'missing'}</strong>
    </div>
    <div>
      <span class="label">Recovery</span>
      <strong>{reviewState.recovery || 'ready'}</strong>
    </div>
  </div>

  <div class="review-grid">
    <section class="dimension-panel" aria-label="Diff dimensions">
      <h2>Dimensions</h2>
      <div class="dimension-list">
        {#each requiredDimensions as dimension}
          <div class="dimension-row">
            <span>{dimension.label}</span>
            {#if reviewState.dimensions[dimension.key]}
              <Icon name="circle-check" />
              <VisuallyHidden>present</VisuallyHidden>
            {:else}
              <Icon name="circle-exclamation" />
              <VisuallyHidden>missing</VisuallyHidden>
            {/if}
          </div>
        {/each}
      </div>
    </section>

    <section class="gate-panel" aria-label="Fail closed gates">
      <h2>Gates</h2>
      <div class="gate-list">
        {#each reviewState.blockers as blocker}
          <div class="gate-row">
            <Icon name="ban" />
            <span>{blocker}</span>
          </div>
        {:else}
          <div class="gate-row ready">
            <Icon name="circle-check" />
            <span>ready_for_approval</span>
          </div>
        {/each}
      </div>
    </section>
  </div>
</section>

<style>
  .approval-diff-view {
    min-height: 100%;
    padding: 24px;
    background: var(--bg-primary);
    color: var(--text-primary);
  }

  .approval-diff-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 20px;
  }

  .eyebrow {
    margin: 0 0 4px;
    color: var(--text-muted);
    font-size: 12px;
    text-transform: uppercase;
  }

  h1,
  h2 {
    margin: 0;
    letter-spacing: 0;
  }

  h1 {
    font-size: 28px;
  }

  h2 {
    font-size: 16px;
    margin-bottom: 12px;
  }

  .status-chip {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--border-default);
    border-radius: 8px;
    padding: 8px 10px;
    background: var(--surface-elevated);
    color: var(--text-primary);
    text-transform: capitalize;
  }

  .summary-strip {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 20px;
  }

  .summary-strip > div,
  .dimension-panel,
  .gate-panel {
    border: 1px solid var(--border-default);
    border-radius: 8px;
    background: var(--surface-elevated);
    padding: 14px;
  }

  .label {
    display: block;
    margin-bottom: 6px;
    color: var(--text-muted);
    font-size: 12px;
  }

  .review-grid {
    display: grid;
    grid-template-columns: minmax(0, 2fr) minmax(240px, 1fr);
    gap: 16px;
  }

  .dimension-list,
  .gate-list {
    display: grid;
    gap: 8px;
  }

  .dimension-row,
  .gate-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    min-height: 36px;
    border: 1px solid var(--border-subtle, var(--border-default));
    border-radius: 6px;
    padding: 8px 10px;
  }

  .dimension-row :global(i),
  .gate-row :global(i) {
    color: var(--warning, #f59e0b);
  }

  .gate-row {
    justify-content: flex-start;
  }

  @media (max-width: 820px) {
    .approval-diff-header,
    .summary-strip,
    .review-grid {
      grid-template-columns: 1fr;
    }

    .approval-diff-header {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
