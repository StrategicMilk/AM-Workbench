<script>
  import { showToast } from '$lib/stores/toast.svelte.js';
  import { workbenchKernelRequest } from '$lib/api.js';
  import { unwrapReadiness } from '$lib/contracts/unwrap.js';
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';
  import HelpTooltip from '$lib/components/help/HelpTooltip.svelte';

  let loading = $state(true);
  let actionPending = $state(false);
  let readiness = $state(null);
  let error = $state(null);
  let activeRuntime = $state('lmstudio');

  const runtimeLabels = {
    lmstudio: 'LM Studio',
    jan: 'Jan',
    openwebui: 'Open WebUI',
  };

  let probes = $derived(readiness?.probes ?? []);
  let blockers = $derived(readiness?.blockers ?? []);
  let hardwareFit = $derived(Object.values(readiness?.hardware_fit_by_model ?? {}));
  let schedulerLanes = $derived(Object.entries(readiness?.scheduler_lanes_ready ?? {}));

  async function loadHealth() {
    loading = true;
    error = null;
    try {
      const data = await workbenchKernelRequest('/api/v1/workbench/onboarding/health');
      readiness = unwrapReadiness(data);
    } catch (err) {
      error = err.message;
    } finally {
      loading = false;
    }
  }

  async function refresh(dryRun = false) {
    actionPending = true;
    error = null;
    try {
      const data = await workbenchKernelRequest('/api/v1/workbench/onboarding/refresh', {
        method: 'POST',
        body: JSON.stringify({ dry_run: dryRun }),
      });
      readiness = unwrapReadiness(data);
      showToast(dryRun ? 'Runtime setup checked' : 'Runtime setup refreshed', 'success');
    } catch (err) {
      error = err.message;
      showToast(`Runtime refresh failed: ${err.message}`, 'error');
    } finally {
      actionPending = false;
    }
  }

  async function smokeTest(runtime) {
    actionPending = true;
    error = null;
    try {
      const result = await workbenchKernelRequest('/api/v1/workbench/onboarding/smoke-test', {
        method: 'POST',
        body: JSON.stringify({ runtime, sample_prompt: 'ping' }),
      });
      showToast(result.success ? `${runtimeLabels[runtime]} smoke test passed` : `${runtimeLabels[runtime]} smoke test failed`, result.success ? 'success' : 'warning');
      await loadHealth();
    } catch (err) {
      error = err.message;
      showToast(`Smoke test failed: ${err.message}`, 'error');
    } finally {
      actionPending = false;
    }
  }

  $effect(() => { loadHealth(); });
</script>

<div class="runtime-setup-view">
  <div class="view-header">
    <div>
      <h2><i class="fas fa-server" aria-hidden="true"></i> Local Runtime Setup</h2>
      <HelpPopover
        title="Local Runtime Setup"
        body="First-run and ongoing health check for AM Workbench's local inference runtimes. The launcher (vetinari/desktop/launcher) probes each configured runtime endpoint (LM Studio, Jan, Open WebUI) at startup and reports reachability, discovered models, and latency. Hardware Fit shows whether each configured model's memory requirements can be met by the detected hardware. Scheduler Lanes shows which execution lanes are ready to accept work. Use Refresh to re-probe all endpoints after making changes to your local runtime configuration."
        severity="info"
      />
    </div>
    <div class="runtime-actions">
      <button class="btn btn-secondary" onclick={() => refresh(true)} disabled={actionPending}>
        <i class="fas fa-stethoscope" aria-hidden="true"></i>
        Check
      </button>
      <button class="btn btn-primary" onclick={() => refresh(false)} disabled={actionPending}>
        <i class="fas fa-sync-alt" class:fa-spin={actionPending} aria-hidden="true"></i>
        Refresh
      </button>
    </div>
  </div>

  {#if loading}
    <div class="loading-state" role="status" aria-live="polite">
      <i class="fas fa-spinner fa-spin" aria-hidden="true"></i>
      Loading runtime setup...
    </div>
  {:else if error}
    <div class="runtime-alert" role="alert">
      <i class="fas fa-exclamation-triangle" aria-hidden="true"></i>
      <span>{error}</span>
      <button class="btn btn-secondary" onclick={loadHealth}>Retry</button>
    </div>
  {:else}
    <section class="runtime-grid" aria-label="Runtime endpoints">
      {#each ['lmstudio', 'jan', 'openwebui'] as runtime}
        {@const probe = probes.find((item) => item.runtime_kind === runtime)}
        <article class="runtime-card" class:ready={probe?.reachable}>
          <header>
            <h3>{runtimeLabels[runtime]}</h3>
            <span class="status-pill" class:ok={probe?.reachable}>{probe?.reachable ? 'Ready' : 'Blocked'}</span>
          </header>
          <dl>
            <div><dt>Endpoint</dt><dd>{probe?.base_url ?? 'Not checked'}</dd></div>
            <div><dt>Models</dt><dd>{probe?.discovered_models?.length ?? 0}</dd></div>
            <div><dt>Latency</dt><dd>{probe?.latency_ms ?? 0} ms</dd></div>
          </dl>
          {#if probe?.error}
            <p class="runtime-error">{probe.error}</p>
          {/if}
          <button class="btn btn-small btn-secondary" onclick={() => smokeTest(runtime)} disabled={actionPending}>
            <i class="fas fa-vial" aria-hidden="true"></i>
            Run smoke test
          </button>
        </article>
      {/each}
    </section>

    <section class="setup-section">
      <h3>Blockers</h3>
      {#if blockers.length === 0}
        <p class="empty-state">No runtime blockers reported.</p>
      {:else}
        <div class="blocker-list">
          {#each blockers as blocker, index (`${blocker.kind}-${index}`)}
            <article class="blocker-row">
              <strong>{blocker.kind}</strong>
              <span>{blocker.message}</span>
              <p>{blocker.remediation}</p>
            </article>
          {/each}
        </div>
      {/if}
    </section>

    <section class="setup-section">
      <h3>Hardware Fit</h3>
      <div class="fit-table">
        {#each hardwareFit as fit (fit.model_id)}
          <div class="fit-row" class:bad={!fit.fits}>
            <span>{fit.model_id}</span>
            <span>{fit.required_memory_gb} GB required</span>
            <span>{fit.available_memory_gb} GB detected</span>
            <span>{fit.reason}</span>
          </div>
        {/each}
      </div>
    </section>

    <section class="setup-section">
      <h3>Scheduler Lanes</h3>
      <div class="lane-list">
        {#each schedulerLanes as [lane, ready] (lane)}
          <span class="lane-chip" class:ready role="status" aria-label={`Scheduler lane ${lane} is ${ready ? 'ready' : 'not ready'}`}>
            <i class={ready ? 'fas fa-check-circle' : 'fas fa-circle-exclamation'} aria-hidden="true"></i>
            {lane}
          </span>
        {/each}
      </div>
    </section>
  {/if}
</div>

<style>
  .runtime-setup-view {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }

  .view-header,
  .runtime-actions,
  .lane-list {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .view-header {
    justify-content: space-between;
  }

  .runtime-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 1rem;
  }

  .runtime-card,
  .setup-section,
  .runtime-alert {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem;
    background: var(--surface-color);
  }

  .runtime-card header,
  .blocker-row,
  .fit-row {
    display: flex;
    gap: 0.75rem;
    justify-content: space-between;
  }

  .status-pill,
  .lane-chip {
    border: 1px solid var(--border-color);
    border-radius: 999px;
    padding: 0.25rem 0.5rem;
  }

  .status-pill.ok,
  .lane-chip.ready {
    color: var(--success-color);
  }

  .blocker-list,
  .fit-table {
    display: grid;
    gap: 0.75rem;
  }

  .blocker-row,
  .fit-row {
    align-items: start;
    border-top: 1px solid var(--border-color);
    padding-top: 0.75rem;
  }

  .fit-row.bad,
  .runtime-error {
    color: var(--warning-color);
  }

  @media (max-width: 720px) {
    .view-header {
      align-items: stretch;
      flex-direction: column;
    }

    .runtime-actions,
    .lane-list {
      flex-wrap: wrap;
    }

    .runtime-actions .btn {
      flex: 1 1 10rem;
    }

    .runtime-grid {
      grid-template-columns: 1fr;
    }

    .runtime-card header,
    .blocker-row,
    .fit-row {
      align-items: flex-start;
      flex-direction: column;
    }
  }
</style>
