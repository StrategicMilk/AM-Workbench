<script>
  import {
    fetchWorkbenchStatusAssistantContext,
    fetchWorkbenchStatusSnapshot,
    resolveWorkbenchApprovalChain,
    runWorkbenchStatusAction,
  } from '$lib/api.js';
  import {
    FixActionDrawer,
    HealthResultTable,
    SettingsActionReceiptPanel,
    StatusSummaryPanel,
  } from '$lib/components/workbench/status';
  import { UpdateSafetyPanel } from '$lib/components/workbench/updates';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import { assertNoPlaceholders, buildReadinessSignals, requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { projectId = 'default' } = $props();
  let loading = $state(false);
  let actionBusy = $state(false);
  let adminRequired = $state(false);
  let error = $state('');
  let snapshot = $state(null);
  let assistantContext = $state(null);
  let selectedDomain = $state('all');
  let selectedResult = $state(null);
  let actionResult = $state(null);

  const states = ['configured', 'degraded', 'broken', 'busy', 'stale', 'approval_required'];
  let results = $derived(snapshot?.results ?? []);
  let domains = $derived(['all', ...Array.from(new Set(results.map((result) => result.domain)))]);

  const readinessKinds = [
    'setup', 'identity', 'config', 'provider', 'capability_pack', 'policy',
    'scheduler', 'memory', 'connector', 'tool_pin', 'run_kernel', 'verified_history', 'probe',
  ];

  function snapshotEvidenceRefs() {
    return requireEvidence(
      results.flatMap((result) => result.evidence_refs ?? result.evidence ?? []),
      'workbench_status.snapshot_evidence_refs',
    );
  }

  function isAdminRequiredError(err) {
    const status = err?.status ?? err?.statusCode ?? err?.response?.status;
    const message = String(err?.message ?? '').toLowerCase();
    return err?.code === 'admin_required' || status === 401 || status === 403 || message.includes('admin');
  }

  function approvalPayload(actionId) {
    const evidenceRefs = snapshotEvidenceRefs();
    if (evidenceRefs.length === 0) {
      throw new Error('Workbench status actions require current health result evidence refs.');
    }
    const requestPayload = {
      project_id: projectId,
      session_id: 'workbench-status-console',
      channel: 'desktop',
      action_id: actionId,
      action_type: 'settings_change',
      actor_id: 'workbench-status-console',
      run_id: `workbench-status:${actionId}`,
      risk_domain: 'tool_invocation',
      summary: `Apply Workbench status action ${actionId}`,
      action_fingerprint: `workbench-status:${projectId}:${actionId}`,
      approval_sources: [],
      governance_mode: 'observe',
      governance_available: true,
      readiness_signals: buildReadinessSignals(snapshot, readinessKinds),
      evidence_links: evidenceRefs.map((ref) => ({
        evidence_id: ref,
        kind: 'external',
        ref,
        summary: `health result evidence: ${ref}`,
      })),
      authority_refs: ['workbench-status-console'],
    };
    assertNoPlaceholders(requestPayload, ['readiness_signals.*.evidence_refs[].ref', 'evidence_links[].ref'], 'workbench_status.approval_payload');
    return requestPayload;
  }

  async function loadSnapshot() {
    loading = true;
    error = '';
    adminRequired = false;
    try {
      snapshot = await fetchWorkbenchStatusSnapshot(projectId);
      assistantContext = await fetchWorkbenchStatusAssistantContext(projectId);
      if (!selectedResult && snapshot?.results?.length) selectedResult = snapshot.results[0];
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
      adminRequired = isAdminRequiredError(err);
    } finally {
      loading = false;
    }
  }

  async function submitAction(actionId) {
    actionBusy = true;
    error = '';
    try {
      const decision = await resolveWorkbenchApprovalChain(approvalPayload(actionId));
      actionResult = await runWorkbenchStatusAction({
        project_id: projectId,
        action_id: actionId,
        approval_decision: decision,
      });
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    } finally {
      actionBusy = false;
    }
  }

  $effect(() => {
    void projectId;
    loadSnapshot();
  });
</script>

<section class="status-view" aria-label="Workbench status health console">
  <header class="view-header">
    <div>
      <h1>Workbench Status</h1>
      <p>Health check results across all monitored workbench domains. States: configured, degraded, broken, busy, stale, approval_required.</p>
      <HelpPopover
        title="Health check categories"
        body="configured — domain is set up and passing all checks. degraded — domain is operational but one or more checks warn. broken — domain has a failing check; operator action required. busy — a long-running operation is in progress; checks are deferred. stale — check results are older than the freshness window; refresh to update. approval_required — an administrative approval gate is blocking this domain. Use the fix-action drawer to resolve individual failing checks. Support bundle captures a snapshot of all check results for escalation."
        severity="info"
      />
    </div>
    <div class="actions">
      <select bind:value={selectedDomain} aria-label="Health domain filter">
        {#each domains as domain}
          <option value={domain}>{domain}</option>
        {/each}
      </select>
      <button onclick={loadSnapshot} disabled={loading}>{loading ? 'Refreshing' : 'Refresh'}</button>
    </div>
  </header>

  {#if adminRequired}
    <div class="status blocked" role="alert">admin access required</div>
  {:else if error}
    <div class="status blocked" role="alert">blocked {error}</div>
  {:else if loading && !snapshot}
    <div class="status" role="status" aria-live="polite">loading</div>
  {/if}

  {#if snapshot}
    <StatusSummaryPanel {snapshot} />
    <UpdateSafetyPanel {projectId} currentVersion={snapshot?.version ?? '0.0.0-dev'} />
    <div class="status-layout">
      <HealthResultTable {results} {selectedDomain} onSelect={(result) => { selectedResult = result; }} />
      <div class="side-panels">
        <FixActionDrawer result={selectedResult} busy={actionBusy} onRun={submitAction} />
        <SettingsActionReceiptPanel result={actionResult} />
      </div>
    </div>
    <section class="assistant-context" aria-label="Assistant read-only context">
      <h2>Assistant Context</h2>
      <p>read_only={String(assistantContext?.read_only ?? true)} callbacks={(assistantContext?.write_callbacks ?? []).length}</p>
      <div class="state-strip">
        {#each states as state}
          <span>{state}</span>
        {/each}
      </div>
    </section>
  {:else if !loading}
    <div class="status blocked">empty snapshot</div>
  {/if}
</section>

<style>
  .status-view {
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: 20px;
    color: var(--text-primary);
  }
  .view-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
  }
  .view-header h1 { margin: 0 0 4px; font-size: 24px; }
  .view-header p { margin: 0; color: var(--text-muted); overflow-wrap: anywhere; }
  .actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  button, select {
    min-height: 44px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: var(--text-primary);
    font: inherit;
    padding: 7px 10px;
  }
  .status {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 10px 12px;
    background: var(--surface-elevated, #111827);
  }
  .status.blocked { border-color: #d44d4d; }
  .status-layout {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(260px, 320px);
    gap: 12px;
    align-items: start;
  }
  .side-panels { display: grid; gap: 12px; }
  .assistant-context {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 12px;
    background: var(--surface-elevated, #111827);
  }
  .assistant-context h2 { margin: 0 0 8px; font-size: 16px; }
  .state-strip { display: flex; flex-wrap: wrap; gap: 8px; }
  .state-strip span {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 4px 7px;
    color: var(--text-muted);
  }
  @media (max-width: 980px) {
    .view-header { flex-direction: column; }
    .status-layout { grid-template-columns: 1fr; }
  }
</style>
