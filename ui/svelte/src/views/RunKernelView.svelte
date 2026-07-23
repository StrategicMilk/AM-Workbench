<script>
  import { showToast } from '$lib/stores/toast.svelte.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';
  import { workbenchKernelRequest } from '$lib/api.js';
  import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

  let { projectId = 'default' } = $props();

  let runId = $state(crypto.randomUUID());
  let snapshot = $state(null);
  let loading = $state(false);
  let error = $state(null);
  let runRequest = $state({
    workload_kind: 'agent_run',
    lease_id: '',
    policy_decision_ref: '',
    dry_run_ref: '',
    shadow_run_ref: '',
    context_manifest_ref: '',
    artifact_refs: '',
    trace_refs: '',
    eval_refs: '',
    repro_capsule_refs: '',
  });
  let checkpointRequest = $state({
    checkpoint_id: '',
    payload_ref: '',
    payload_hash: '',
  });

  let status = $derived(snapshot?.status ?? 'unknown');
  let recoveryAction = $derived(snapshot?.recovery_action ?? 'none');
  let evidenceLinks = $derived(snapshot?.snapshot?.evidence_links ?? {});
  let checkpoint = $derived(snapshot?.snapshot?.checkpoint ?? {});
  let events = $derived(snapshot?.snapshot?.events ?? []);
  let requiredRunRefsMissing = $derived(
    !runRequest.lease_id.trim()
      || !runRequest.policy_decision_ref.trim()
      || !runRequest.dry_run_ref.trim()
      || !runRequest.shadow_run_ref.trim()
      || !runRequest.context_manifest_ref.trim()
  );
  let requiredCheckpointRefsMissing = $derived(
    !checkpointRequest.checkpoint_id.trim()
      || !checkpointRequest.payload_ref.trim()
      || !checkpointRequest.payload_hash.trim()
  );

  async function kernelRequest(path, options = {}) {
    try {
      return await workbenchKernelRequest(path, options);
    } catch (err) {
      throw new Error(err?.message ?? String(err));
    }
  }

  function refList(value) {
    return String(value ?? '')
      .split(/[\n,]/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function requiredRef(value, context) {
    const refs = requireEvidence(value, context);
    if (refs.length === 0) {
      throw new Error(`${context} is required.`);
    }
    return refs[0];
  }

  async function inspectRun() {
    if (!runId.trim()) return;
    loading = true;
    error = null;
    try {
      snapshot = await kernelRequest(
        `/api/workbench/run-kernel/runs/${encodeURIComponent(runId)}?project_id=${encodeURIComponent(projectId)}`
      );
    } catch (err) {
      error = err.message ?? String(err);
    } finally {
      loading = false;
    }
  }

  async function startRun() {
    if (requiredRunRefsMissing) {
      error = 'Lease, policy, dry-run, shadow-run, and context manifest refs are required.';
      showToast(error, 'error');
      return;
    }
    loading = true;
    error = null;
    try {
      snapshot = await kernelRequest('/api/workbench/run-kernel/runs', {
        method: 'POST',
        body: JSON.stringify({
          project_id: projectId,
          run_id: runId,
          workload_kind: runRequest.workload_kind || 'agent_run',
          lease_id: requiredRef(runRequest.lease_id, 'run_kernel.lease_id'),
          policy_decision_ref: requiredRef(runRequest.policy_decision_ref, 'run_kernel.policy_decision_ref'),
          dry_run_ref: requiredRef(runRequest.dry_run_ref, 'run_kernel.dry_run_ref'),
          shadow_run_ref: requiredRef(runRequest.shadow_run_ref, 'run_kernel.shadow_run_ref'),
          context_manifest_ref: requiredRef(runRequest.context_manifest_ref, 'run_kernel.context_manifest_ref'),
          artifacts: requireEvidence(refList(runRequest.artifact_refs), 'run_kernel.artifacts'),
          evidence_links: {
            trace_refs: requireEvidence(refList(runRequest.trace_refs), 'run_kernel.trace_refs'),
            eval_refs: requireEvidence(refList(runRequest.eval_refs), 'run_kernel.eval_refs'),
            repro_capsule_refs: requireEvidence(refList(runRequest.repro_capsule_refs), 'run_kernel.repro_capsule_refs'),
          },
        }),
      });
      showToast('Run kernel object started.', 'success');
    } catch (err) {
      error = err.message ?? String(err);
      showToast(`Run start failed: ${error}`, 'error');
    } finally {
      loading = false;
    }
  }

  async function sealCheckpoint() {
    if (requiredCheckpointRefsMissing) {
      error = 'Checkpoint id, payload ref, and payload hash are required.';
      showToast(error, 'error');
      return;
    }
    if (!confirm(`Seal checkpoint for run "${runId}" and mark it interrupted?`)) return;
    loading = true;
    error = null;
    try {
      snapshot = await kernelRequest(`/api/workbench/run-kernel/runs/${encodeURIComponent(runId)}/checkpoint`, {
        method: 'POST',
        body: JSON.stringify({
          project_id: projectId,
          checkpoint_id: requiredRef(checkpointRequest.checkpoint_id, 'run_kernel.checkpoint_id'),
          payload_ref: requiredRef(checkpointRequest.payload_ref, 'run_kernel.payload_ref'),
          payload_hash: requiredRef(checkpointRequest.payload_hash, 'run_kernel.payload_hash'),
          mark_interrupted: true,
        }),
      });
      showToast('Checkpoint sealed.', 'success');
    } catch (err) {
      error = err.message ?? String(err);
      showToast(`Checkpoint failed: ${error}`, 'error');
    } finally {
      loading = false;
    }
  }

  async function resumeRun() {
    loading = true;
    error = null;
    try {
      snapshot = await kernelRequest(`/api/workbench/run-kernel/runs/${encodeURIComponent(runId)}/resume`, {
        method: 'POST',
        body: JSON.stringify({ project_id: projectId }),
      });
      showToast('Run resumed from checkpoint.', 'success');
    } catch (err) {
      error = err.message ?? String(err);
      showToast(`Resume failed: ${error}`, 'error');
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    void inspectRun();
  });
</script>

<main class="run-kernel" aria-label="Run Kernel">
  <header class="kernel-header">
    <div>
      <h1>Run Kernel</h1>
      <p>Inspect and control durable run objects for project {projectId}.</p>
      <HelpPopover
        title="Run kernel"
        body="Run objects are durable: they persist across workbench restarts and survive process crashes. Status states: running (active), succeeded (completed normally), interrupted (stopped before completion), blocked (waiting on an upstream gate or approval), recovery_needed (the run exited abnormally and requires operator action). Restart: resumes the run from its last sealed checkpoint; if no checkpoint exists, the run restarts from the beginning. Recovery states: check the recovery_action field for the recommended next step — options include retry, rollback, or manual intervention. Heartbeat indicator: the kernel emits a heartbeat event every 30 seconds while running; absence of a heartbeat for >2 minutes indicates a hung run."
        severity="info"
      />
    </div>
    <div class="run-input">
      <input bind:value={runId} aria-label="Run id" />
      <button type="button" onclick={inspectRun} disabled={loading}>
        <i class="fas fa-magnifying-glass"></i>
        <span>Inspect</span>
      </button>
    </div>
  </header>

  <section class="toolbar" aria-label="Run kernel actions">
    <button type="button" onclick={startRun} disabled={loading || !runId.trim() || requiredRunRefsMissing}>
      <i class="fas fa-play"></i>
      <span>Start</span>
    </button>
    <button type="button" onclick={sealCheckpoint} disabled={loading || !runId.trim() || requiredCheckpointRefsMissing}>
      <i class="fas fa-lock"></i>
      <span>Checkpoint</span>
    </button>
    <button type="button" onclick={resumeRun} disabled={loading || !runId.trim()}>
      <i class="fas fa-rotate-right"></i>
      <span>Resume</span>
    </button>
  </section>

  <section class="request-grid" aria-label="Run request refs">
    <label>
      <span>Workload</span>
      <input bind:value={runRequest.workload_kind} aria-label="Workload kind" />
    </label>
    <label>
      <span>Lease ref</span>
      <input bind:value={runRequest.lease_id} aria-label="Lease ref" />
    </label>
    <label>
      <span>Policy ref</span>
      <input bind:value={runRequest.policy_decision_ref} aria-label="Policy decision ref" />
    </label>
    <label>
      <span>Dry run ref</span>
      <input bind:value={runRequest.dry_run_ref} aria-label="Dry run ref" />
    </label>
    <label>
      <span>Shadow run ref</span>
      <input bind:value={runRequest.shadow_run_ref} aria-label="Shadow run ref" />
    </label>
    <label>
      <span>Context manifest</span>
      <input bind:value={runRequest.context_manifest_ref} aria-label="Context manifest ref" />
    </label>
    <label>
      <span>Artifacts</span>
      <input bind:value={runRequest.artifact_refs} aria-label="Artifact refs" />
    </label>
    <label>
      <span>Trace refs</span>
      <input bind:value={runRequest.trace_refs} aria-label="Trace refs" />
    </label>
    <label>
      <span>Eval refs</span>
      <input bind:value={runRequest.eval_refs} aria-label="Eval refs" />
    </label>
    <label>
      <span>Repro refs</span>
      <input bind:value={runRequest.repro_capsule_refs} aria-label="Repro capsule refs" />
    </label>
    <label>
      <span>Checkpoint id</span>
      <input bind:value={checkpointRequest.checkpoint_id} aria-label="Checkpoint id" />
    </label>
    <label>
      <span>Payload ref</span>
      <input bind:value={checkpointRequest.payload_ref} aria-label="Checkpoint payload ref" />
    </label>
    <label>
      <span>Payload hash</span>
      <input bind:value={checkpointRequest.payload_hash} aria-label="Checkpoint payload hash" />
    </label>
  </section>

  {#if loading}
    <section class="state" role="status" aria-live="polite">Loading run kernel state.</section>
  {:else if error}
    <section class="state error" role="alert">{error}</section>
  {:else}
    <section class="status-strip" aria-label="Run status">
      <span data-status={status}>{status}</span>
      <span>{recoveryAction}</span>
      <span>{snapshot?.snapshot?.restart_count ?? 0} restarts</span>
    </section>

    <section class="kernel-grid" aria-label="Run kernel detail">
      <article>
        <h2>Checkpoint</h2>
        <dl>
          <div><dt>sealed</dt><dd>{checkpoint.sealed ? 'yes' : 'no'}</dd></div>
          <div><dt>id</dt><dd>{checkpoint.checkpoint_id || 'none'}</dd></div>
          <div><dt>payload</dt><dd>{checkpoint.payload_ref || 'none'}</dd></div>
        </dl>
      </article>
      <article>
        <h2>Capsule Links</h2>
        <dl>
          <div><dt>traces</dt><dd>{(evidenceLinks.trace_refs ?? []).join(', ') || 'none'}</dd></div>
          <div><dt>evals</dt><dd>{(evidenceLinks.eval_refs ?? []).join(', ') || 'none'}</dd></div>
          <div><dt>repro</dt><dd>{(evidenceLinks.repro_capsule_refs ?? []).join(', ') || 'none'}</dd></div>
        </dl>
      </article>
    </section>

    <section class="events" aria-label="Run events">
      <h2>Events</h2>
      <ol>
        {#each events as event}
          <li>{event}</li>
        {:else}
          <li>No events recorded.</li>
        {/each}
      </ol>
    </section>
  {/if}
</main>

<style>
  .run-kernel {
    display: grid;
    gap: 16px;
    min-height: 100%;
    padding: 18px;
    background: var(--bg-primary, #0b1120);
    color: var(--text-primary, #e5e7eb);
  }

  .kernel-header,
  .toolbar,
  .status-strip {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    justify-content: space-between;
  }

  .request-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
    gap: 10px;
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 12px;
  }

  .request-grid label {
    display: grid;
    gap: 5px;
    min-width: 0;
  }

  .request-grid span {
    color: var(--text-muted, #94a3b8);
    font-size: 12px;
  }

  h1,
  h2,
  p,
  dl,
  ol {
    margin: 0;
  }

  h1 {
    font-size: 28px;
    letter-spacing: 0;
  }

  h2 {
    font-size: 15px;
    letter-spacing: 0;
  }

  p,
  dt {
    color: var(--text-muted, #94a3b8);
  }

  .run-input {
    display: flex;
    gap: 8px;
    min-width: min(100%, 360px);
  }

  input {
    min-width: 0;
    flex: 1;
    border: 1px solid var(--border-default, #334155);
    border-radius: 6px;
    background: var(--surface-elevated, #111827);
    color: inherit;
    padding: 8px 10px;
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

  button:disabled {
    opacity: 0.62;
    cursor: wait;
  }

  .status-strip span,
  article,
  .events,
  .state {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    padding: 10px;
  }

  .status-strip span[data-status="running"],
  .status-strip span[data-status="succeeded"] {
    color: #86efac;
  }

  .status-strip span[data-status="interrupted"],
  .status-strip span[data-status="blocked"],
  .status-strip span[data-status="recovery_needed"] {
    color: #fbbf24;
  }

  .kernel-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
  }

  article,
  .events {
    display: grid;
    gap: 10px;
  }

  dl {
    display: grid;
    gap: 8px;
  }

  dl div {
    min-width: 0;
    border-top: 1px solid var(--border-default, #334155);
    padding-top: 8px;
  }

  dd,
  li {
    overflow-wrap: anywhere;
  }

  .error {
    color: var(--danger, #fca5a5);
  }

  @media (max-width: 780px) {
    .kernel-header,
    .run-input,
    .kernel-grid {
      display: grid;
      grid-template-columns: 1fr;
    }
  }
</style>
