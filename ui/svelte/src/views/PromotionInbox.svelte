<script>
  import { onMount } from 'svelte';
  import PromotionCard from '../components/workbench/PromotionCard.svelte';
  import { appState } from '$lib/stores/app.svelte.js';
  import { showToast } from '$lib/stores/toast.svelte.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import EvidenceTraceLink from '../components/workbench/promotion_inbox/EvidenceTraceLink.svelte';
  import { readWorkbenchJourneyState } from '$lib/workbench/journey_router.ts';
  import { workbenchKernelRequest } from '$lib/api.js';
  import { isBrowser } from '$lib/utils/browser.js';

  // Props use $props(); do not replace with legacy export let in Svelte 5.
  let { projectId = '' } = $props();

  let items = $state([]);
  let loading = $state(true);
  let error = $state('');
  let decidingProposalId = $state('');
  let cardErrors = $state({});

  let activeProjectId = $derived(projectId || appState.currentProjectId || 'default');
  function browserSearch() {
    return isBrowser() ? window.location.search : '';
  }

  let journeyState = $derived(readWorkbenchJourneyState(browserSearch(), activeProjectId));

  async function loadPromotions() {
    loading = true;
    error = '';
    try {
      const data = await workbenchKernelRequest(`/api/v1/projects/${encodeURIComponent(activeProjectId)}/workbench/promotions`);
      items = data.items ?? [];
    } catch (err) {
      error = err?.message ?? 'Failed to load promotion inbox';
      showToast(`Failed to load promotion inbox: ${error}`, 'error');
    } finally {
      loading = false;
    }
  }

  async function handleDecide(event) {
    const detail = event.detail;
    decidingProposalId = detail.proposal_id;
    cardErrors = { ...cardErrors, [detail.proposal_id]: '' };
    try {
      await workbenchKernelRequest(
        `/api/v1/projects/${encodeURIComponent(activeProjectId)}/workbench/promotions/${encodeURIComponent(detail.proposal_id)}/decide`,
        {
          method: 'POST',
          body: JSON.stringify({
            accepted: detail.accepted,
            decided_by: detail.decided_by,
            rationale: detail.rationale,
          }),
        },
      );
      showToast(detail.accepted ? 'Promotion approved' : 'Promotion rejected', 'success');
      await loadPromotions();
    } catch (err) {
      if (err?.status === 409) {
        cardErrors = { ...cardErrors, [detail.proposal_id]: 'This proposal has already been decided.' };
        await loadPromotions();
      } else if (err?.status === 422) {
        cardErrors = { ...cardErrors, [detail.proposal_id]: err.detail || 'Validation failed.' };
      } else {
        error = err?.message ?? 'Decision failed';
        showToast(`Promotion decision failed: ${error}`, 'error');
      }
    } finally {
      decidingProposalId = '';
    }
  }

  onMount(loadPromotions);
</script>

<section class="promotion-inbox">
  <header class="view-header">
    <div>
      <h2>Promotion Inbox</h2>
      <p>{activeProjectId}</p>
      <HelpPopover
        title="Promotion Inbox"
        body="Pending method promotion proposals for this project. A promotion proposal is created when the AM Workbench Inspector detects a behavior improvement candidate that meets the promotion threshold. Approval uses first-match semantics: the first chain member to approve or reject a proposal decides the outcome — later chain members cannot override. Rejection is final and fail-closed: a rejected proposal cannot be re-opened. Approve proposals you want to persist as learned methods; reject proposals that reflect unwanted behaviors."
        severity="info"
      />
    </div>
    <button type="button" onclick={loadPromotions} disabled={loading}>Refresh</button>
  </header>

  {#if journeyState.runId && (journeyState.traceId || journeyState.evidenceTraceId)}
    <div class="journey-strip" aria-label="Promotion evidence trace target">
      <span>{journeyState.experimentId ?? journeyState.proposalId ?? journeyState.runId}</span>
      <EvidenceTraceLink
        projectId={activeProjectId}
        runId={journeyState.runId}
        traceId={journeyState.evidenceTraceId ?? journeyState.traceId}
        assetId={journeyState.assetId ?? ''}
        proposalId={journeyState.proposalId ?? ''}
        label="Console"
      />
    </div>
  {/if}

  {#if loading}
    <div class="state-panel" aria-live="polite">Loading pending promotions...</div>
  {:else if error}
    <div class="state-panel error-state" role="alert">
      <strong>Error loading promotions.</strong>
      <span>{error}</span>
      <button type="button" onclick={loadPromotions}>Retry</button>
    </div>
  {:else if items.length === 0}
    <div class="state-panel">No proposals are pending promotion.</div>
  {:else}
    <div class="summary-row">
      <strong>{items.length}</strong>
      <span>pending proposals</span>
    </div>
    <div class="promotion-list">
      {#each items as proposal (proposal.proposal_id)}
        <div class="proposal-row">
          <PromotionCard
            {proposal}
            projectId={activeProjectId}
            disabled={decidingProposalId === proposal.proposal_id}
            on:decide={handleDecide}
          />
          {#if cardErrors[proposal.proposal_id]}
            <p class="card-error">{cardErrors[proposal.proposal_id]}</p>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</section>

<style>
  .promotion-inbox {
    display: grid;
    gap: 1rem;
    max-width: 72rem;
  }

  .view-header,
  .summary-row {
    align-items: center;
    display: flex;
    justify-content: space-between;
  }

  .journey-strip {
    align-items: center;
    border: 1px solid #c7ced7;
    border-radius: 8px;
    display: flex;
    gap: 0.75rem;
    justify-content: space-between;
    padding: 0.75rem 1rem;
  }

  .journey-strip span {
    color: #5d6876;
    font-family: var(--font-mono, ui-monospace, SFMono-Regular, Consolas, monospace);
    font-size: 0.84rem;
    overflow-wrap: anywhere;
  }

  h2,
  p {
    margin: 0;
  }

  .view-header p {
    color: #5d6876;
    margin-top: 0.2rem;
  }

  button {
    border: 0;
    border-radius: 6px;
    cursor: pointer;
    font: inherit;
    font-weight: 700;
    min-height: 2.25rem;
    padding: 0.5rem 0.8rem;
  }

  button:disabled {
    cursor: wait;
    opacity: 0.6;
  }

  .state-panel {
    border: 1px solid #c7ced7;
    border-radius: 8px;
    display: grid;
    gap: 0.5rem;
    padding: 1rem;
  }

  .error-state {
    border-color: #d64533;
    color: #9a2d22;
  }

  .promotion-list,
  .proposal-row {
    display: grid;
    gap: 0.75rem;
  }

  .card-error {
    color: #9a2d22;
    font-weight: 700;
    margin: 0;
  }

  @media (max-width: 720px) {
    .promotion-inbox {
      max-width: none;
    }

    .view-header,
    .summary-row,
    .journey-strip {
      align-items: stretch;
      flex-direction: column;
      gap: 0.75rem;
    }

    .view-header button,
    .state-panel button {
      width: 100%;
    }
  }
</style>
