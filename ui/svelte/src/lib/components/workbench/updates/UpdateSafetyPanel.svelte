<script>
  import { resolveWorkbenchApprovalChain } from '$lib/api.js';
  import RollbackSupportPanel from './RollbackSupportPanel.svelte';
  import SupportBundleDialog from './SupportBundleDialog.svelte';
  import UpdateChannelSelector from './UpdateChannelSelector.svelte';
  import UpdateManifestCard from './UpdateManifestCard.svelte';
  import { createUpdateSafetyStore } from './updateSafetyStore.svelte.js';
  import { CheckStatus, ReadinessState } from '$lib/contracts';
  import { assertNoPlaceholders, requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { projectId = 'default', currentVersion = '0.0.0-dev' } = $props();
  const UPDATE_CHANNEL_ORDER = ['stable', 'beta', 'canary'];
  const store = createUpdateSafetyStore({
    projectId: () => projectId,
    currentVersion: () => currentVersion,
  });
  let busy = $state(false);
  let dialogOpen = $state(false);

  let readiness = $derived(store.readiness);
  let state = $derived(store.state);
  let noAutoInstall = $derived(store.noAutoInstall);

  function updateEvidenceRefs() {
    return requireEvidence(
      [
        ...(Array.isArray(readiness?.evidence_refs) ? readiness.evidence_refs : []),
        readiness?.manifest?.public_export?.export_ref,
        readiness?.manifest?.integrity?.manifest_ref,
        readiness?.manifest?.integrity?.signature_ref,
        readiness?.manifest?.release_notes_ref,
        readiness?.public_export_ref,
        readiness?.release_notes_ref,
      ],
      'update_safety.evidence_refs',
    );
  }

  function approvalPayload(action, version) {
    const evidenceRefs = updateEvidenceRefs();
    if (evidenceRefs.length === 0) {
      throw new Error('Update approval requires runtime evidence refs from the readiness snapshot.');
    }
    const payload = {
      project_id: projectId,
      session_id: 'workbench-update-status-panel',
      channel: store.selectedChannel,
      action_id: `workbench-update-${action}:${version}`,
      action_type: 'settings_change',
      actor_id: 'workbench-update-status-panel',
      run_id: `workbench-update:${action}:${version}`,
      risk_domain: 'tool_invocation',
      summary: `Workbench update ${action} for ${version}`,
      action_fingerprint: `workbench-update:${action}:${store.selectedChannel}:${version}`,
      governance_mode: readiness?.governance_mode ?? 'update_safety_gate',
      governance_available: true,
      readiness_signals: {
        update_safety: {
          status: readiness?.state === ReadinessState.READY ? CheckStatus.PASSING : ReadinessState.BLOCKED,
          summary: readiness?.state ?? ReadinessState.BLOCKED,
          evidence_refs: evidenceRefs.map((ref) => ({ ref, kind: 'artifact' })),
        },
      },
      evidence_links: evidenceRefs.map((ref) => ({
        evidence_id: ref,
        kind: 'external',
        ref,
        summary: `update readiness evidence: ${ref}`,
      })),
      authority_refs: ['workbench-update-safety'],
    };
    assertNoPlaceholders(payload, ['readiness_signals.*.evidence_refs[].ref', 'evidence_links[].ref'], 'update_safety.approval_payload');
    return payload;
  }

  async function skipVersion() {
    if (!store.candidateVersion) {
      store.error = 'No update candidate is available to skip.';
      return;
    }
    busy = true;
    try {
      const requestPayload = approvalPayload('skip', store.candidateVersion);
      const decision = await resolveWorkbenchApprovalChain(requestPayload);
      if (!decision) {
        store.error = 'Approval decision is required before skipping an update.';
        return;
      }
      await store.skip(decision);
    } catch (err) {
      store.error = err instanceof Error ? err.message : String(err);
    } finally {
      busy = false;
    }
  }

  async function rollbackPlan() {
    busy = true;
    try {
      await store.requestRollbackPlan();
    } finally {
      busy = false;
    }
  }

  async function supportBundle() {
    busy = true;
    try {
      await store.createSupportBundle();
      dialogOpen = true;
    } finally {
      busy = false;
    }
  }
</script>

<section class="update-panel" aria-label="Workbench update safety" data-state={state}>
  <header>
    <div>
      <h2>Updates</h2>
      <p>{state} approval_required blocked ready skipped no_auto_install={String(noAutoInstall)}</p>
    </div>
    <UpdateChannelSelector
      channel={store.selectedChannel}
      channels={UPDATE_CHANNEL_ORDER}
      disabled={store.loading || busy}
      onChange={(value) => { store.selectedChannel = value; }}
    />
  </header>

  {#if store.error}
    <div class="status blocked">{store.error}</div>
  {:else if store.loading && !readiness}
    <div class="status">loading</div>
  {/if}

  <div class="grid">
    <UpdateManifestCard {readiness} />
    <RollbackSupportPanel
      {readiness}
      rollbackPlan={store.rollbackPlan}
      {busy}
      onRollbackPlan={rollbackPlan}
      onSupportBundle={supportBundle}
    />
  </div>

  <div class="actions">
    <button onclick={store.checkNow} disabled={store.loading || busy}>Check</button>
    <button onclick={skipVersion} disabled={state !== ReadinessState.READY || busy}>Skip</button>
  </div>

  <SupportBundleDialog result={store.supportBundle} open={dialogOpen} onClose={() => { dialogOpen = false; }} />
</section>

<style>
  .update-panel {
    display: grid;
    gap: 12px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 12px;
    background: var(--surface-elevated, #111827);
  }
  header {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    align-items: flex-start;
  }
  h2 {
    margin: 0 0 4px;
    font-size: 18px;
  }
  p {
    margin: 0;
    color: var(--text-muted);
    overflow-wrap: anywhere;
  }
  .grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(260px, 0.8fr);
    gap: 12px;
  }
  .actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  button {
    min-height: 36px;
    border: 1px solid var(--border-default);
    border-radius: 6px;
    background: var(--surface-base, #0b1020);
    color: var(--text-primary);
    padding: 7px 10px;
  }
  button:disabled {
    opacity: 0.5;
  }
  .status {
    border: 1px solid var(--border-default);
    border-radius: 6px;
    padding: 8px 10px;
  }
  .blocked {
    border-color: #d6a821;
  }
  [data-state="ready"] {
    border-color: #31a66a;
  }
  [data-state="blocked"] {
    border-color: #d6a821;
  }
  @media (max-width: 980px) {
    header {
      flex-direction: column;
    }
    .grid {
      grid-template-columns: 1fr;
    }
  }
</style>
