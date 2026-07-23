<script>
  import { appState } from '$lib/stores/app.svelte.js';
  import { fetchResourceCockpitSnapshot } from '$lib/api.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import {
    ResourceCockpitLeaseTable,
    ResourceCockpitMachinePanel,
    ResourceCockpitPolicyTuningPanel,
    ResourceCockpitQueuedTable,
  } from '$lib/components/workbench/resources';

  let loading = $state(true);
  let error = $state(null);
  let snapshot = $state(null);
  let selectedProposalId = $state(null);
  let lastApprovalDiff = $state(null);

  let machineProfile = $derived(snapshot?.machine_profile ?? {});
  let runtimeAppliance = $derived(snapshot?.runtime_appliance ?? {});
  let activeLeases = $derived(snapshot?.active_leases ?? []);
  let queuedJobs = $derived(snapshot?.queued_jobs ?? []);
  let safeActions = $derived(snapshot?.safe_actions ?? []);
  let policyProposals = $derived(snapshot?.policy_proposals ?? []);
  let degradationReasons = $derived(snapshot?.degradation_reasons ?? []);
  let overallStatus = $derived(snapshot?.overall_status ?? 'unknown');

  async function loadSnapshot() {
    loading = true;
    error = null;
    try {
      const result = await fetchResourceCockpitSnapshot();
      snapshot = result?.snapshot ?? result;
    } catch (err) {
      error = err?.message ?? String(err);
    } finally {
      loading = false;
    }
  }

  function valueOrUnknown(value, suffix = '') {
    if (value === null || value === undefined || value === '') return 'unknown';
    return `${value}${suffix}`;
  }

  function handleAction(action) {
    selectedProposalId = action.target_ref;
  }

  function handleApprovalDiff(diff) {
    selectedProposalId = diff.proposal_id;
    lastApprovalDiff = diff;
  }

  $effect(() => {
    void loadSnapshot();
  });
</script>

<main class="resource-cockpit" aria-label="Resource Cockpit">
  <header class="cockpit-header">
    <div>
      <h1>Resource Cockpit</h1>
      <p>GPU, RAM, CPU, SSD, queue, lease, and policy state for local Workbench resources.</p>
      <HelpPopover
        title="Resource Cockpit"
        body="Live hardware and scheduling state for this Workbench node. The hardware digital twin measures actual GPU VRAM, RAM, CPU, and SSD availability against the requirements of each configured model. Hardware bottleneck indicators show which resource is the binding constraint for the current workload. Storage tiering shows which model files are in fast local storage vs. slow remote paths. Service residency shows which inference backends are active and their per-model allocation. Use policy proposals to tune concurrency limits and lease durations without restarting the Workbench."
        severity="info"
      />
    </div>
    <button type="button" onclick={loadSnapshot} disabled={loading}>
      <i class="fas fa-rotate"></i>
      <span>Refresh</span>
    </button>
  </header>

  {#if loading}
    <section class="loading-state" role="status" aria-live="polite">Loading resource cockpit...</section>
  {:else if error}
    <section class="cockpit-alert" role="alert">
      <strong>Action required</strong>
      <span>{error}</span>
    </section>
  {:else if snapshot}
    <section class="status-strip" aria-label="Resource cockpit status">
      <span class="status-pill" data-status={overallStatus}>{overallStatus}</span>
      <span>{valueOrUnknown(snapshot.concurrency_profile_id)}</span>
      {#if selectedProposalId}
        <span>{selectedProposalId}</span>
      {/if}
      {#if lastApprovalDiff}
        <span>{lastApprovalDiff.status}</span>
      {/if}
    </section>

    {#if overallStatus === 'degraded' || overallStatus === 'unknown' || degradationReasons.length > 0}
      <section class="cockpit-alert" role="alert" aria-live="assertive">
        <strong>Degraded resource state</strong>
        <ul>
          {#each degradationReasons as reason (reason)}
            <li>{reason}</li>
          {/each}
        </ul>
      </section>
    {/if}

    <section class="cockpit-grid" aria-label="Resource cockpit panels">
      <ResourceCockpitMachinePanel
        machineProfile={machineProfile}
        runtimeAppliance={runtimeAppliance}
        degradationReasons={degradationReasons}
      />
      <ResourceCockpitQueuedTable queued={queuedJobs} />
    </section>

    <ResourceCockpitLeaseTable leases={activeLeases} safeActions={safeActions} onAction={handleAction} />
    <ResourceCockpitPolicyTuningPanel
      proposals={policyProposals}
      projectId={appState.currentProjectId || 'default'}
      onRequestApprovalDiff={handleApprovalDiff}
    />
  {/if}
</main>

<style>
  .resource-cockpit {
    display: grid;
    gap: 16px;
    min-height: 100%;
    padding: 18px;
    background: var(--bg-primary, #0b1120);
    color: var(--text-primary, #e5e7eb);
  }

  .cockpit-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 16px;
  }

  h1,
  p {
    margin-top: 0;
  }

  h1 {
    margin-bottom: 4px;
    font-size: 28px;
    letter-spacing: 0;
  }

  p {
    margin-bottom: 0;
    color: var(--text-muted, #94a3b8);
  }

  button {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: inherit;
    padding: 8px 11px;
  }

  .status-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }

  .status-strip span,
  .loading-state,
  .cockpit-alert {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 10px;
  }

  .status-pill[data-status="ready"] {
    color: #86efac;
  }

  .status-pill[data-status="approval_required"] {
    color: #fbbf24;
  }

  .status-pill[data-status="degraded"],
  .status-pill[data-status="unknown"] {
    color: #fca5a5;
  }

  .cockpit-alert {
    border-color: #f59e0b;
  }

  .cockpit-alert ul {
    margin: 8px 0 0;
  }

  .cockpit-grid {
    display: grid;
    grid-template-columns: minmax(0, 2fr) minmax(260px, 1fr);
    gap: 16px;
  }

  @media (max-width: 900px) {
    .cockpit-header,
    .cockpit-grid {
      grid-template-columns: 1fr;
    }

    .cockpit-header {
      flex-direction: column;
    }
  }
</style>
